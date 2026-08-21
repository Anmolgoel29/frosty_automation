# linkedin/tasks/scheduler.py
"""Single source of truth for Task row creation.

The daemon's task queue is a reflection of CRM state. Handlers execute
work; this module decides *what task row should exist next* and makes it
so. No other module creates Task rows.

Three layers:

1. **Low-level enqueue** — ``enqueue_connect``, ``enqueue_check_pending``,
   ``enqueue_follow_up``. Insert a PENDING Task row, deduplicating against
   existing PENDING rows with the same key. Called for in-state
   continuations (connect loop, follow-up retries, rate-limit waits).

2. **State-transition hook** — ``on_deal_state_entered(deal)``. Called by
   ``set_profile_state`` after a Deal is saved. Looks at the new state and
   enqueues the appropriate next task, if any. This is what makes the
   pipeline move without handlers calling enqueue themselves.

3. **Reconcile** — ``reconcile(sessions)``. Walks CRM state and ensures the
   Task table reflects it: one connect task per (campaign, account), one
   check_pending per PENDING deal, one follow_up per CONNECTED deal.
   Recovers stale RUNNING tasks. Runs on daemon startup and whenever the
   queue has no ready task — this is the retry mechanism for crashed
   handlers.

Every Task carries the account that must execute it (``Task.linkedin_profile``).
For deal-level tasks that is the deal's ``assigned_profile``; the daemon
routes the task to that account's browser session.
"""
from __future__ import annotations

import datetime
import logging
import random
from datetime import timedelta

from django.utils import timezone

from linkedin.conf import CAMPAIGN_CONFIG
from linkedin.enums import ProfileState
from linkedin.models import Task

logger = logging.getLogger(__name__)


# ── Low-level enqueue ─────────────────────────────────────────────────


def _insert_task(
    task_type: "Task.TaskType",
    payload: dict,
    delay_seconds: float,
    profile,
    dedup_keys: list[str] | None = None,
) -> bool:
    """Insert a PENDING Task row, skipping if a duplicate already exists.

    Duplicate = same ``task_type``, same executing account, status=PENDING,
    and matching payload on ``dedup_keys`` (defaults to all payload keys).
    Returns True if a row was inserted.
    """
    filter_kwargs = {
        "task_type": task_type,
        "status": Task.Status.PENDING,
        "linkedin_profile": profile,
    }
    for key in (dedup_keys if dedup_keys is not None else payload):
        filter_kwargs[f"payload__{key}"] = payload[key]

    if Task.objects.filter(**filter_kwargs).exists():
        return False

    Task.objects.create(
        task_type=task_type,
        linkedin_profile=profile,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
        payload=payload,
    )
    return True


def enqueue_connect(campaign_id: int, profile, delay_seconds: float = 10) -> None:
    """Enqueue a connect task for one account working the given campaign.

    Every account runs its own connect loop, so a campaign has as many
    in-flight connect tasks as it has accounts.
    """
    _insert_task(
        task_type=Task.TaskType.CONNECT,
        payload={"campaign_id": campaign_id},
        delay_seconds=delay_seconds,
        profile=profile,
    )


def enqueue_check_pending(
    campaign_id: int,
    public_id: str,
    backoff_hours: float,
    profile,
) -> float:
    """Enqueue a check_pending task with equal-jitter backoff.

    Delay is uniform over ``[backoff_hours/2, backoff_hours]``. Returns
    the chosen delay in hours (for logging).
    """
    half = backoff_hours / 2
    delay_hours = half + random.uniform(0, half)

    _insert_task(
        task_type=Task.TaskType.CHECK_PENDING,
        payload={
            "campaign_id": campaign_id,
            "public_id": public_id,
            "backoff_hours": backoff_hours,
        },
        delay_seconds=delay_hours * 3600,
        profile=profile,
        dedup_keys=["campaign_id", "public_id"],
    )
    return delay_hours


def enqueue_follow_up(
    campaign_id: int,
    public_id: str,
    profile,
    delay_seconds: float = 10,
) -> None:
    """Enqueue a follow-up task for a CONNECTED profile."""
    _insert_task(
        task_type=Task.TaskType.FOLLOW_UP,
        payload={"campaign_id": campaign_id, "public_id": public_id},
        delay_seconds=delay_seconds,
        profile=profile,
        dedup_keys=["campaign_id", "public_id"],
    )


# ── Delay helpers ─────────────────────────────────────────────────────


def seconds_until_tomorrow() -> float:
    """Seconds until 00:00 local time — used for daily rate-limit waits."""
    now = timezone.now()
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return (tomorrow - now).total_seconds()


# ── State-transition hook ─────────────────────────────────────────────


def on_deal_state_entered(deal) -> None:
    """Enqueue the task implied by the Deal's current state, if any.

    Called by ``set_profile_state`` after the Deal row is saved. Idempotent:
    relies on enqueue dedup so repeated calls produce at most one pending
    task per (campaign, public_id).

    The task is routed to the deal's owning account — the one that sent the
    invite. It is the only account that can see the reply.
    """
    state = ProfileState(deal.state)
    campaign_id = deal.campaign_id
    public_id = deal.lead.public_identifier

    if not public_id:
        return

    if state not in (ProfileState.PENDING, ProfileState.CONNECTED):
        # QUALIFIED, READY_TO_CONNECT, COMPLETED and FAILED have no implied
        # deal-level task — handled by the connect loop, or terminal.
        return

    profile = deal.assigned_profile
    if profile is None:
        logger.warning(
            "Deal %s is %s but has no owning account — no task enqueued. "
            "Assign one in Admin → Deals to resume it.",
            public_id, state,
        )
        return

    if state == ProfileState.PENDING:
        backoff = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
        enqueue_check_pending(campaign_id, public_id, backoff_hours=backoff, profile=profile)
    else:
        enqueue_follow_up(campaign_id, public_id, profile)


# ── Reconciliation ────────────────────────────────────────────────────


def _recover_stale_running_tasks() -> int:
    """Reset RUNNING tasks to PENDING. RUNNING rows can only linger if the
    daemon crashed mid-task, so they are always stale at reconcile time."""
    count = Task.objects.filter(status=Task.Status.RUNNING).update(
        status=Task.Status.PENDING,
    )
    if count:
        logger.info("Recovered %d stale running tasks", count)
    return count


def campaigns_in(sessions: dict) -> list:
    """Every campaign covered by the running accounts, deduplicated."""
    by_pk = {}
    for session in sessions.values():
        for campaign in session.campaigns:
            by_pk.setdefault(campaign.pk, campaign)
    return [by_pk[pk] for pk in sorted(by_pk)]


def _seed_connect_tasks(campaign, sessions: dict) -> None:
    """Give every running account on this campaign its own connect task."""
    for profile in campaign.active_profiles():
        if profile.pk in sessions:
            enqueue_connect(campaign.pk, profile, delay_seconds=0)


def _seed_deal_tasks(campaign, sessions: dict) -> None:
    """Ensure every active Deal has the task its state implies.

    Iterates PENDING and CONNECTED deals, letting ``on_deal_state_entered``
    decide what to enqueue (with dedup).

    Deals whose lead is flagged for human takeover are excluded from
    follow_up task seeding — the human is managing that conversation and
    no automated tasks should be re-created for it.

    A deal whose owning account isn't running is left alone rather than
    handed to another account: the pending invite and the message thread
    only exist on the account that sent them.
    """
    from crm.models import Deal

    active_states = (ProfileState.PENDING, ProfileState.CONNECTED)
    stranded = 0
    deals = Deal.objects.filter(
        state__in=active_states,
        campaign=campaign,
        lead__human_takeover=False,
    ).select_related("lead", "assigned_profile")
    for deal in deals:
        # Skip human-takeover leads entirely — reconcile must not
        # re-create follow_up tasks for conversations a human owns.
        if deal.state == ProfileState.CONNECTED and deal.lead.human_takeover:
            continue
        if deal.assigned_profile_id not in sessions:
            stranded += 1
            continue
        on_deal_state_entered(deal)

    if stranded:
        logger.warning(
            "[%s] %d in-flight deal(s) belong to an account that isn't running — "
            "re-activate it to resume them",
            campaign, stranded,
        )


def reconcile(sessions: dict) -> None:
    """Reconcile the Task queue with CRM state.

    ``sessions`` maps LinkedInProfile pk → AccountSession: the accounts this
    daemon can actually drive. Runs on daemon startup and when the queue
    drains. This is the safety net that re-creates tasks for deals whose
    handlers crashed (leaving a FAILED task with no successor), and the
    point where leads left un-owned in the ready pool get dealt out.
    """
    from linkedin.pipeline.allocation import allocate_ready_deals, reclaim_unreachable_deals

    _recover_stale_running_tasks()
    for campaign in campaigns_in(sessions):
        reclaim_unreachable_deals(campaign)
        allocate_ready_deals(campaign)
        _seed_connect_tasks(campaign, sessions)
        _seed_deal_tasks(campaign, sessions)

    pending_count = Task.objects.pending().count()
    logger.info("Task queue reconciled: %d pending tasks", pending_count)
