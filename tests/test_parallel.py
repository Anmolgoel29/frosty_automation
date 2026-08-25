# tests/test_parallel.py
"""The pieces that make one worker thread per account safe.

These exercise the claiming/locking primitives directly rather than spawning
real worker threads: pytest-django wraps each test in a transaction on the
main thread's connection, so a second thread gets its own connection and
cannot see the test's uncommitted rows.
"""
import threading
import time

import pytest
from django.utils import timezone

from linkedin.models import SearchKeyword, Task
from linkedin.pipeline.locks import campaign_lock
from linkedin.pipeline.search import _claim_keyword


def _make_task(session, task_type=Task.TaskType.CONNECT, **payload):
    return Task.objects.create(
        task_type=task_type,
        linkedin_profile=session.linkedin_profile,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now(),
        payload={"campaign_id": session.campaign.pk, **payload},
    )


@pytest.mark.django_db
class TestTaskClaiming:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_claim_marks_running_atomically(self, fake_session):
        task = _make_task(fake_session)

        claimed = Task.objects.claim_for(fake_session.linkedin_profile.pk)

        assert claimed is not None and claimed.pk == task.pk
        assert claimed.status == Task.Status.RUNNING
        assert claimed.started_at is not None
        task.refresh_from_db()
        assert task.status == Task.Status.RUNNING

    def test_claim_is_scoped_to_one_account(self, fake_session, second_session):
        """A worker only ever claims its own account's work, so the two
        accounts can run their queues at the same time without contending."""
        _make_task(second_session)

        assert Task.objects.claim_for(fake_session.linkedin_profile.pk) is None
        assert Task.objects.claim_for(second_session.linkedin_profile.pk) is not None

    def test_claimed_task_is_not_claimed_twice(self, fake_session):
        _make_task(fake_session)

        first = Task.objects.claim_for(fake_session.linkedin_profile.pk)
        second = Task.objects.claim_for(fake_session.linkedin_profile.pk)

        assert first is not None
        assert second is None

    def test_future_tasks_are_not_claimed(self, fake_session):
        task = _make_task(fake_session)
        task.scheduled_at = timezone.now() + timezone.timedelta(hours=1)
        task.save(update_fields=["scheduled_at"])

        assert Task.objects.claim_for(fake_session.linkedin_profile.pk) is None

    def test_mark_pending_returns_it_to_the_queue(self, fake_session):
        _make_task(fake_session)
        claimed = Task.objects.claim_for(fake_session.linkedin_profile.pk)

        claimed.mark_pending()

        assert claimed.started_at is None
        assert Task.objects.claim_for(fake_session.linkedin_profile.pk) is not None

    def test_claim_prioritizes_messaging_over_connect(self, fake_session):
        """follow_up and check_pending share top priority (both are messaging-
        adjacent — check_pending is what discovers an accepted invite and
        hands it to follow_up), so connect claims last regardless of
        insertion order or which is chronologically oldest."""
        connect = _make_task(fake_session, task_type=Task.TaskType.CONNECT)
        check_pending = _make_task(fake_session, task_type=Task.TaskType.CHECK_PENDING, public_id="a")
        follow_up = _make_task(fake_session, task_type=Task.TaskType.FOLLOW_UP, public_id="b")

        first = Task.objects.claim_for(fake_session.linkedin_profile.pk)
        second = Task.objects.claim_for(fake_session.linkedin_profile.pk)
        third = Task.objects.claim_for(fake_session.linkedin_profile.pk)

        assert {first.pk, second.pk} == {check_pending.pk, follow_up.pk}
        assert third.pk == connect.pk

    def test_seconds_to_next_is_per_account(self, fake_session, second_session):
        task = _make_task(second_session)
        task.scheduled_at = timezone.now() + timezone.timedelta(hours=2)
        task.save(update_fields=["scheduled_at"])

        assert Task.objects.seconds_to_next(fake_session.linkedin_profile.pk) is None
        assert Task.objects.seconds_to_next(second_session.linkedin_profile.pk) > 0


@pytest.mark.django_db
class TestKeywordClaiming:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_claiming_marks_used(self, fake_session):
        SearchKeyword.objects.create(campaign=fake_session.campaign, keyword="cto berlin")

        kw = _claim_keyword(fake_session.campaign)

        assert kw.keyword == "cto berlin"
        assert kw.used is True
        assert kw.used_at is not None

    def test_each_keyword_goes_to_one_account_only(self, fake_session):
        """The keyword queue is how search work is divided — no account ever
        repeats a search another already ran."""
        for word in ("a", "b"):
            SearchKeyword.objects.create(campaign=fake_session.campaign, keyword=word)

        claimed = [_claim_keyword(fake_session.campaign) for _ in range(3)]

        assert [k.keyword for k in claimed[:2]] == ["a", "b"]
        assert claimed[2] is None

    def test_returns_none_when_pool_empty(self, fake_session):
        assert _claim_keyword(fake_session.campaign) is None


class TestCampaignLock:
    """No DB needed — the lock registry is pure in-memory coordination."""

    class _FakeCampaign:
        def __init__(self, pk):
            self.pk = pk

    def test_same_campaign_shares_one_lock(self):
        a = self._FakeCampaign(7)
        b = self._FakeCampaign(7)
        assert campaign_lock(a) is campaign_lock(b)

    def test_different_campaigns_do_not_block_each_other(self):
        assert campaign_lock(self._FakeCampaign(1)) is not campaign_lock(self._FakeCampaign(2))

    def test_lock_actually_serialises(self):
        campaign = self._FakeCampaign(99)
        order = []

        def worker(tag):
            with campaign_lock(campaign):
                order.append(f"{tag}-in")
                time.sleep(0.05)
                order.append(f"{tag}-out")

        threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Whoever went first finished before the other started — never
        # interleaved, which is what protects the shared GP model.
        assert order[0].endswith("-in")
        assert order[1] == order[0].replace("-in", "-out")


class TestStartSpacer:
    def test_second_account_waits_out_the_gap(self, monkeypatch):
        """Accounts don't begin a task in the same instant."""
        from linkedin import daemon

        monkeypatch.setitem(daemon.CAMPAIGN_CONFIG, "account_stagger_min_seconds", 0.2)
        monkeypatch.setitem(daemon.CAMPAIGN_CONFIG, "account_stagger_max_seconds", 0.2)

        spacer = daemon._StartSpacer()
        stop = threading.Event()

        start = time.monotonic()
        spacer.wait_turn(stop)   # first caller goes immediately
        spacer.wait_turn(stop)   # second waits out the gap
        elapsed = time.monotonic() - start

        assert elapsed >= 0.2

    def test_stop_event_cuts_the_wait_short(self, monkeypatch):
        from linkedin import daemon

        monkeypatch.setitem(daemon.CAMPAIGN_CONFIG, "account_stagger_min_seconds", 30)
        monkeypatch.setitem(daemon.CAMPAIGN_CONFIG, "account_stagger_max_seconds", 30)

        spacer = daemon._StartSpacer()
        stop = threading.Event()
        spacer.wait_turn(stop)

        stop.set()
        start = time.monotonic()
        spacer.wait_turn(stop)
        assert time.monotonic() - start < 1
