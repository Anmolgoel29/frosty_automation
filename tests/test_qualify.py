# tests/test_qualify.py
"""Tests for the two-stage qualification cascade in pipeline/qualify.py."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from linkedin.ml.qualifier import CheapDisqualifyDecision, QualificationDecision
from linkedin.pipeline.qualify import run_qualification

CHEAP_MODEL = "anthropic:claude-haiku"
EXPENSIVE_MODEL = "anthropic:claude-opus"


def _create_lead(lead_id, public_id, **coarse):
    from crm.models import Lead
    return Lead.objects.create(
        pk=lead_id,
        public_identifier=public_id,
        linkedin_url=f"https://linkedin.com/in/{public_id}/",
        **coarse,
    )


class TestRunQualificationCascade:
    def test_cheap_disqualify_skips_dossier_and_expensive_stage(self, db):
        """A cheap-stage disqualify must skip the whole dossier scrape.

        The dossier is several LinkedIn reads per lead, so the cheap stage
        earning its keep depends on it short-circuiting *before* that, not
        just before the expensive LLM call.
        """
        session = MagicMock()
        lead = _create_lead(1, "alice", headline="Student")

        cheap_decision = CheapDisqualifyDecision(disqualify=True, reason="wrong seniority")

        with (
            patch("linkedin.pipeline.qualify.fetch_next_qualification_candidate", return_value=lead),
            patch("linkedin.ml.qualifier.qualify_cheap", return_value=(cheap_decision, CHEAP_MODEL)) as mock_cheap,
            patch("linkedin.ml.qualifier.qualify_with_llm") as mock_expensive,
            patch("linkedin.ml.dossier.build_dossier_text") as mock_dossier,
            patch("linkedin.db.deals.create_disqualified_deal") as mock_disqualify,
        ):
            result = run_qualification(session)

        assert result == "alice"
        mock_cheap.assert_called_once()
        mock_expensive.assert_not_called()
        mock_dossier.assert_not_called()
        mock_disqualify.assert_called_once_with(
            session, "alice", reason="wrong seniority", qualification_stage="cheap",
        )

    def test_cheap_pass_reaches_expensive_stage(self, db):
        session = MagicMock()
        lead = _create_lead(1, "alice")

        expensive_decision = QualificationDecision(qualified=True, fit_score=4, reason="Good fit")

        with (
            patch("linkedin.pipeline.qualify.fetch_next_qualification_candidate", return_value=lead),
            patch("linkedin.ml.qualifier.qualify_cheap", return_value=(CheapDisqualifyDecision(disqualify=False, reason=""), CHEAP_MODEL)),
            patch("linkedin.ml.dossier.build_dossier_text", return_value="Lead headline: engineer at acme"),
            patch("linkedin.ml.qualifier.qualify_with_llm", return_value=(expensive_decision, EXPENSIVE_MODEL)) as mock_expensive,
            patch("linkedin.db.leads.promote_lead_to_deal") as mock_promote,
        ):
            result = run_qualification(session)

        assert result == "alice"
        mock_expensive.assert_called_once()
        mock_promote.assert_called_once_with(session, "alice", reason="Good fit", fit_score=4)

    def test_expensive_reject_disqualifies(self, db):
        session = MagicMock()
        lead = _create_lead(1, "alice")

        with (
            patch("linkedin.pipeline.qualify.fetch_next_qualification_candidate", return_value=lead),
            patch("linkedin.ml.qualifier.qualify_cheap", return_value=(CheapDisqualifyDecision(disqualify=False, reason=""), CHEAP_MODEL)),
            patch("linkedin.ml.dossier.build_dossier_text", return_value="Lead headline: engineer at acme"),
            patch("linkedin.ml.qualifier.qualify_with_llm", return_value=(QualificationDecision(qualified=False, fit_score=1, reason="Bad fit"), EXPENSIVE_MODEL)),
            patch("linkedin.db.deals.create_disqualified_deal") as mock_disqualify,
        ):
            run_qualification(session)
            mock_disqualify.assert_called_once_with(
                session, "alice", reason="Bad fit", qualification_stage="expensive",
            )

    def test_unreachable_profile_disqualifies_without_expensive_call(self, db):
        session = MagicMock()
        lead = _create_lead(1, "alice")

        with (
            patch("linkedin.pipeline.qualify.fetch_next_qualification_candidate", return_value=lead),
            patch("linkedin.ml.qualifier.qualify_cheap", return_value=(CheapDisqualifyDecision(disqualify=False, reason=""), CHEAP_MODEL)),
            patch("linkedin.ml.dossier.build_dossier_text", return_value=None),
            patch("linkedin.ml.qualifier.qualify_with_llm") as mock_expensive,
            patch("linkedin.db.deals.create_disqualified_deal") as mock_disqualify,
        ):
            run_qualification(session)

        mock_expensive.assert_not_called()
        mock_disqualify.assert_called_once_with(
            session, "alice", reason="profile not reachable", qualification_stage="expensive",
        )

    def test_disqualify_on_promote_failure(self, db):
        session = MagicMock()
        lead = _create_lead(1, "alice")

        with (
            patch("linkedin.pipeline.qualify.fetch_next_qualification_candidate", return_value=lead),
            patch("linkedin.ml.qualifier.qualify_cheap", return_value=(CheapDisqualifyDecision(disqualify=False, reason=""), CHEAP_MODEL)),
            patch("linkedin.ml.dossier.build_dossier_text", return_value="Lead headline: engineer at acme"),
            patch("linkedin.ml.qualifier.qualify_with_llm", return_value=(QualificationDecision(qualified=True, fit_score=5, reason="Good fit"), EXPENSIVE_MODEL)),
            patch("linkedin.db.leads.promote_lead_to_deal", side_effect=ValueError("no company_name")),
            patch("linkedin.db.deals.create_disqualified_deal") as mock_disqualify,
        ):
            run_qualification(session)
            mock_disqualify.assert_called_once()

    def test_returns_none_when_backlog_empty(self, db):
        session = MagicMock()
        with patch("linkedin.pipeline.qualify.fetch_next_qualification_candidate", return_value=None):
            assert run_qualification(session) is None


class TestFetchNextQualificationCandidate:
    def test_returns_oldest_first(self, fake_session):
        from linkedin.pipeline.qualify import fetch_next_qualification_candidate
        from crm.models import Lead

        for i in range(3):
            Lead.objects.create(
                linkedin_url=f"https://www.linkedin.com/in/n{i}/",
                public_identifier=f"n{i}",
            )

        candidate = fetch_next_qualification_candidate(fake_session)
        assert candidate.public_identifier == "n0"

    def test_returns_none_when_empty(self, fake_session):
        from linkedin.pipeline.qualify import fetch_next_qualification_candidate

        assert fetch_next_qualification_candidate(fake_session) is None

    def test_excludes_leads_already_dealt_in_campaign(self, fake_session):
        from crm.models import Lead, Deal
        from linkedin.enums import ProfileState
        from linkedin.pipeline.qualify import fetch_next_qualification_candidate

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/", public_identifier="alice",
        )
        Deal.objects.create(lead=lead, campaign=fake_session.campaign, state=ProfileState.QUALIFIED)

        assert fetch_next_qualification_candidate(fake_session) is None
