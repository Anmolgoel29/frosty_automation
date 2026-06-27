# linkedin/daemon.py
from __future__ import annotations

import logging
import random
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from pydantic_ai.exceptions import ModelHTTPError

from termcolor import colored

from linkedin.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ACTIVE_TIMEZONE,
    CAMPAIGN_CONFIG,
    ENABLE_ACTIVE_HOURS,
    REST_DAYS,
)
from linkedin.diagnostics import failure_diagnostics
from linkedin.exceptions import AuthenticationError
from linkedin.ml.qualifier import BayesianQualifier
from linkedin.models import Task
from linkedin.tasks.check_pending import handle_check_pending
from linkedin.tasks.connect import handle_connect
from linkedin.tasks.follow_up import handle_follow_up

logger = logging.getLogger(__name__)

_HANDLERS = {
    Task.TaskType.CONNECT: handle_connect,
    Task.TaskType.CHECK_PENDING: handle_check_pending,
    Task.TaskType.FOLLOW_UP: handle_follow_up,
}

HEARTBEAT_INTERVAL = 300  # 5 minutes
HEARTBEAT_SLICE = 60      # wake every minute during long sleeps


# ── Heartbeat ────────────────────────────────────────────────────────


class Heartbeat:
    """Logs an ``alive — <context>`` line at most once every *interval* seconds.

    The first call won't log (``_last`` starts at now) — quiet gaps begin
    counting from daemon start, not the Unix epoch.
    """

    def __init__(self, interval: float = HEARTBEAT_INTERVAL):
        self._interval = interval
        self._last = time.monotonic()

    def maybe_log(self, context: str) -> None:
        now = time.monotonic()
        if now - self._last < self._interval:
            return
        self._last = now
        logger.info(colored("alive", "cyan") + " — %s", context)


def sleep_with_heartbeat(seconds: float, heartbeat: Heartbeat, context: str) -> None:
    """``time.sleep(seconds)`` that wakes every ``HEARTBEAT_SLICE`` seconds to
    let *heartbeat* fire. Use for any idle sleep longer than the heartbeat
    interval so the daemon never goes silent for more than 5 minutes.
    """
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(HEARTBEAT_SLICE, remaining))
        heartbeat.maybe_log(context)


# ── Human-rhythm pacing ──────────────────────────────────────────────


class _HumanRhythmBreak:
    """Wall-clock burst timer that injects a random break between bursts.

    Call ``reset()`` after idle sleeps (active-hours pause, waiting for
    the next scheduled task) so the burst timer tracks real work, not
    wall-clock. Call ``maybe_break()`` after each successful task —
    it sleeps a random break duration when the current burst is done.
    """

    def __init__(self, heartbeat: Heartbeat):
        self._heartbeat = heartbeat
        self._new_burst()

    def _new_burst(self):
        self._burst_start = time.monotonic()
        self._burst_duration = random.uniform(
            CAMPAIGN_CONFIG["burst_min_seconds"],
            CAMPAIGN_CONFIG["burst_max_seconds"],
        )

    def reset(self):
        """Start a fresh burst without taking a break. Use after idle gaps."""
        self._new_burst()

    def maybe_break(self):
        """Sleep a random break and start a new burst if the current one is done."""
        if time.monotonic() - self._burst_start < self._burst_duration:
            return
        break_seconds = random.uniform(
            CAMPAIGN_CONFIG["break_min_seconds"],
            CAMPAIGN_CONFIG["break_max_seconds"],
        )
        logger.info("Taking a %dm break", int(break_seconds // 60))
        sleep_with_heartbeat(
            break_seconds,
            self._heartbeat,
            f"on break, {int(break_seconds // 60)}m total",
        )
        self._new_burst()


def _build_qualifiers(campaigns, cfg):
    """Create a qualifier for every campaign, keyed by campaign PK."""
    from crm.models import Lead

    qualifiers: dict[int, BayesianQualifier] = {}
    for campaign in campaigns:
        q = BayesianQualifier(
            seed=42,
            n_mc_samples=cfg["qualification_n_mc_samples"],
            campaign=campaign,
        )
        X, y = Lead.get_labeled_arrays(campaign)
        if len(X) > 0:
            q.warm_start(X, y)
            logger.info(
                colored("GP qualifier warm-started", "cyan")
                + " on %d labelled samples (%d positive, %d negative)"
                + " for campaign %s",
                len(y), int((y == 1).sum()), int((y == 0).sum()), campaign,
            )
        qualifiers[campaign.pk] = q

    return qualifiers


# ------------------------------------------------------------------
# Active-hours schedule guard
# ------------------------------------------------------------------


def seconds_until_active() -> float:
    """Return seconds to wait before the next active window, or 0 if active now."""
    if not ENABLE_ACTIVE_HOURS:
        return 0.0
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)

    if now.weekday() not in REST_DAYS and ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR:
        return 0.0

    # Find the next active start: try today first, then subsequent days
    candidate = timezone.make_aware(
        now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0, tzinfo=None),
        timezone=tz,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() in REST_DAYS:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


# ------------------------------------------------------------------
# Task queue worker
# ------------------------------------------------------------------


def run_daemon(session):
    from linkedin.models import Campaign

    cfg = CAMPAIGN_CONFIG

    qualifiers = _build_qualifiers(session.campaigns, cfg)

    campaigns = session.campaigns
    if not campaigns:
        logger.error("No campaigns found — cannot start daemon")
        return

    logger.info(
        colored("Daemon started", "green", attrs=["bold"])
        + " — %d campaigns, task queue worker",
        len(campaigns),
    )

    heartbeat = Heartbeat()
    rhythm = _HumanRhythmBreak(heartbeat)

    # Single-threaded: one task at a time, no concurrent enqueuing,
    # so sleeping until the next scheduled_at is safe.
    while True:
        pause = seconds_until_active()
        if pause > 0:
            h, m = int(pause // 3600), int(pause % 3600 // 60)
            logger.info("Outside active hours — sleeping %dh%02dm", h, m)
            sleep_with_heartbeat(
                pause, heartbeat, f"outside active hours, {h}h{m:02d}m left",
            )
            rhythm.reset()
            continue

        task = Task.objects.claim_next()
        if task is None:
            # Nothing ready — reconcile the queue from CRM state. Any deal
            # stuck without a pending task (e.g. because a prior handler
            # crashed) gets a fresh task here; this is the retry mechanism.
            from linkedin.tasks.scheduler import reconcile
            reconcile(session)

            wait = Task.objects.seconds_to_next()
            if wait is None:
                logger.info("Queue empty after reconcile — sleeping 1h")
                sleep_with_heartbeat(3600, heartbeat, "queue empty")
                rhythm.reset()
                continue
            if wait > 0:
                h, m = int(wait // 3600), int(wait % 3600 // 60)
                logger.info("Next task in %dh%02dm — sleeping", h, m)
                sleep_with_heartbeat(
                    wait, heartbeat, f"next task in {h}h{m:02d}m",
                )
                rhythm.reset()
            continue

        campaign = Campaign.objects.filter(pk=task.payload.get("campaign_id")).first()
        if not campaign:
            logger.error("Campaign %s not found", task.payload.get("campaign_id"))
            task.mark_failed()
            continue

        session.campaign = campaign
        task.mark_running()

        handler = _HANDLERS.get(task.task_type)
        if handler is None:
            logger.error("Unknown task type: %s", task.task_type)
            task.mark_failed()
            continue

        try:
            with failure_diagnostics(session):
                handler(task, session, qualifiers)
        except AuthenticationError:
            logger.warning("Session expired during %s — re-authenticating", task)
            try:
                session.reauthenticate()
            except Exception:
                logger.exception("Re-authentication failed for %s", task)
            # Either way, mark this task FAILED; reconcile will re-create a
            # fresh task for the deal on the next idle cycle.
            task.mark_failed()
            continue
        except ModelHTTPError as e:
            task.mark_failed()
            logger.error(
                colored("Daemon stopped — LLM API error", "red", attrs=["bold"])
                + "\n%s\nCheck llm_provider, ai_model, llm_api_key, and llm_api_base in Admin → Site Configuration.", e,
            )
            return
        except Exception:
            task.mark_failed()
            logger.exception("Task %s failed", task)
            continue

        task.mark_completed()
        rhythm.maybe_break()
