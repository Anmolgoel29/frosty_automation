# tests/test_db_query_budget.py
"""Query-count budgets for the paths the daemon runs on a loop.

These assert *shape*, not micro-performance: each hot path must cost a
constant number of queries rather than one per lead/deal. The daemon
reconciles every 60s forever and re-ranks the pool on every connect task, so
a per-row query reintroduced here shows up as a continuously growing DB load
in production — the exact regression these numbers were chosen to catch.
"""
import numpy as np
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from unittest.mock import patch

from crm.models import Deal, Lead
from linkedin.enums import ProfileState
from linkedin.models import Task
from tests.conftest import sessions_map


def _seed_qualified(session, n):
    """Add n more QUALIFIED deals whose leads are already embedded."""
    emb = np.ones(384, dtype=np.float32).tobytes()
    start = Lead.objects.count()
    for i in range(start, start + n):
        lead = Lead.objects.create(
            linkedin_url=f"https://www.linkedin.com/in/q{i}/",
            public_identifier=f"q{i}", embedding=emb,
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
        from linkedin.ml.qualifier import BayesianQualifier
        from linkedin.pipeline.ready_pool import promote_to_ready

        scorer = BayesianQualifier(seed=42)

        _seed_qualified(fake_session, 5)
        with patch.object(scorer, "predict_probs", return_value=np.zeros(5)):
            small = _query_count(lambda: promote_to_ready(fake_session, scorer, 0.9))

        _seed_qualified(fake_session, 45)
        with patch.object(scorer, "predict_probs", return_value=np.zeros(50)):
            large = _query_count(lambda: promote_to_ready(fake_session, scorer, 0.9))

        assert small == large, (
            f"promote_to_ready went from {small} to {large} queries when the "
            f"QUALIFIED pool grew 5 → 50 — the embedding N+1 is back"
        )

    def test_rank_profiles_cost_is_flat_in_pool_size(self, fake_session):
        """Ranking runs on every connect task over the whole ready pool."""
        from linkedin.db.deals import get_ready_to_connect_profiles
        from linkedin.ml.qualifier import BayesianQualifier
        from linkedin.pipeline.allocation import allocate_ready_deals

        scorer = BayesianQualifier(seed=42)
        scorer._fitted = True
        scorer._pipeline = type(
            "P", (), {"predict": staticmethod(lambda X: np.zeros(len(X)))},
        )()

        def measure(n_total):
            _seed_qualified(fake_session, n_total - Deal.objects.count())
            Deal.objects.update(state=ProfileState.READY_TO_CONNECT,
                                assigned_profile=None)
            allocate_ready_deals(fake_session.campaign)
            profiles = get_ready_to_connect_profiles(fake_session)
            return _query_count(
                lambda: scorer.rank_profiles(profiles, session=fake_session),
            )

        assert measure(5) == measure(50)

    def test_qualification_pool_is_capped(self, fake_session):
        """An unbounded pool read grows forever as the campaign discovers leads."""
        from linkedin.pipeline.qualify import (
            MAX_QUALIFICATION_CANDIDATES, fetch_qualification_candidates,
        )

        emb = np.ones(384, dtype=np.float32).tobytes()
        Lead.objects.bulk_create([
            Lead(linkedin_url=f"https://www.linkedin.com/in/p{i}/",
                 public_identifier=f"p{i}", embedding=emb)
            for i in range(MAX_QUALIFICATION_CANDIDATES + 25)
        ])

        assert _query_count(lambda: fetch_qualification_candidates(fake_session)) == 1
        assert len(fetch_qualification_candidates(fake_session)) == MAX_QUALIFICATION_CANDIDATES

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
