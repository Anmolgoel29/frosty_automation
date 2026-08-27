# tests/test_db_query_budget.py
"""Query-count budgets for the paths the daemon runs on a loop.

These assert *shape*, not micro-performance: each hot path must cost a
constant number of queries rather than one per lead/deal. The daemon
reconciles every 60s forever and re-ranks the pool on every connect task, so
a per-row query reintroduced here shows up as a continuously growing DB load
in production — the exact regression these numbers were chosen to catch.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from crm.models import Deal, Lead
from linkedin.enums import ProfileState
from linkedin.models import Task
from tests.conftest import sessions_map


def _seed_qualified(session, n):
    """Add n more QUALIFIED deals."""
    start = Lead.objects.count()
    for i in range(start, start + n):
        lead = Lead.objects.create(
            linkedin_url=f"https://www.linkedin.com/in/q{i}/",
            public_identifier=f"q{i}",
        )
        Deal.objects.create(lead=lead, campaign=session.campaign,
                            state=ProfileState.QUALIFIED)


def _query_count(fn) -> int:
    with CaptureQueriesContext(connection) as ctx:
        fn()
    return len(ctx.captured_queries)


@pytest.mark.django_db
class TestQueryBudgets:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_reconcile_cost_is_flat_in_deal_count(self, fake_session):
        """Reconcile runs every 60s; its cost must not track the pipeline size."""
        from linkedin.tasks.scheduler import reconcile

        sessions = sessions_map(fake_session)

        _seed_qualified(fake_session, 5)
        Deal.objects.update(state=ProfileState.CONNECTED,
                            assigned_profile=fake_session.linkedin_profile)
        reconcile(sessions)
        Task.objects.all().delete()
        small = _query_count(lambda: reconcile(sessions))

        _seed_qualified(fake_session, 45)
        Deal.objects.update(state=ProfileState.CONNECTED,
                            assigned_profile=fake_session.linkedin_profile)
        reconcile(sessions)
        Task.objects.all().delete()
        large = _query_count(lambda: reconcile(sessions))

        assert small == large, (
            f"reconcile went from {small} to {large} queries when deals grew "
            f"5 → 50 — a per-deal query crept back in"
        )
        assert large < 20

    def test_promote_to_ready_cost_is_flat_in_pool_size(self, fake_session):
        """A single bulk UPDATE, not one query per QUALIFIED deal."""
        from linkedin.pipeline.ready_pool import promote_to_ready

        _seed_qualified(fake_session, 5)
        Deal.objects.update(fit_score=5)
        small = _query_count(lambda: promote_to_ready(fake_session, 4))

        _seed_qualified(fake_session, 45)
        Deal.objects.filter(state=ProfileState.QUALIFIED).update(fit_score=5)
        large = _query_count(lambda: promote_to_ready(fake_session, 4))

        assert small == large, (
            f"promote_to_ready went from {small} to {large} queries when the "
            f"QUALIFIED pool grew 5 → 50 — the per-deal loop is back"
        )

    def test_ready_pool_ranking_cost_is_flat_in_pool_size(self, fake_session):
        """Ranking is now an indexed ORDER BY at read time, not a scoring pass —
        runs on every connect task over the whole ready pool."""
        from linkedin.db.deals import get_ready_to_connect_profiles
        from linkedin.pipeline.allocation import allocate_ready_deals

        def measure(n_total):
            _seed_qualified(fake_session, n_total - Deal.objects.count())
            Deal.objects.update(state=ProfileState.READY_TO_CONNECT,
                                assigned_profile=None, fit_score=3)
            allocate_ready_deals(fake_session.campaign)
            return _query_count(lambda: get_ready_to_connect_profiles(fake_session))

        assert measure(5) == measure(50)

    def test_qualification_candidate_fetch_is_a_single_row_query(self, fake_session):
        """The cheap-stage cascade only ever needs the next one lead in FIFO
        order — no reason to pull a windowed batch across the wire anymore."""
        from linkedin.pipeline.qualify import fetch_next_qualification_candidate

        Lead.objects.bulk_create([
            Lead(linkedin_url=f"https://www.linkedin.com/in/p{i}/", public_identifier=f"p{i}")
            for i in range(300)
        ])

        assert _query_count(lambda: fetch_next_qualification_candidate(fake_session)) == 1
        candidate = fetch_next_qualification_candidate(fake_session)
        assert candidate.public_identifier == "p0"

    def test_state_transition_does_not_rewrite_summary_blobs(self, fake_session):
        """Deal carries two JSON fact lists; a state change must not resend them."""
        from linkedin.db.deals import set_profile_state
        from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal

        create_enriched_lead(fake_session, "https://www.linkedin.com/in/x/",
                             {"first_name": "X", "positions": []})
        promote_lead_to_deal(fake_session, "x")
        Deal.objects.update(chat_summary=[{"fact": "f" * 500}])

        with CaptureQueriesContext(connection) as ctx:
            set_profile_state(fake_session, "x", ProfileState.PENDING.value)

        updates = [q["sql"] for q in ctx.captured_queries
                   if q["sql"].lstrip().upper().startswith("UPDATE")]
        assert updates, "expected the deal row to be updated"
        assert not any("chat_summary" in sql for sql in updates), (
            "state transition rewrote chat_summary — use update_fields"
        )
