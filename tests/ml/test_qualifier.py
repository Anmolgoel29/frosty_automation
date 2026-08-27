# tests/ml/test_qualifier.py
"""Tests for the qualification decision schemas."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from linkedin.ml.qualifier import CheapDisqualifyDecision, QualificationDecision


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
