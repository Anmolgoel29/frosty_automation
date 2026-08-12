# linkedin/supervisor.py
"""Supervises one OS process per LinkedIn profile.

Each profile's automation is a separate process — `manage.py runworker
--profile-id N` — with its own Python interpreter, its own Playwright, its
own browser, and its own database connections. One profile crashing, wedging
inside a Playwright call, or leaking memory cannot touch another.

**The admin panel governs these processes without spawning them.** It can't:
uvicorn is a different process (often several), and anything it forked would
be orphaned on the next redeploy. So the two halves talk through the
database instead — the classic desired/observed split:

    admin panel  ──writes──▶  WorkerProcess.desired_state
                                       │
                             supervisor reconciles
                                       │
                                       ▼
                              spawn / stop / restart

The supervisor owns everything else on the row (status, pid, exit code,
restart count) and never second-guesses the operator's intent. Because
intent lives in Postgres, it survives shutdown: a restarted container brings
back exactly the processes that were meant to be running, and a profile the
operator stopped last week stays stopped.

Safety against double-running: every worker holds a Postgres advisory lock
on its profile for its whole life (see ``locks.py``). If this supervisor is
SIGKILLed and leaves orphans behind, the replacement's workers refuse to
start rather than run a second browser against the same LinkedIn account.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

from django.db import connection
from django.utils import timezone
from termcolor import colored

from linkedin.conf import (
    ROOT_DIR,
    WORKER_POLL_INTERVAL,
    WORKER_RESTART_DELAY,
    WORKER_RESTART_MAX_DELAY,
    WORKER_SHUTDOWN_TIMEOUT,
)
from linkedin.locks import profile_lock_is_free
from linkedin.models import LinkedInProfile, WorkerProcess

logger = logging.getLogger(__name__)


def _die_with_parent() -> None:
    """Ask the kernel to SIGTERM this child if the supervisor ever dies.

    Runs in the child between fork and exec. Linux-only (``PR_SET_PDEATHSIG``);
    elsewhere it is a no-op and orphans are instead caught by the profile
    advisory lock, which stops a replacement from double-running an account.

    Safe as a ``preexec_fn`` here because the supervisor is single-threaded —
    the usual warning is about forking from a process with other threads
    holding locks.
    """
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:  # pragma: no cover - platform dependent
        pass


@dataclass
class _Child:
    """A live subprocess and the bookkeeping the supervisor keeps for it."""

    profile_id: int
    label: str
    popen: subprocess.Popen
    started_at: float = field(default_factory=time.monotonic)
    # Set when we've sent SIGTERM and are waiting for it to go.
    terminating_since: float | None = None


def eligible_profiles() -> list[LinkedInProfile]:
    """Profiles that are allowed to run: active, active client, has campaigns.

    Eligibility is about configuration; ``desired_state`` is about intent.
    A profile has to pass both to get a process.
    """
    return list(
        LinkedInProfile.objects.filter(active=True, client__active=True)
        .filter(campaigns__isnull=False)
        .select_related("client")
        .distinct()
        .order_by("pk"),
    )


class Supervisor:
    """Reconciles running processes against the ``WorkerProcess`` table."""

    def __init__(self, poll_interval: float = WORKER_POLL_INTERVAL):
        self._poll_interval = poll_interval
        self._children: dict[int, _Child] = {}
        self._retry_after: dict[int, float] = {}
        self._shutdown = threading.Event()
        self.boot_id = uuid.uuid4().hex
        self.host = socket.gethostname()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def run_forever(self) -> None:
        """Supervise until SIGTERM/SIGINT. Blocks the calling thread."""
        self._install_signal_handlers()
        self.adopt_previous_boot()

        logger.info(
            "%s (boot %s on %s)",
            colored("Supervisor started", "green", attrs=["bold"]),
            self.boot_id[:8], self.host,
        )
        try:
            while not self._shutdown.is_set():
                try:
                    self.tick()
                except Exception:
                    # A bad poll must never kill the supervisor — that would
                    # strand every worker with nobody to restart or stop them.
                    logger.exception("Supervisor tick failed — retrying next poll")
                # Don't hold a connection open across the sleep: the supervisor
                # is idle almost all the time and a server-side idle timeout
                # would break the next query. It holds no advisory locks, so
                # closing costs nothing.
                connection.close()
                self._shutdown.wait(self._poll_interval)
        finally:
            self.shutdown()

    def _install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            logger.info("Received %s — shutting down", signal.Signals(signum).name)
            self._shutdown.set()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    def adopt_previous_boot(self) -> None:
        """Clean up rows left behind by a supervisor that didn't exit cleanly.

        Any row claiming to be RUNNING under a different boot id describes a
        process this supervisor does not own. Its pid is meaningless here, so
        the row is marked crashed; if the profile is still wanted, the normal
        reconcile spawns a fresh process. Should an orphan somehow still be
        alive, its advisory lock stops the replacement from double-running.
        """
        stale = WorkerProcess.objects.exclude(boot_id=self.boot_id).exclude(
            status=WorkerProcess.Status.STOPPED,
        )
        count = stale.update(
            status=WorkerProcess.Status.CRASHED,
            pid=None,
            stopped_at=timezone.now(),
            last_error="Supervisor restarted; process from a previous boot was not adopted.",
        )
        if count:
            logger.info("Reset %d worker row(s) left over from a previous boot", count)

    def shutdown(self) -> None:
        """Stop every child and record it, so the next boot starts clean."""
        self._shutdown.set()
        if not self._children:
            return

        logger.info("Stopping %d worker process(es)", len(self._children))
        for child in list(self._children.values()):
            self._signal_stop(child)

        deadline = time.monotonic() + WORKER_SHUTDOWN_TIMEOUT
        while self._children and time.monotonic() < deadline:
            self._reap()
            time.sleep(0.2)

        for child in list(self._children.values()):
            logger.warning("[%s] did not exit in time — killing", child.label)
            self._kill(child)
            self._forget(child, status=WorkerProcess.Status.STOPPED)

    # ── One reconcile pass ────────────────────────────────────────────

    def tick(self) -> None:
        """Make the world match ``WorkerProcess.desired_state``."""
        self._provision_rows()
        self._reap()

        wanted = self._wanted_profile_ids()
        self._stop_unwanted(wanted)
        self._start_missing(wanted)

    def _provision_rows(self) -> None:
        """Give every eligible profile a WorkerProcess row.

        New profiles default to ``desired_state=RUNNING`` — adding a profile
        in the admin panel is meant to start automating it. An existing row
        is never touched, so an operator's "stop" decision is not undone by
        the next poll.
        """
        for profile in eligible_profiles():
            _, created = WorkerProcess.objects.get_or_create(linkedin_profile=profile)
            if created:
                logger.info("[%s] registered — will start on this pass", profile)

    def _wanted_profile_ids(self) -> set[int]:
        """Profiles that are both eligible and wanted running."""
        eligible_ids = {p.pk for p in eligible_profiles()}
        wanted = set(
            WorkerProcess.objects.filter(
                desired_state=WorkerProcess.Desired.RUNNING,
                linkedin_profile_id__in=eligible_ids,
            ).values_list("linkedin_profile_id", flat=True),
        )
        return wanted

    def _reap(self) -> None:
        """Record any child that has exited."""
        for profile_id, child in list(self._children.items()):
            code = child.popen.poll()
            if code is None:
                continue

            if child.terminating_since is not None:
                logger.info("[%s] stopped (exit %s)", child.label, code)
                self._forget(child, status=WorkerProcess.Status.STOPPED, exit_code=code)
                continue

            if code == 0:
                # A clean exit we didn't ask for. The usual cause is the worker
                # finding the profile's advisory lock already held — it declines
                # to double-run and returns 0. Not a crash, so no crash backoff
                # and no restart_count bump, but still pause before retrying so
                # a persistent orphan can't spin the supervisor.
                logger.info(
                    "[%s] exited cleanly without being asked — retrying in %ds",
                    child.label, WORKER_RESTART_DELAY,
                )
                self._forget(child, status=WorkerProcess.Status.STOPPED, exit_code=0)
                self._retry_after[profile_id] = time.monotonic() + WORKER_RESTART_DELAY
                continue

            delay = self._backoff_for(profile_id)
            logger.warning(
                "[%s] exited unexpectedly (exit %s) — restarting in %ds",
                child.label, code, delay,
            )
            self._forget(
                child,
                status=WorkerProcess.Status.CRASHED,
                exit_code=code,
                error=f"Process exited with code {code}.",
                bump_restart=True,
            )
            self._retry_after[profile_id] = time.monotonic() + delay

    def _stop_unwanted(self, wanted: set[int]) -> None:
        """Terminate processes whose profile is no longer wanted running."""
        for profile_id, child in list(self._children.items()):
            if profile_id in wanted:
                continue
            if child.terminating_since is None:
                logger.info("[%s] no longer wanted — stopping", child.label)
                self._signal_stop(child)
            elif time.monotonic() - child.terminating_since > WORKER_SHUTDOWN_TIMEOUT:
                logger.warning("[%s] ignored SIGTERM — killing", child.label)
                self._kill(child)

    def _start_missing(self, wanted: set[int]) -> None:
        """Spawn a process for anything wanted that isn't running."""
        now = time.monotonic()
        for profile_id in sorted(wanted):
            if profile_id in self._children:
                continue
            if now < self._retry_after.get(profile_id, 0.0):
                continue
            if not profile_lock_is_free(profile_id):
                # Some process we don't own still holds this profile — an
                # orphan from a supervisor that was killed outright. Spawning
                # now would just produce a worker that exits on the lock, so
                # wait for the orphan to go instead of churning.
                logger.warning(
                    "Profile %d is still held by another process — deferring start",
                    profile_id,
                )
                self._retry_after[profile_id] = now + WORKER_RESTART_DELAY
                continue
            self._retry_after.pop(profile_id, None)
            self._spawn(profile_id)

    # ── Process control ───────────────────────────────────────────────

    def _spawn(self, profile_id: int) -> None:
        record = (
            WorkerProcess.objects.select_related(
                "linkedin_profile", "linkedin_profile__client",
            )
            .filter(linkedin_profile_id=profile_id)
            .first()
        )
        if record is None:
            return
        label = str(record.linkedin_profile)

        WorkerProcess.objects.filter(pk=record.pk).update(
            status=WorkerProcess.Status.STARTING,
            boot_id=self.boot_id,
            host=self.host,
            last_error="",
        )

        popen = subprocess.Popen(
            [sys.executable, "manage.py", "runworker", "--profile-id", str(profile_id)],
            cwd=str(ROOT_DIR),
            env=os.environ.copy(),
            # Own session: a Ctrl-C in an attached terminal hits the supervisor
            # only, and children are stopped deliberately and in order.
            start_new_session=True,
            # ...but never outlive the supervisor. Without this, killing the
            # supervisor (SIGKILL, OOM) would strand running browsers that the
            # replacement supervisor can neither see nor stop.
            preexec_fn=_die_with_parent,
        )

        self._children[profile_id] = _Child(
            profile_id=profile_id, label=label, popen=popen,
        )
        WorkerProcess.objects.filter(pk=record.pk).update(
            status=WorkerProcess.Status.RUNNING,
            pid=popen.pid,
            started_at=timezone.now(),
            stopped_at=None,
            last_heartbeat=timezone.now(),
        )
        logger.info("[%s] %s (pid %d)", label, colored("worker started", "green"), popen.pid)

    def _signal_stop(self, child: _Child) -> None:
        child.terminating_since = time.monotonic()
        WorkerProcess.objects.filter(linkedin_profile_id=child.profile_id).update(
            status=WorkerProcess.Status.STOPPING,
        )
        try:
            child.popen.terminate()
        except ProcessLookupError:
            pass

    def _kill(self, child: _Child) -> None:
        try:
            child.popen.kill()
        except ProcessLookupError:
            pass

    def _forget(
        self,
        child: _Child,
        *,
        status: str,
        exit_code: int | None = None,
        error: str = "",
        bump_restart: bool = False,
    ) -> None:
        self._children.pop(child.profile_id, None)

        record = WorkerProcess.objects.filter(linkedin_profile_id=child.profile_id).first()
        if record is None:
            return
        record.status = status
        record.pid = None
        record.stopped_at = timezone.now()
        record.last_exit_code = exit_code
        if error:
            record.last_error = error
        if bump_restart:
            record.restart_count += 1
        record.save(update_fields=[
            "status", "pid", "stopped_at", "last_exit_code", "last_error", "restart_count",
        ])

    def _backoff_for(self, profile_id: int) -> int:
        """Exponential backoff on repeated crashes, capped.

        Read from the persisted restart count, so a profile that crash-loops
        across supervisor restarts keeps backing off instead of resetting to
        a tight loop every boot.
        """
        count = (
            WorkerProcess.objects.filter(linkedin_profile_id=profile_id)
            .values_list("restart_count", flat=True)
            .first()
        ) or 0
        return min(WORKER_RESTART_DELAY * (2 ** min(count, 5)), WORKER_RESTART_MAX_DELAY)

    # ── Introspection (used by tests and logging) ─────────────────────

    @property
    def running(self) -> list[str]:
        return [c.label for c in self._children.values() if c.popen.poll() is None]


def run_supervisor() -> None:
    """Entrypoint used by ``manage.py rundaemon``."""
    # The supervisor spends its life sleeping; holding a Postgres connection
    # open across those gaps invites a server-side idle timeout, and the next
    # query fails. Close between polls and let Django reconnect.
    connection.close()
    Supervisor().run_forever()
