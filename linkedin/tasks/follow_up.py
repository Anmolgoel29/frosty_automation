# linkedin/tasks/follow_up.py
"""Follow-up task — runs the agentic follow-up for one CONNECTED profile."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from linkedin.models import ActionLog

logger = logging.getLogger(__name__)

# Required silence between nudges scales with unanswered count:
# 1 unanswered → 3d, 2 → 6d, 3 → 9d. Skips the LLM call while open.
MIN_DAYS_PER_UNANSWERED = 3


def _build_send_profile(deal) -> dict:
    """Minimal profile dict for ``send_raw_message`` and its fallbacks.

    Populated from the Lead row — all three send strategies (popup,
    direct-thread, API) now navigate by URN so no human-readable name
    is required.
    """
    lead = deal.lead
    return {
        "public_identifier": lead.public_identifier,
        "urn": lead.urn or "",
    }


def _unanswered_count(deal) -> int:
    """Count consecutive outgoing messages with no reply."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(type(deal.lead))
    messages = ChatMessage.objects.filter(content_type=ct, object_id=deal.lead_id)

    last_reply = messages.filter(is_outgoing=False).order_by("-creation_date").first()
    nudges = messages.filter(is_outgoing=True)
    if last_reply:
        nudges = nudges.filter(creation_date__gt=last_reply.creation_date)
        
    return nudges.count()


def _too_soon_to_nudge(deal, unanswered: int) -> bool:
    """Wait `unanswered_count * MIN_DAYS_PER_UNANSWERED` days between nudges."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(type(deal.lead))
    messages = ChatMessage.objects.filter(content_type=ct, object_id=deal.lead_id)

    last = messages.order_by("-creation_date").first()
    if last is None or not last.is_outgoing:
        return False

    required = timedelta(days=unanswered * MIN_DAYS_PER_UNANSWERED)
    return timezone.now() - last.creation_date < required


def handle_follow_up(task, session):
    from crm.models import Deal
    from linkedin.actions.message import send_raw_message
    from linkedin.agents.follow_up import run_follow_up_agent
    from linkedin.db.chat import sync_conversation
    from linkedin.db.deals import set_profile_state
    from linkedin.db.summaries import materialize_profile_summary_if_missing
    from linkedin.enums import ProfileState
    from linkedin.tasks.scheduler import enqueue_follow_up

    payload = task.payload
    public_id = payload["public_id"]
    campaign_id = payload["campaign_id"]
    # The daemon routed this task to the account that owns the deal, so the
    # conversation we're about to read and answer is on this account.
    account = session.linkedin_profile

    logger.info(
        "[%s] %s %s as %s",
        session.campaign, colored("\u25b6 follow_up", "green", attrs=["bold"]),
        public_id, account.linkedin_username,
    )

    # Rate limit check
    if not account.can_execute(ActionLog.ActionType.FOLLOW_UP):
        enqueue_follow_up(campaign_id, public_id, account, delay_seconds=3600)
        return

    deal = (
        Deal.objects.filter(lead__public_identifier=public_id, campaign=session.campaign)
        .select_related("lead", "campaign")
        .first()
    )
    if deal is None:
        logger.warning("follow_up: no Deal for %s — skipping", public_id)
        return

    if deal.lead.human_takeover:
        logger.info("[%s] follow_up %s: lead is flagged for human takeover — skipping AI follow-up", session.campaign, public_id)
        return

    # Pull the latest messages before gating on them — otherwise a reply that
    # just arrived on LinkedIn is invisible to _unanswered_count /
    # _too_soon_to_nudge (they read local ChatMessage rows), and the handler
    # re-enqueues on stale state instead of ever seeing the reply. This also
    # makes the admin's "Run now" action actually respond to a fresh reply
    # instead of silently re-enqueuing.
    sync_conversation(session, public_id)

    unanswered = _unanswered_count(deal)
    if unanswered >= 3:
        logger.info("[%s] follow_up %s: hit 3 unanswered limit — marking unresponsive", session.campaign, public_id)
        set_profile_state(session, public_id, ProfileState.COMPLETED.value, outcome="unresponsive")
        return

    if _too_soon_to_nudge(deal, unanswered):
        logger.info("[%s] follow_up %s: too soon to nudge — re-enqueuing", session.campaign, public_id)
        enqueue_follow_up(campaign_id, public_id, account, delay_seconds=24 * 3600)
        return

    materialize_profile_summary_if_missing(deal, session)
    decision = run_follow_up_agent(session, deal)

    profile = _build_send_profile(deal)

    if decision.action == "send_message":
        logger.info("[%s] follow_up message for %s: %s", session.campaign, public_id, decision.message)
        sent = send_raw_message(session, profile, decision.message)
        if not sent:
            set_profile_state(session, public_id, ProfileState.QUALIFIED.value)
            logger.warning("follow_up for %s: send failed — moving to QUALIFIED for re-connection", public_id)
            return
        account.record_action(ActionLog.ActionType.FOLLOW_UP, session.campaign)
        # send_raw_message only drives the browser/API — it never writes a
        # ChatMessage row itself. Without this, the message we just sent is
        # invisible (dashboard counts, chat_summary, _unanswered_count /
        # _too_soon_to_nudge) until the *next* scheduled follow_up for this
        # lead happens to sync, which can be hours to days away.
        sync_conversation(session, public_id)
        enqueue_follow_up(
            campaign_id, public_id, account, delay_seconds=decision.follow_up_hours * 3600,
        )

    elif decision.action == "mark_completed":
        set_profile_state(session, public_id, ProfileState.COMPLETED.value, outcome=decision.outcome)
        logger.info("[%s] follow_up completed for %s: outcome=%s", session.campaign, public_id, decision.outcome)

    elif decision.action == "wait":
        enqueue_follow_up(
            campaign_id, public_id, account, delay_seconds=decision.follow_up_hours * 3600,
        )
