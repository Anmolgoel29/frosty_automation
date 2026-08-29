# linkedin/tasks/manual_send.py
"""Manual-send task — delivers an admin-composed message from the webadmin chat view.

The first Task row for a given send is inserted directly by webadmin (see
linkedin/tasks/scheduler.py's module docstring) since that process has no
Django ORM/Playwright access; this handler is what actually drives the
account's browser to send it, exactly as if the AI had composed it — same
rate limit, same send primitive, same post-send sync.
"""
from __future__ import annotations

import logging

from termcolor import colored

from linkedin.models import ActionLog

logger = logging.getLogger(__name__)


def handle_manual_send(task, session):
    from crm.models import Lead
    from linkedin.actions.message import send_raw_message
    from linkedin.db.chat import sync_conversation, tag_last_outgoing
    from linkedin.tasks.scheduler import enqueue_manual_send

    payload = task.payload
    lead_id = payload["lead_id"]
    message = payload["message"]
    account = session.linkedin_profile

    logger.info(
        "[%s] %s lead #%s as %s",
        session.campaign, colored("▶ manual_send", "yellow", attrs=["bold"]),
        lead_id, account.linkedin_username,
    )

    # Manual sends share the AI's daily cap: LinkedIn's spam/ban detection
    # watches the account's total outbound volume and pattern, not who
    # composed each message — a human clicking Send repeatedly through the
    # admin panel looks identical to the AI doing it.
    if not account.can_execute(ActionLog.ActionType.FOLLOW_UP):
        enqueue_manual_send(lead_id, message, account, delay_seconds=3600)
        return

    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None or not lead.public_identifier:
        logger.warning("manual_send: no Lead #%s — dropping message", lead_id)
        return

    public_id = lead.public_identifier

    # Pull anything new before sending — same rationale as handle_follow_up:
    # a reply that just arrived on LinkedIn shouldn't be invisible to
    # whoever queued this from the admin panel.
    sync_conversation(session, public_id)

    profile = {"public_identifier": public_id, "urn": lead.urn or ""}
    sent = send_raw_message(session, profile, message)
    if not sent:
        # A human composed this message — silently auto-retrying it later,
        # possibly stale, would be wrong. Leave the task COMPLETED and let
        # the admin see it didn't go out (surfacing failed sends in the UI
        # is a natural follow-up, out of scope here).
        logger.warning("manual_send: send failed for %s", public_id)
        return

    account.record_action(ActionLog.ActionType.FOLLOW_UP, session.campaign)

    # webadmin already sets this eagerly at the moment the admin clicked
    # Send, closing the race with an already-due follow_up task. This is a
    # belt-and-suspenders re-assertion so the invariant holds even if a
    # manual_send Task is ever created through a different path.
    if not lead.human_takeover:
        lead.human_takeover = True
        lead.save(update_fields=["human_takeover"])

    # send_raw_message doesn't return the created message's linkedin_urn —
    # re-sync to pull it back with a correct dedup key so it renders in the
    # admin thread view.
    sync_conversation(session, public_id)
    tag_last_outgoing(public_id, "human")
