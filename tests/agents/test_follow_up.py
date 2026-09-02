"""Tests for the follow-up agent context builder + Jinja template."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.factories import LeadFactory, DealFactory


@pytest.fixture
def deal_with_summaries(db, fake_session):
    lead = LeadFactory(public_identifier="alice")
    return DealFactory(
        lead=lead,
        campaign=fake_session.campaign,
        profile_summary={"facts": [
            "Senior engineer at Acme Corp.",
            "Based in Berlin, Germany.",
            "Speaks English and German.",
        ]},
    )


def _msg(content, is_outgoing, creation_date=None):
    m = MagicMock()
    m.content = content
    m.is_outgoing = is_outgoing
    m.creation_date = creation_date
    return m


def _seed_messages(lead, owner, count, base):
    """Create `count` alternating ChatMessages one minute apart from `base`."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType
    from datetime import timedelta

    ct = ContentType.objects.get_for_model(lead)
    for i in range(count):
        ChatMessage.objects.create(
            content_type=ct, object_id=lead.pk,
            content=f"msg-{i}",
            is_outgoing=(i % 2 == 0),
            owner=owner,
            linkedin_urn=f"urn:msg:{i}",
            creation_date=base + timedelta(minutes=i),
        )


class TestRenderSystemPrompt:
    def test_includes_profile_facts_and_full_transcript(self, db, fake_session, deal_with_summaries):
        from linkedin.agents.follow_up import _render_system_prompt

        # Stub session.self_profile so the prompt builder works without a browser.
        fake_session.self_profile = {"first_name": "Bob", "last_name": "Builder", "urn": "urn:li:fsd_profile:SELF"}

        messages = [_msg("Hi, what do you do?", is_outgoing=True), _msg("Sales tooling.", is_outgoing=False)]
        prompt = _render_system_prompt(fake_session, deal_with_summaries, messages)

        # Profile facts appear under the lead-knowledge block.
        assert "Senior engineer at Acme Corp." in prompt
        assert "Based in Berlin, Germany." in prompt
        # Every message is present verbatim, speaker-tagged and numbered.
        assert "[1] YOU (Bob Builder)" in prompt
        assert "Hi, what do you do?" in prompt
        assert "[2] LEAD" in prompt
        assert "Sales tooling." in prompt
        # The transcript is explicitly delimited.
        assert "--- BEGIN TRANSCRIPT ---" in prompt
        assert "--- END TRANSCRIPT ---" in prompt
        # No summarised-chat block survives.
        assert "What We Know From the Conversation" not in prompt
        # The legacy flat fields are gone.
        assert "Headline:" not in prompt
        assert "Company:" not in prompt

    def test_handles_empty_thread_gracefully(self, db, fake_session):
        from linkedin.agents.follow_up import _render_system_prompt

        lead = LeadFactory(public_identifier="bob")
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)
        fake_session.self_profile = {"first_name": "Bob", "last_name": "Builder", "urn": "urn:li:fsd_profile:SELF"}

        prompt = _render_system_prompt(fake_session, deal, [])

        # Renders without crashing and shows the empty placeholders.
        assert "(none yet)" in prompt
        assert "this thread is empty" in prompt


class TestFormatTranscript:
    def test_multiline_body_is_indented_under_its_turn(self):
        from django.utils import timezone
        from linkedin.agents.follow_up import _format_transcript

        now = timezone.now()
        out = _format_transcript(
            [_msg("line one\nline two", is_outgoing=False)], now, self_name="Bob",
        )

        assert "    line one" in out
        assert "    line two" in out

    def test_omitted_count_is_announced(self):
        from django.utils import timezone
        from linkedin.agents.follow_up import _format_transcript

        now = timezone.now()
        out = _format_transcript(
            [_msg("hi", is_outgoing=True)], now, self_name="Bob", omitted=7,
        )

        assert "7 older message(s) omitted" in out


class TestLoadConversation:
    def test_returns_every_message_in_chronological_order(self, db, fake_session):
        from django.utils import timezone

        from linkedin.agents.follow_up import _load_conversation

        lead = LeadFactory(public_identifier="alice")
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)
        _seed_messages(lead, fake_session.django_user, 25, timezone.now())

        messages, omitted = _load_conversation(deal)

        # Nothing is dropped and order is oldest-first.
        assert omitted == 0
        contents = [m.content for m in messages]
        assert contents == [f"msg-{i}" for i in range(25)]

    def test_caps_at_max_and_reports_the_remainder(self, db, fake_session, monkeypatch):
        from django.utils import timezone

        from linkedin.agents import follow_up

        monkeypatch.setattr(follow_up, "MAX_TRANSCRIPT_MESSAGES", 5)

        lead = LeadFactory(public_identifier="carol")
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)
        _seed_messages(lead, fake_session.django_user, 12, timezone.now())

        messages, omitted = follow_up._load_conversation(deal)

        # Keeps the newest 5, still chronological, and says 7 were dropped.
        assert [m.content for m in messages] == [f"msg-{i}" for i in range(7, 12)]
        assert omitted == 7
