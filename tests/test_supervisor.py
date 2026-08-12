# tests/test_supervisor.py
"""The supervisor: real subprocesses, reconciled against WorkerProcess rows.

``Supervisor.tick()`` is the whole contract, so the tests drive it directly
rather than racing ``run_forever``. Spawning is stubbed with a trivial
sleeping subprocess — these tests are about the reconcile logic and the
persisted state, not about Playwright.
"""
import subprocess
import sys
import time

import pytest

from linkedin.models import WorkerProcess
from linkedin.supervisor import Supervisor, eligible_profiles
from tests.conftest import make_session
from tests.factories import CampaignFactory, LinkedInProfileFactory


def _sleeper(seconds: int = 60) -> subprocess.Popen:
    """A stand-in child process that just stays alive until terminated."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _instant(code: int = 1) -> subprocess.Popen:
    """A stand-in child that exits immediately with *code*."""
    return subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])


class _StubSpawn:
    """Replaces subprocess.Popen inside the supervisor, recording each call."""

    def __init__(self, factory=_sleeper):
        self._factory = factory
        self.calls: list[list[str]] = []
        self.procs: list[subprocess.Popen] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        proc = self._factory()
        self.procs.append(proc)
        return proc

    def cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture
def spawn(monkeypatch):
    stub = _StubSpawn()
    monkeypatch.setattr("linkedin.supervisor.subprocess.Popen", stub)
    yield stub
    stub.cleanup()


@pytest.fixture
def supervisor():
    sup = Supervisor(poll_interval=0.01)
    yield sup
    sup.shutdown()


@pytest.mark.django_db
class TestEligibility:
    def test_profile_on_a_campaign_is_eligible(self, fake_session):
        assert [p.pk for p in eligible_profiles()] == [fake_session.linkedin_profile.pk]

    def test_profile_with_no_campaign_is_not_eligible(self, fake_client):
        LinkedInProfileFactory(client=fake_client)
        assert eligible_profiles() == []

    def test_inactive_profile_is_not_eligible(self, fake_session):
        profile = fake_session.linkedin_profile
        profile.active = False
        profile.save(update_fields=["active"])
        assert eligible_profiles() == []

    def test_paused_client_takes_its_profiles_offline(self, fake_session, second_session):
        client = fake_session.client
        client.active = False
        client.save(update_fields=["active"])
        assert eligible_profiles() == []

    def test_one_paused_client_does_not_affect_another(self, fake_session, other_client_session):
        paused = fake_session.client
        paused.active = False
        paused.save(update_fields=["active"])
        assert [p.pk for p in eligible_profiles()] == [
            other_client_session.linkedin_profile.pk,
        ]

    def test_profile_on_several_campaigns_appears_once(self, fake_session):
        second = CampaignFactory(client=fake_session.client, name="Second")
        second.profiles.add(fake_session.linkedin_profile)
        assert len(eligible_profiles()) == 1


@pytest.mark.django_db
class TestProvisioning:
    def test_registers_a_row_per_eligible_profile(self, fake_session, second_session, supervisor, spawn):
        supervisor.tick()

        assert WorkerProcess.objects.count() == 2
        assert set(WorkerProcess.objects.values_list("desired_state", flat=True)) == {"running"}

    def test_new_profiles_default_to_running(self, fake_session, supervisor, spawn):
        supervisor.tick()
        record = WorkerProcess.objects.get()
        assert record.desired_state == WorkerProcess.Desired.RUNNING

    def test_does_not_overwrite_an_operator_decision(self, fake_session, supervisor, spawn):
        """A profile the operator stopped must stay stopped across polls."""
        supervisor.tick()
        WorkerProcess.objects.update(desired_state=WorkerProcess.Desired.STOPPED)

        supervisor.tick()
        supervisor.tick()

        assert WorkerProcess.objects.get().desired_state == WorkerProcess.Desired.STOPPED


@pytest.mark.django_db
class TestSpawning:
    def test_starts_one_process_per_profile(self, fake_session, second_session, supervisor, spawn):
        supervisor.tick()
        assert len(spawn.calls) == 2
        assert len(supervisor.running) == 2

    def test_spawns_runworker_with_the_profile_id(self, fake_session, supervisor, spawn):
        supervisor.tick()

        argv = spawn.calls[0]
        assert "runworker" in argv
        assert "--profile-id" in argv
        assert argv[argv.index("--profile-id") + 1] == str(fake_session.linkedin_profile.pk)

    def test_records_pid_and_status(self, fake_session, supervisor, spawn):
        supervisor.tick()

        record = WorkerProcess.objects.get()
        assert record.status == WorkerProcess.Status.RUNNING
        assert record.pid == spawn.procs[0].pid
        assert record.started_at is not None
        assert record.boot_id == supervisor.boot_id

    def test_does_not_start_a_second_process_for_one_profile(self, fake_session, supervisor, spawn):
        supervisor.tick()
        supervisor.tick()
        supervisor.tick()

        assert len(spawn.calls) == 1

    def test_new_profile_starts_on_the_next_tick(self, fake_session, supervisor, spawn):
        """Adding a profile is all it takes — no restart."""
        supervisor.tick()
        assert len(supervisor.running) == 1

        make_session(campaign=fake_session.campaign)
        supervisor.tick()

        assert len(supervisor.running) == 2

    def test_stopped_profile_is_never_started(self, fake_session, supervisor, spawn):
        supervisor.tick()  # registers the row
        WorkerProcess.objects.update(desired_state=WorkerProcess.Desired.STOPPED)
        spawn.calls.clear()

        # Drop the child so only desired_state governs the next decision.
        supervisor.tick()
        supervisor.tick()

        assert spawn.calls == []


@pytest.mark.django_db
class TestStopping:
    def test_stop_request_terminates_the_process(self, fake_session, supervisor, spawn):
        supervisor.tick()
        proc = spawn.procs[0]

        WorkerProcess.objects.update(desired_state=WorkerProcess.Desired.STOPPED)
        supervisor.tick()

        assert proc.wait(timeout=10) is not None
        supervisor.tick()  # reap
        record = WorkerProcess.objects.get()
        assert record.status == WorkerProcess.Status.STOPPED
        assert record.pid is None

    def test_deactivated_profile_is_stopped(self, fake_session, supervisor, spawn):
        supervisor.tick()
        proc = spawn.procs[0]

        profile = fake_session.linkedin_profile
        profile.active = False
        profile.save(update_fields=["active"])
        supervisor.tick()

        assert proc.wait(timeout=10) is not None

    def test_shutdown_stops_everything(self, fake_session, second_session, supervisor, spawn):
        supervisor.tick()
        procs = list(spawn.procs)

        supervisor.shutdown()

        for proc in procs:
            assert proc.poll() is not None
        assert supervisor.running == []


@pytest.mark.django_db
class TestCrashHandling:
    def test_crash_is_recorded_and_backed_off(self, fake_session, monkeypatch, supervisor):
        stub = _StubSpawn(factory=_instant)
        monkeypatch.setattr("linkedin.supervisor.subprocess.Popen", stub)

        supervisor.tick()
        stub.procs[0].wait(timeout=10)
        supervisor.tick()  # reaps the dead child

        record = WorkerProcess.objects.get()
        assert record.status == WorkerProcess.Status.CRASHED
        assert record.last_exit_code == 1
        assert record.restart_count == 1
        assert "exited with code 1" in record.last_error

        # Still inside the backoff window — must not respawn immediately.
        supervisor.tick()
        assert len(stub.calls) == 1

    def test_backoff_grows_with_the_persisted_restart_count(self, fake_session, supervisor, spawn):
        supervisor.tick()
        record = WorkerProcess.objects.get()

        first = supervisor._backoff_for(record.linkedin_profile_id)
        WorkerProcess.objects.update(restart_count=3)
        later = supervisor._backoff_for(record.linkedin_profile_id)

        assert later > first


@pytest.mark.django_db
class TestStateAcrossRestarts:
    def test_rows_from_a_previous_boot_are_reset(self, fake_session):
        """A container restart leaves rows claiming to run; they're not ours."""
        WorkerProcess.objects.create(
            linkedin_profile=fake_session.linkedin_profile,
            status=WorkerProcess.Status.RUNNING,
            desired_state=WorkerProcess.Desired.RUNNING,
            pid=999999,
            boot_id="a-previous-boot",
        )

        Supervisor().adopt_previous_boot()

        record = WorkerProcess.objects.get()
        assert record.status == WorkerProcess.Status.CRASHED
        assert record.pid is None

    def test_desired_state_survives_a_restart(self, fake_session, spawn):
        """The operator's decision is the thing that must persist."""
        first = Supervisor(poll_interval=0.01)
        first.tick()
        WorkerProcess.objects.update(desired_state=WorkerProcess.Desired.STOPPED)
        first.shutdown()

        second = Supervisor(poll_interval=0.01)
        second.adopt_previous_boot()
        spawn.calls.clear()
        second.tick()
        try:
            assert spawn.calls == []
            assert second.running == []
        finally:
            second.shutdown()

    def test_wanted_profiles_come_back_after_a_restart(self, fake_session, spawn):
        first = Supervisor(poll_interval=0.01)
        first.tick()
        first.shutdown()
        spawn.calls.clear()

        second = Supervisor(poll_interval=0.01)
        second.adopt_previous_boot()
        second.tick()
        try:
            assert len(spawn.calls) == 1
            assert len(second.running) == 1
        finally:
            second.shutdown()


@pytest.mark.django_db
class TestAdvisoryLocks:
    """One process per profile, enforced by Postgres rather than convention."""

    def test_a_profile_can_be_claimed(self, fake_session):
        from linkedin.locks import acquire_profile_lock, release_profile_lock

        profile_id = fake_session.linkedin_profile.pk
        try:
            assert acquire_profile_lock(profile_id) is True
        finally:
            release_profile_lock(profile_id)

    def test_a_claimed_profile_reads_as_taken(self, fake_session):
        """What the supervisor checks before spawning a replacement."""
        from linkedin.locks import (
            acquire_profile_lock, profile_lock_is_free, release_profile_lock,
        )

        profile_id = fake_session.linkedin_profile.pk
        assert profile_lock_is_free(profile_id) is True

        acquire_profile_lock(profile_id)
        try:
            assert profile_lock_is_free(profile_id) is False
        finally:
            release_profile_lock(profile_id)

        assert profile_lock_is_free(profile_id) is True

    def test_pool_lock_is_released_on_exit(self, fake_session):
        from linkedin.locks import campaign_pool_lock

        campaign_id = fake_session.campaign.pk
        with campaign_pool_lock(campaign_id) as acquired:
            assert acquired is True
        # Re-acquiring after the block proves the unlock ran.
        with campaign_pool_lock(campaign_id) as again:
            assert again is True

    def test_supervisor_defers_a_profile_someone_else_owns(self, fake_session, supervisor, spawn):
        """An orphan holding the lock must not cause a spawn/exit churn."""
        from linkedin.locks import acquire_profile_lock, release_profile_lock

        profile_id = fake_session.linkedin_profile.pk
        acquire_profile_lock(profile_id)
        try:
            supervisor.tick()
            assert spawn.calls == []
        finally:
            release_profile_lock(profile_id)
