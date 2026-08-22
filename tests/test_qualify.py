# tests/test_qualify.py
"""Tests for the qualification logic in qualify module."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from linkedin.pipeline.qualify import run_qualification
from linkedin.ml.qualifier import BayesianQualifier


def _make_trained_qualifier(seed=42):
    qualifier = BayesianQualifier(seed=seed)
    rng = np.random.RandomState(seed)
    for _ in range(5):
        qualifier.update(rng.randn(384).astype(np.float32) + 1.0, 1)
        qualifier.update(rng.randn(384).astype(np.float32) - 1.0, 0)
    return qualifier


def _create_lead_with_embedding(lead_id, public_id):
    from crm.models import Lead
    emb = np.ones(384, dtype=np.float32)
    return Lead.objects.create(
        pk=lead_id,
        public_identifier=public_id,
        linkedin_url=f"https://linkedin.com/in/{public_id}/",
        embedding=emb.tobytes(),
    )


class TestQualifyAutoDecisions:
    def test_always_calls_llm(self, db):
        qualifier = _make_trained_qualifier()
        session = MagicMock()
        lead = _create_lead_with_embedding(1, "alice")

        with (
            patch("linkedin.pipeline.qualify.fetch_qualification_candidates", return_value=[lead]),
            patch("linkedin.pipeline.qualify._fetch_profile_text", return_value="engineer at acme"),
            patch("linkedin.ml.qualifier.qualify_with_llm", return_value=(1, "Good fit")) as mock_llm,
            patch.object(qualifier, "update"),
            patch("linkedin.db.leads.promote_lead_to_deal"),
        ):
            run_qualification(session, qualifier)
            mock_llm.assert_called_once()

    def test_llm_on_cold_start(self, db):
        qualifier = BayesianQualifier(seed=42)
        session = MagicMock()
        lead = _create_lead_with_embedding(1, "alice")

        with (
            patch("linkedin.pipeline.qualify.fetch_qualification_candidates", return_value=[lead]),
            patch("linkedin.pipeline.qualify._fetch_profile_text", return_value="engineer at acme"),
            patch("linkedin.ml.qualifier.qualify_with_llm", return_value=(0, "Bad fit")) as mock_llm,
            patch.object(qualifier, "update"),
            patch("linkedin.db.deals.create_disqualified_deal"),
        ):
            run_qualification(session, qualifier)
            mock_llm.assert_called_once()

    def test_disqualify_on_promote_failure(self, db):
        qualifier = _make_trained_qualifier()
        session = MagicMock()
        lead = _create_lead_with_embedding(1, "alice")

        with (
            patch("linkedin.pipeline.qualify.fetch_qualification_candidates", return_value=[lead]),
            patch("linkedin.pipeline.qualify._fetch_profile_text", return_value="engineer at acme"),
            patch("linkedin.ml.qualifier.qualify_with_llm", return_value=(1, "Good fit")),
            patch.object(qualifier, "update"),
            patch("linkedin.db.leads.promote_lead_to_deal",
                  side_effect=ValueError("no company_name")),
            patch("linkedin.db.deals.create_disqualified_deal") as mock_disqualify,
        ):
            run_qualification(session, qualifier)
            mock_disqualify.assert_called_once()


class TestFetchQualificationCandidatesOrdering:
    def test_newest_first_returns_most_recently_discovered(self, fake_session):
        """newest_first=True must surface freshly-discovered leads, not old ones.

        Regression guard for the bug this parameter exists to fix: once the
        unlabelled backlog exceeds MAX_QUALIFICATION_CANDIDATES, the default
        oldest-first window can never contain a lead search just found —
        qualify_source's exploit-mode retry then judges search results it
        can't actually see, and never converges. newest_first=True is what
        lets that check observe what search just discovered.
        """
        from linkedin.pipeline.qualify import (
            MAX_QUALIFICATION_CANDIDATES, fetch_qualification_candidates,
        )

        emb = np.ones(384, dtype=np.float32).tobytes()
        for i in range(MAX_QUALIFICATION_CANDIDATES + 5):
            from crm.models import Lead
            Lead.objects.create(
                linkedin_url=f"https://www.linkedin.com/in/n{i}/",
                public_identifier=f"n{i}", embedding=emb,
            )
        # The 5 leads created last are outside the default oldest-first
        # window (capped at MAX_QUALIFICATION_CANDIDATES) but must be the
        # only ones missing from the newest-first window.
        newest_ids = {
            f"n{i}" for i in range(5, MAX_QUALIFICATION_CANDIDATES + 5)
        }

        oldest_first = fetch_qualification_candidates(fake_session)
        newest_first = fetch_qualification_candidates(fake_session, newest_first=True)

        oldest_ids = {c.public_identifier for c in oldest_first}
        newest_first_ids = {c.public_identifier for c in newest_first}

        assert len(newest_first) == MAX_QUALIFICATION_CANDIDATES
        assert newest_first_ids == newest_ids
        # The two windows must actually differ — proves ordering, not just count.
        assert oldest_ids != newest_first_ids
        for i in range(5):
            assert f"n{i}" not in newest_first_ids, (
                "a freshly-created lead is missing from the newest-first window"
            )
