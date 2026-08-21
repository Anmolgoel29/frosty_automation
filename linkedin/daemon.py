# linkedin/daemon.py
from __future__ import annotations

import logging
import random
import threading
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
QUEUE_POLL_INTERVAL = 60  # re-check the queue at least this often while waiting


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


def sync_sessions(sessions: dict) -> None:
    """Refresh the account roster in place, from the DB.

    Accounts are added and deactivated in the admin panel while the daemon is
    running, so the roster is re-read on every idle cycle rather than frozen
    at startup: a LinkedIn account added there starts working within a minute
    instead of on the next restart. Mutates ``sessions`` so the daemon loop
    and everything holding the same dict see the change.
    """
    from linkedin.browser.registry import get_active_profiles, get_or_create_session

    live = {}
    for profile in get_active_profiles():
        session = get_or_create_session(profile)
        # Re-bind the row (limits, active flag) and drop the cached campaign
        # list — membership may have changed since this session was built.
        # `_exhausted` records "LinkedIn itself cut this account off today",
        # which no column reflects, so it has to survive the swap or the
        # account would retry every time the roster is re-read.
        previous = session.linkedin_profile
        if previous is not profile:
            profile._exhausted = previous._exhausted
        session.linkedin_profile = profile
        session.__dict__.pop("campaigns", None)
        if not session.campaigns:
            continue
        if session.campaign is None or session.campaign not in session.campaigns:
            session.campaign = next(
                (c for c in session.campaigns if not c.is_freemium), None,
            ) or session.campaigns[0]
        live[profile.pk] = session

    for pk in sorted(set(live) - set(sessions)):
        logger.info(
            colored("Account added", "green") + " — %s joins the rotation",
            live[pk].linkedin_profile.linkedin_username,
        )
    for pk in sorted(set(sessions) - set(live)):
        logger.info(
            colored("Account removed", "yellow") + " — %s leaves the rotation",
            sessions[pk].linkedin_profile.linkedin_username,
        )
        # Its browser is closed by its own worker thread on shutdown — the
        # Playwright sync API cannot be driven from this thread.

    sessions.clear()
    sessions.update(live)


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
# Multi-account task queue: one worker thread per LinkedIn account
# ------------------------------------------------------------------


class _StartSpacer:
    """Keeps two accounts from firing their first request at the same instant.

    The workers genuinely run in parallel — a search takes a minute or two and
    the accounts overlap for most of it, which is the point. What this avoids
    is the sharper signal: two accounts on one IP starting an action in the
    same second, over and over. Each worker waits out a short jittered gap
    since the last task *start* before beginning its own.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_start = 0.0

    def wait_turn(self, stop: threading.Event) -> None:
        with self._lock:
            gap = random.uniform(
                CAMPAIGN_CONFIG["account_stagger_min_seconds"],
                CAMPAIGN_CONFIG["account_stagger_max_seconds"],
            )
            wait = self._last_start + gap - time.monotonic()
            if wait > 0:
                stop.wait(wait)
            self._last_start = time.monotonic()


class _AccountWorker(threading.Thread):
    """Runs one LinkedIn account's queue on its own browser.

    Everything Playwright touches — launching the browser, driving it, closing
    it — happens on this thread, because the sync API is thread-affine. The
    supervisor never reaches into a session; it asks the worker to stop and
    the worker tears its own browser down.
    """

    def __init__(self, session, qualifiers, spacer, stop_all):
        profile = session.linkedin_profile
        super().__init__(name=profile.linkedin_username.split("@")[0], daemon=True)
        self.session = session
        self.profile_id = profile.pk
        self._qualifiers = qualifiers
        self._spacer = spacer
        self._stop_all = stop_all
        self._stop_me = threading.Event()

    def stop(self):
        """Ask the worker to finish its current task and shut down."""
        self._stop_me.set()

    @property
    def _stopping(self) -> bool:
        return self._stop_me.is_set() or self._stop_all.is_set()

    def run(self):
        from django.db import connection

        heartbeat = Heartbeat()
        rhythm = _HumanRhythmBreak(heartbeat)
        logger.info(colored("worker started", "green"))

        try:
            while not self._stopping:
                if self._wait_for_active_hours(heartbeat, rhythm):
                    continue

                task = Task.objects.claim_for(self.profile_id)
                if task is None:
                    self._wait_for_work(heartbeat, rhythm)
                    continue

                self._spacer.wait_turn(self._stop_me)
                if self._stopping:
                    # Put it back rather than running it on the way out.
                    task.mark_pending()
                    break

                if self._run_task(task):
                    rhythm.maybe_break()
        except Exception:
            logger.exception("worker crashed")
        finally:
            self.session.close()
            connection.close()
            logger.info(colored("worker stopped", "yellow"))

    # -- loop steps ----------------------------------------------------

    def _wait_for_active_hours(self, heartbeat, rhythm) -> bool:
        pause = seconds_until_active()
        if pause <= 0:
            return False
        h, m = int(pause // 3600), int(pause % 3600 // 60)
        logger.info("Outside active hours — sleeping %dh%02dm", h, m)
        self._interruptible_sleep(pause, heartbeat, f"outside active hours, {h}h{m:02d}m left")
        rhythm.reset()
        return True

    def _wait_for_work(self, heartbeat, rhythm) -> None:
        """Idle until this account's next task is due.

        Polls in short slices rather than sleeping the whole delay, so a task
        rescheduled to run sooner — the admin's "Run now" — starts within
        QUEUE_POLL_INTERVAL. Reconciliation is the supervisor's job; a worker
        with nothing to do simply waits.
        """
        wait = Task.objects.seconds_to_next(self.profile_id)
        if wait is None:
            self._interruptible_sleep(QUEUE_POLL_INTERVAL, heartbeat, "no tasks queued")
            rhythm.reset()
            return
        if wait > 0:
            h, m = int(wait // 3600), int(wait % 3600 // 60)
            if wait > QUEUE_POLL_INTERVAL:
                logger.info("Next task in %dh%02dm", h, m)
            self._interruptible_sleep(
                min(wait, QUEUE_POLL_INTERVAL), heartbeat, f"next task in {h}h{m:02d}m",
            )
            rhythm.reset()

    def _run_task(self, task) -> bool:
        """Execute one task. Returns True when it completed cleanly."""
        from linkedin.models import Campaign

        campaign = Campaign.objects.filter(pk=task.payload.get("campaign_id")).first()
        if not campaign:
            logger.error("Campaign %s not found", task.payload.get("campaign_id"))
            task.mark_failed()
            return False

        self.session.campaign = campaign

        handler = _HANDLERS.get(task.task_type)
        if handler is None:
            logger.error("Unknown task type: %s", task.task_type)
            task.mark_failed()
            return False

        qualifier_for = self._qualifiers
        try:
            with failure_diagnostics(self.session):
                handler(task, self.session, qualifier_for)
        except AuthenticationError:
            logger.warning("Session expired during %s — re-authenticating", task)
            try:
                self.session.reauthenticate()
            except Exception:
                logger.exception("Re-authentication failed for %s", task)
            # Either way, mark this task FAILED; reconcile will re-create a
            # fresh task for the deal on the next supervisor cycle.
            task.mark_failed()
            return False
        except ModelHTTPError as e:
            task.mark_failed()
            logger.error(
                colored("Daemon stopping — LLM API error", "red", attrs=["bold"])
                + "\n%s\nCheck chat_/task_ llm_provider, ai_model, llm_api_key, and llm_api_base "
                "in Admin → Site Configuration.", e,
            )
            # A bad LLM config breaks every account, not just this one.
            self._stop_all.set()
            return False
        except Exception:
            task.mark_failed()
            logger.exception("Task %s failed", task)
            return False

        task.mark_completed()
        return True

    def _interruptible_sleep(self, seconds: float, heartbeat: Heartbeat, context: str) -> None:
        end = time.monotonic() + seconds
        while not self._stopping:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self._stop_me.wait(min(HEARTBEAT_SLICE, remaining))
            heartbeat.maybe_log(context)


def run_daemon(sessions: dict):
    """Run every LinkedIn account in parallel, one worker thread each.

    ``sessions`` maps LinkedInProfile pk → AccountSession. Each account gets a
    thread with its own browser and its own slice of the queue (tasks carry
    the account that must execute them), so the accounts search and connect
    at the same time instead of taking turns.

    This thread is the supervisor: it owns reconciliation — the single writer,
    so workers never race each other re-creating the same task — re-reads the
    account roster so accounts added in the admin start working without a
    restart, and keeps a worker alive for every account.
    """
    from linkedin.tasks.scheduler import campaigns_in, recover_stale_running_tasks

    cfg = CAMPAIGN_CONFIG

    campaigns = campaigns_in(sessions)
    if not campaigns:
        logger.error("No campaigns found — cannot start daemon")
        return

    qualifiers = _build_qualifiers(campaigns, cfg)

    # Safe here and nowhere else: no worker exists yet, so any RUNNING row is
    # an orphan from a previous process rather than live work.
    recover_stale_running_tasks()

    logger.info(
        colored("Daemon started", "green", attrs=["bold"])
        + " — %d campaigns, %d LinkedIn account(s) in parallel: %s",
        len(campaigns), len(sessions),
        ", ".join(s.linkedin_profile.linkedin_username for s in sessions.values()),
    )

    stop_all = threading.Event()
    spacer = _StartSpacer()
    workers: dict[int, _AccountWorker] = {}

    try:
        while not stop_all.is_set():
            sync_sessions(sessions)
            if not sessions:
                logger.error("No usable LinkedIn accounts — waiting")
            else:
                # A newly-attached campaign needs its own GP model; existing
                # qualifiers are left alone so their in-memory state survives.
                for campaign in campaigns_in(sessions):
                    if campaign.pk not in qualifiers:
                        qualifiers.update(_build_qualifiers([campaign], cfg))

                _sync_workers(workers, sessions, qualifiers, spacer, stop_all)

                from linkedin.tasks.scheduler import reconcile
                reconcile(sessions)

            stop_all.wait(cfg["supervisor_interval_seconds"])
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
    finally:
        stop_all.set()
        for worker in workers.values():
            worker.stop()
        for worker in workers.values():
            worker.join(timeout=60)


def _sync_workers(workers: dict, sessions: dict, qualifiers, spacer, stop_all) -> None:
    """Keep exactly one live worker per account in the roster."""
    for pk in list(workers):
        worker = workers[pk]
        if pk not in sessions:
            worker.stop()
            del workers[pk]
        elif not worker.is_alive():
            # Crashed or finished shutting down — drop it so it is respawned
            # below with a fresh session.
            logger.warning(
                "Worker for %s is gone — restarting it",
                worker.session.linkedin_profile.linkedin_username,
            )
            del workers[pk]

    for pk, session in sessions.items():
        if pk in workers:
            continue
        worker = _AccountWorker(session, qualifiers, spacer, stop_all)
        workers[pk] = worker
        worker.start()
