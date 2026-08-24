# tests/ml/test_qualifier.py
"""Tests for KitQualifier (freemium kits) and the qualification decision schemas."""
from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from linkedin.ml.qualifier import CheapDisqualifyDecision, KitQualifier, QualificationDecision


class _StubPipeline:
    """Stands in for a fitted sklearn Pipeline(StandardScaler, GPR)."""

    def predict(self, X):
        # Score = sum of the embedding — deterministic and easy to rank.
        return np.asarray(X).sum(axis=1)


class TestKitQualifierRankProfiles:
    def test_ranks_by_descending_score(self, db):
        from crm.models import Lead

        Lead.objects.create(
            pk=1, public_identifier="low", linkedin_url="https://linkedin.com/in/low/",
            embedding=np.zeros(384, dtype=np.float32).tobytes(),
        )
        Lead.objects.create(
            pk=2, public_identifier="high", linkedin_url="https://linkedin.com/in/high/",
            embedding=np.ones(384, dtype=np.float32).tobytes(),
        )

        qualifier = KitQualifier(_StubPipeline())
        profiles = [
            {"lead_id": 1, "public_identifier": "low"},
            {"lead_id": 2, "public_identifier": "high"},
        ]
        ranked = qualifier.rank_profiles(profiles, session=None)
        assert [p["public_identifier"] for p in ranked] == ["high", "low"]

    def test_empty_profiles_returns_empty(self):
        assert KitQualifier(_StubPipeline()).rank_profiles([], session=None) == []

    def test_skips_leads_with_no_embedding(self, db):
        from unittest.mock import patch

        from crm.models import Lead

        Lead.objects.create(
            pk=1, public_identifier="none_emb", linkedin_url="https://linkedin.com/in/none_emb/",
        )
        qualifier = KitQualifier(_StubPipeline())
        profiles = [{"lead_id": 1, "public_identifier": "none_emb"}]
        with patch("crm.models.lead.Lead.get_embedding", return_value=None):
            assert qualifier.rank_profiles(profiles, session=None) == []


class TestKitQualifierExplain:
    def test_no_embedding_found(self, db):
        qualifier = KitQualifier(_StubPipeline())
        explanation = qualifier.explain({"lead_id": 999}, session=None)
        assert "no embedding" in explanation.lower()


class TestQualificationDecisionSchema:
    def test_fit_score_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            QualificationDecision(qualified=True, fit_score=6, reason="too high")
        with pytest.raises(ValidationError):
            QualificationDecision(qualified=True, fit_score=0, reason="too low")

    def test_valid_decision(self):
        decision = QualificationDecision(qualified=True, fit_score=4, reason="good fit")
        assert decision.qualified is True
        assert decision.fit_score == 4


class TestCheapDisqualifyDecisionSchema:
    def test_valid_decision(self):
        decision = CheapDisqualifyDecision(disqualify=True, reason="wrong industry")
        assert decision.disqualify is True
