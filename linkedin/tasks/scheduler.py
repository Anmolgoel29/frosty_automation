# linkedin/tasks/scheduler.py
"""Single source of truth for Task row creation.

The daemon's task queue is a reflection of CRM state. Handlers execute
work; this module decides *what task row should exist next* and makes it
so. No other module creates Task rows.

Three layers:

1. **Low-level enqueue** — ``enqueue_connect``, ``enqueue_check_pending``,
   ``enqueue_follow_up``, ``enqueue_check_inbox``, ``enqueue_manual_send``.
   Insert a PENDING Task row, deduplicating against existing PENDING rows
   with the same key. Called for in-state continuations (connect loop,
   follow-up retries, rate-limit waits, the inbox poll's self-reschedule).

   One narrow, documented exception: the *first* ``manual_send`` row for a
   given admin-panel send is inserted directly by ``webadmin`` (which has no
   Django ORM/Playwright access by design — it writes to the same
   ``linkedin_task`` table via its own SQLAlchemy mirror, the same pattern
   ``TaskAdmin.run_now`` already uses to reschedule an existing task).
   ``enqueue_manual_send`` here is only used for *retries* past that first
   row (e.g. a rate-limit wait) — everything downstream of the initial
   webadmin insert still funnels through this module's dedup machinery.

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
import threading
from datetime import timedelta

from django.utils import timezone

from linkedin.conf import CAMPAIGN_CONFIG
from linkedin.enums import ProfileState
from linkedin.models import Task

logger = logging.getLogger(__name__)

# Task creation is check-then-insert, and with one worker thread per account
# there are several threads doing it at once (plus the supervisor running
# reconcile). Serialising the whole read-decide-write here is what keeps the
# dedup honest — without it two threads can both miss an existing row and
# create a duplicate follow_up, which means messaging the same lead twice.
# Re-entrant because reconcile holds it across its own enqueue calls.
_TASK_LOCK = threading.RLock()


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

    with _TASK_LOCK:
        if Task.objects.filter(**filter_kwargs).exists():
            return False

        Task.objects.create(
            task_type=task_type,
            linkedin_profile=profile,
            scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
            payload=payload,
        )
    return True


def has_live_task(task_type: "Task.TaskType", profile, **payload_filters) -> bool:
    """True if such a task is already pending *or* mid-execution.

    Only reconcile needs this. Handlers self-reschedule while their own task
    is RUNNING, so the enqueue dedup above deliberately ignores RUNNING rows;
    reconcile must not, or it would queue a second connect task on top of the
    one an account is running right now and short-circuit its pacing.
    """
    filter_kwargs = {
        "task_type": task_type,
        "status__in": (Task.Status.PENDING, Task.Status.RUNNING),
        "linkedin_profile": profile,
    }
    for key, value in payload_filters.items():
        filter_kwargs[f"payload__{key}"] = value
    return Task.objects.filter(**filter_kwargs).exists()


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


def enqueue_check_inbox(campaign_id: int, profile, delay_seconds: float) -> None:
    """Enqueue the next inbox-poll task for one account working the campaign.

    Account+campaign scoped like enqueue_connect (no public_id) — one
    check_inbox loop per account covers every CONNECTED lead it owns in
    a single Voyager call.
    """
    _insert_task(
        task_type=Task.TaskType.CHECK_INBOX,
        payload={"campaign_id": campaign_id},
        delay_seconds=delay_seconds,
        profile=profile,
    )


def enqueue_manual_send(lead_id: int, message: str, profile, delay_seconds: float = 3600) -> None:
    """Retry path for a manual send that hit the account's rate limit.

    The first row for a given send is inserted directly by webadmin (see the
    module docstring's exception note) — this is only reached from
    handle_manual_send when it needs to reschedule itself.
    """
    _insert_task(
        task_type=Task.TaskType.MANUAL_SEND,
        payload={"lead_id": lead_id, "message": message},
        delay_seconds=delay_seconds,
        profile=profile,
        dedup_keys=["lead_id", "message"],
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
        # Fires the moment a connect/check_pending task observes an accepted
        # invite — no delay, so the account's next claim (top-priority,
        # tied with check_pending) sends the first message right away
        # instead of waiting out the generic enqueue_follow_up default.
        enqueue_follow_up(campaign_id, public_id, profile, delay_seconds=0)


# ── Reconciliation ────────────────────────────────────────────────────


def recover_stale_running_tasks() -> int:
    """Reset leftover RUNNING tasks to PENDING. **Startup only.**

    A RUNNING row means "a worker thread is executing this right now", so this
    must not run while workers are alive — it would hand a live task back to
    the queue and get it executed twice. At daemon startup no worker exists
    yet, so any RUNNING row is by definition an orphan from a crashed process.
    """
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


def _live_task_keys(campaign_id: int) -> set[tuple]:
    """Every pending-or-running task for this campaign, as dedup keys.

    Reconcile runs on a timer forever, so asking the DB "does a task exist?"
    once per deal is the single largest source of query volume in the daemon
    (it grew linearly with the active pipeline). One query up front answers
    all of those lookups in memory instead.
    """
    rows = Task.objects.filter(
        status__in=(Task.Status.PENDING, Task.Status.RUNNING),
        payload__campaign_id=campaign_id,
    ).values_list("task_type", "linkedin_profile_id", "payload")
    return {
        (task_type, profile_id, (payload or {}).get("public_id", ""))
        for task_type, profile_id, payload in rows
    }


def _seed_connect_tasks(campaign, sessions: dict, live_keys: set[tuple], new_tasks: list) -> None:
    """Give every running account on this campaign its own connect task.

    An account whose connect task is pending or mid-execution is skipped — it
    will reschedule itself when it finishes. Appends to *new_tasks* for one
    bulk insert at the end of the reconcile pass.
    """
    for profile in campaign.active_profiles():
        if profile.pk not in sessions:
            continue
        key = (Task.TaskType.CONNECT, profile.pk, "")
        if key in live_keys:
            continue
        live_keys.add(key)
        new_tasks.append(Task(
            task_type=Task.TaskType.CONNECT,
            linkedin_profile=profile,
            scheduled_at=timezone.now(),
            payload={"campaign_id": campaign.pk},
        ))


def _seed_check_inbox_tasks(campaign, sessions: dict, live_keys: set[tuple], new_tasks: list) -> None:
    """Give every running account on this campaign its own inbox-poll task.

    Structural copy of _seed_connect_tasks — safety net for an account that
    has no check_inbox task at all (freshly added to a campaign, or its
    previous handler crashed leaving no successor).
    """
    for profile in campaign.active_profiles():
        if profile.pk not in sessions:
            continue
        key = (Task.TaskType.CHECK_INBOX, profile.pk, "")
        if key in live_keys:
            continue
        live_keys.add(key)
        new_tasks.append(Task(
            task_type=Task.TaskType.CHECK_INBOX,
            linkedin_profile=profile,
            scheduled_at=timezone.now(),
            payload={"campaign_id": campaign.pk},
        ))


def _seed_deal_tasks(campaign, sessions: dict, live_keys: set[tuple], new_tasks: list) -> None:
    """Ensure every active Deal has the task its state implies.

    Deals whose lead is flagged for human takeover are excluded from
    follow_up task seeding — the human is managing that conversation and
    no automated tasks should be re-created for it.

    A deal whose owning account isn't running is left alone rather than
    handed to another account: the pending invite and the message thread
    only exist on the account that sent them.

    Membership is tested against *live_keys* (one query for the whole
    campaign) and rows are appended to *new_tasks* for a single bulk insert,
    rather than two queries per deal.
    """
    from crm.models import Deal

    active_states = (ProfileState.PENDING, ProfileState.CONNECTED)
    stranded = 0
    deals = (
        Deal.objects.filter(
            state__in=active_states,
            campaign=campaign,
            lead__human_takeover=False,
        )
        .select_related("lead", "assigned_profile")
        .only(
            "state", "backoff_hours", "assigned_profile",
            "lead__public_identifier", "lead__human_takeover",
        )
    )
    for deal in deals:
        public_id = deal.lead.public_identifier
        if not public_id:
            continue
        if deal.assigned_profile_id not in sessions:
            stranded += 1
            continue

        if deal.state == ProfileState.PENDING:
            task_type = Task.TaskType.CHECK_PENDING
            backoff = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
            half = backoff / 2
            delay_seconds = (half + random.uniform(0, half)) * 3600
            payload = {
                "campaign_id": campaign.pk,
                "public_id": public_id,
                "backoff_hours": backoff,
            }
        else:
            task_type = Task.TaskType.FOLLOW_UP
            delay_seconds = 10
            payload = {"campaign_id": campaign.pk, "public_id": public_id}

        key = (task_type, deal.assigned_profile_id, public_id)
        if key in live_keys:
            continue
        live_keys.add(key)
        new_tasks.append(Task(
            task_type=task_type,
            linkedin_profile=deal.assigned_profile,
            scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
            payload=payload,
        ))

    if stranded:
        logger.warning(
            "[%s] %d in-flight deal(s) belong to an account that isn't running — "
            "re-activate it to resume them",
            campaign, stranded,
        )


def reconcile(sessions: dict) -> None:
    """Reconcile the Task queue with CRM state.

    ``sessions`` maps LinkedInProfile pk → AccountSession: the accounts this
    daemon can actually drive. Runs on a timer in the daemon's supervisor
    thread — the single writer for reconciliation, while worker threads only
    enqueue their own follow-on tasks. This is the safety net that re-creates
    tasks for deals whose handlers crashed (leaving a FAILED task with no
    successor), and the point where leads left un-owned in the ready pool get
    dealt out.
    """
    from linkedin.pipeline.allocation import allocate_ready_deals, reclaim_unreachable_deals

    created = 0
    with _TASK_LOCK:
        for campaign in campaigns_in(sessions):
            reclaim_unreachable_deals(campaign)
            allocate_ready_deals(campaign)

            # One read of the live queue, one write of everything missing —
            # this runs every minute forever, so per-deal queries here are
            # what made the daemon's DB volume grow with the pipeline.
            live_keys = _live_task_keys(campaign.pk)
            new_tasks: list[Task] = []
            _seed_connect_tasks(campaign, sessions, live_keys, new_tasks)
            _seed_check_inbox_tasks(campaign, sessions, live_keys, new_tasks)
            _seed_deal_tasks(campaign, sessions, live_keys, new_tasks)
            if new_tasks:
                Task.objects.bulk_create(new_tasks)
                created += len(new_tasks)

    if created:
        logger.info("Task queue reconciled: %d task(s) created", created)
