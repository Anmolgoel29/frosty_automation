# linkedin/tasks/check_inbox.py
"""Check-inbox task — cheap per-account poll for new message activity.

One Voyager conversations-list call per account (fetch_conversations) tells
us which conversations changed since the last poll, without a full-thread
fetch per lead. Only conversations that actually changed pay for the heavier
sync_conversation() (full-thread fetch + chat_summary update). See
ARCHITECTURE.md's Task Queue section for the ban-risk rationale behind the
polling interval.
"""
from __future__ import annotations

import logging
import random

from termcolor import colored

from linkedin.conf import CAMPAIGN_CONFIG

logger = logging.getLogger(__name__)


def handle_check_inbox(task, session):
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Max

    from chat.models import ChatMessage
    from crm.models import Deal, Lead
    from linkedin.actions.conversations import conversation_last_activity, match_conversations
    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.api.messaging import fetch_conversations
    from linkedin.db.chat import sync_conversation
    from linkedin.enums import ProfileState
    from linkedin.tasks.scheduler import enqueue_check_inbox, enqueue_follow_up

    payload = task.payload
    campaign_id = payload["campaign_id"]
    account = session.linkedin_profile

    def _reschedule():
        delay = random.uniform(
            CAMPAIGN_CONFIG["check_inbox_interval_min_seconds"],
            CAMPAIGN_CONFIG["check_inbox_interval_max_seconds"],
        )
        enqueue_check_inbox(campaign_id, account, delay_seconds=delay)

    logger.debug(
        "[%s] %s %s",
        session.campaign, colored("▶ check_inbox", "cyan", attrs=["bold"]),
        account.linkedin_username,
    )

    deals = list(
        Deal.objects.filter(
            assigned_profile=account,
            campaign_id=campaign_id,
            state=ProfileState.CONNECTED,
        )
        .select_related("lead")
        .only("lead__urn", "lead__public_identifier", "lead__human_takeover", "lead_id")
    )
    urn_to_deal = {d.lead.urn: d for d in deals if d.lead.urn}
    if not urn_to_deal:
        _reschedule()
        return

    ct = ContentType.objects.get_for_model(Lead)
    lead_ids = [d.lead_id for d in urn_to_deal.values()]
    last_known = dict(
        ChatMessage.objects.filter(content_type=ct, object_id__in=lead_ids)
        .values("object_id")
        .annotate(last=Max("creation_date"))
        .values_list("object_id", "last")
    )

    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)
    mailbox_urn = session.self_profile["urn"]
    raw = fetch_conversations(api, mailbox_urn)
    elements = raw.get("data", {}).get("messengerConversationsBySyncToken", {}).get("elements", [])
    matches = match_conversations(elements, set(urn_to_deal))

    changed = 0
    for urn, conv in matches.items():
        deal = urn_to_deal[urn]
        remote_activity = conversation_last_activity(conv)
        local_activity = last_known.get(deal.lead_id)
        if local_activity is not None and remote_activity is not None and remote_activity <= local_activity:
            continue

        changed += 1
        sync_conversation(session, deal.lead.public_identifier)

        newest_incoming = (
            ChatMessage.objects.filter(content_type=ct, object_id=deal.lead_id, is_outgoing=False)
            .order_by("-creation_date")
            .first()
        )
        is_new_reply = newest_incoming is not None and (
            local_activity is None or newest_incoming.creation_date > local_activity
        )
        # Direct path, not on_deal_state_entered — the reconcile-side
        # human_takeover exclusion (_seed_deal_tasks) doesn't cover this, so
        # it needs its own guard here.
        if is_new_reply and not deal.lead.human_takeover:
            enqueue_follow_up(campaign_id, deal.lead.public_identifier, account, delay_seconds=0)

    if changed:
        logger.info(
            "[%s] check_inbox %s: %d/%d conversation(s) had new activity",
            session.campaign, account.linkedin_username, changed, len(matches),
        )
    _reschedule()
