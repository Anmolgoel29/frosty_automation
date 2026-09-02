"""Tests for linkedin/db/summaries.py — the profile fact-list boundary.

There is no chat summariser to test: conversation history is never compressed
(see tests/agents/test_follow_up.py, which covers the verbatim transcript the
follow-up agent reads instead).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelMessage, ModelResponse

from tests.factories import LeadFactory, DealFactory


FAKE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Senior Engineer at Acme",
    "positions": [{"company_name": "Acme Corp", "title": "Senior Engineer"}],
    "urn": "urn:li:fsd_profile:ABC123",
}


def _capturing_function_model(captured: dict, output: dict) -> FunctionModel:
    """FunctionModel that records the messages it receives, then yields *output*."""
    from pydantic_ai.messages import ToolCallPart

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["messages"] = messages
        captured["output_tools"] = info.output_tools
        tool_name = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=output)])

    return FunctionModel(_respond)


@pytest.fixture
def deal_with_lead(db, fake_session):
    lead = LeadFactory(
        public_identifier="alice",
        linkedin_url="https://www.linkedin.com/in/alice/",
    )
    return DealFactory(lead=lead, campaign=fake_session.campaign)


class TestExtractFacts:
    def test_empty_input_returns_empty_list(self, db):
        from linkedin.db.summaries import extract_facts

        assert extract_facts("") == []
        assert extract_facts("   \n  ") == []

    def test_invokes_llm_with_structured_output(self, db):
        from linkedin.db.summaries import extract_facts

        captured: dict = {}
        model = _capturing_function_model(
            captured, {"facts": ["Works at Acme.", "Based in Berlin."]},
        )
        with patch("linkedin.llm.get_llm_model", return_value=model):
            facts = extract_facts(
                "Alice works at Acme. She lives in Berlin.",
                context="Campaign objective: hire engineers",
            )

        assert facts == ["Works at Acme.", "Based in Berlin."]
        # The system prompt carries the vendored prompt + context; the user
        # message carries the input text.
        rendered = "\n".join(
            part.content
            for msg in captured["messages"]
            for part in msg.parts
            if hasattr(part, "content") and isinstance(part.content, str)
        )
        assert "Campaign objective" in rendered
        assert "Alice works at Acme" in rendered


class TestMaterializeProfileSummary:
    def test_noop_when_already_built(self, db, deal_with_lead):
        from linkedin.db.summaries import materialize_profile_summary_if_missing

        deal_with_lead.profile_summary = {"facts": ["already built"]}
        deal_with_lead.save(update_fields=["profile_summary"])

        with patch("linkedin.db.summaries.extract_facts") as mock_extract:
            materialize_profile_summary_if_missing(deal_with_lead, None)

        mock_extract.assert_not_called()

    def test_builds_via_rescrape_and_persists(self, db, fake_session, deal_with_lead):
        from linkedin.db.summaries import materialize_profile_summary_if_missing

        with patch.object(deal_with_lead.lead, "get_profile", return_value=FAKE_PROFILE) as mock_refresh, \
             patch("linkedin.db.summaries.extract_facts",
                   return_value=["Senior Engineer at Acme.", "URN ABC123."]) as mock_extract:
            materialize_profile_summary_if_missing(deal_with_lead, fake_session)

        mock_refresh.assert_called_once_with(fake_session)
        mock_extract.assert_called_once()
        deal_with_lead.refresh_from_db()
        assert deal_with_lead.profile_summary == {
            "facts": ["Senior Engineer at Acme.", "URN ABC123."]
        }

    def test_empty_profile_logs_and_skips(self, db, fake_session, deal_with_lead, caplog):
        from linkedin.db.summaries import materialize_profile_summary_if_missing

        with patch.object(deal_with_lead.lead, "get_profile", return_value=None), \
             patch("linkedin.db.summaries.extract_facts") as mock_extract:
            materialize_profile_summary_if_missing(deal_with_lead, fake_session)

        mock_extract.assert_not_called()
        deal_with_lead.refresh_from_db()
        assert deal_with_lead.profile_summary is None


class TestNoChatSummariser:
    """The chat summariser is gone on purpose — guard against it creeping back."""

    def test_summaries_module_exposes_no_chat_helpers(self):
        import linkedin.db.summaries as summaries

        for name in ("update_chat_summary", "reconcile_facts", "seller_name_from"):
            assert not hasattr(summaries, name), (
                f"{name} is back — conversation history must reach the agent verbatim"
            )

    def test_sync_conversation_does_not_summarise(self, db, fake_session):
        """sync_conversation writes ChatMessage rows and nothing derived."""
        from linkedin.db import chat

        assert not hasattr(chat, "_update_deal_chat_summary")
