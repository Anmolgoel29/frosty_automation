"""Run the automation for exactly one LinkedIn profile, in this process.

Normally spawned by the supervisor (`manage.py rundaemon`), one per profile.
Running it by hand is fine too — useful for debugging a single account with
the supervisor stopped:

    python manage.py runworker --profile-id 3

Two things make it safe to have several of these around:

- It takes a Postgres advisory lock on its profile and exits if another
  process already holds it. That is what guarantees one browser per LinkedIn
  account even if an orphaned worker survived a supervisor kill.
- It handles SIGTERM by asking the work loop to stop at the next safe point,
  so a shutdown closes the browser and finishes the current task's
  bookkeeping instead of tearing the process down mid-action.
"""
from __future__ import annotations

import logging
import signal
import threading

from django.core.management.base import BaseCommand, CommandError

from linkedin.conf import WORKER_HEARTBEAT_INTERVAL

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the automation worker for a single LinkedIn profile."

    def add_arguments(self, parser):
        parser.add_argument(
            "--profile-id", type=int, required=True,
            help="LinkedInProfile primary key to run.",
        )

    def handle(self, *args, **options):
        from linkedin.browser.registry import get_or_create_session
        from linkedin.daemon import run_profile_worker
        from linkedin.locks import acquire_profile_lock
        from linkedin.logging import configure_logging
        from linkedin.models import LinkedInProfile, WorkerProcess

        configure_logging(level=logging.DEBUG if options["verbosity"] >= 2 else logging.INFO)

        profile_id = options["profile_id"]
        profile = (
            LinkedInProfile.objects.select_related("client")
            .filter(pk=profile_id)
            .first()
        )
        if profile is None:
            raise CommandError(f"No LinkedInProfile with id {profile_id}.")

        if not acquire_profile_lock(profile_id):
            # Not an error: the supervisor may have raced with an orphan from
            # a previous boot. Exit 0 so it isn't treated as a crash and
            # backed off — the next reconcile will try again once the lock
            # frees up.
            logger.warning(
                "[%s] another process already owns this profile — exiting", profile,
            )
            return

        stop = threading.Event()
        self._install_signal_handlers(stop, profile)

        record, _ = WorkerProcess.objects.get_or_create(linkedin_profile=profile)
        heartbeat = _Heartbeat(record, stop)
        heartbeat.start()

        try:
            session = get_or_create_session(profile)
            run_profile_worker(session, stop)
        finally:
            stop.set()
            heartbeat.join(timeout=5)
            try:
                session.close()
            except Exception:
                logger.debug("[%s] error closing browser session", profile, exc_info=True)
            logger.info("[%s] worker process exiting", profile)

    def _install_signal_handlers(self, stop: threading.Event, profile) -> None:
        def handle(signum, _frame):
            logger.info(
                "[%s] received %s — finishing up", profile, signal.Signals(signum).name,
            )
            stop.set()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)


class _Heartbeat(threading.Thread):
    """Writes ``WorkerProcess.last_heartbeat`` on a fixed cadence.

    A thread rather than a call inside the work loop, because the loop can
    legitimately sit inside a single long Playwright action. Liveness should
    reflect "this process is up", which is exactly what the supervisor and
    the admin panel need to distinguish a working profile from a wedged one.
    """

    def __init__(self, record, stop: threading.Event):
        super().__init__(name="heartbeat", daemon=True)
        self._record = record
        self._stop = stop

    def run(self) -> None:
        from django.db import connections

        try:
            while not self._stop.wait(WORKER_HEARTBEAT_INTERVAL):
                try:
                    self._record.mark_heartbeat()
                except Exception:
                    # A blip writing the heartbeat must not take the worker
                    # down; the supervisor tolerates a missed beat.
                    logger.debug("heartbeat write failed", exc_info=True)
        finally:
            connections.close_all()
