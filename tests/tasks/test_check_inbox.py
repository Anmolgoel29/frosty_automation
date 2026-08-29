# tests/tasks/test_check_inbox.py
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from linkedin.actions.conversations import conversation_last_activity, match_conversations
from linkedin.db.deals import set_profile_state
from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from linkedin.enums import ProfileState
from linkedin.models import Task
from linkedin.tasks.check_inbox import handle_check_inbox


def _make_connected_with_urn(session, public_id="alice", urn="urn:li:fsd_profile:ALICE"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    profile = {"public_identifier": public_id, "urn": urn, "headline": "Engineer", "positions": []}
    create_enriched_lead(session, url, profile)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, ProfileState.CONNECTED.value)
    Task.objects.all().delete()


def _make_task(session, task_type, payload):
    return Task.objects.create(
        task_type=task_type,
        linkedin_profile=session.linkedin_profile,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload=payload,
    )


def _conv(urn, last_activity_ms):
    return {
        "entityUrn": "urn:li:msg_conversation:1",
        "lastActivityAt": last_activity_ms,
        "conversationParticipants": [{"hostIdentityUrn": urn}],
    }


# ── Pure functions ──────────────────────────────────────────────


def test_match_conversations_finds_owned_participants():
    elements = [_conv("urn:a", 1000), _conv("urn:b", 2000)]
    matches = match_conversations(elements, {"urn:a"})
    assert set(matches) == {"urn:a"}


def test_match_conversations_no_match():
    elements = [_conv("urn:a", 1000)]
    assert match_conversations(elements, {"urn:z"}) == {}


def test_conversation_last_activity_parses_epoch_ms():
    ts = conversation_last_activity({"lastActivityAt": 1_700_000_000_000})
    assert ts is not None
    assert ts.year >= 2023


def test_conversation_last_activity_missing_field_returns_none():
    assert conversation_last_activity({"someOtherField": 1}) is None


# ── handle_check_inbox ──────────────────────────────────────────


@pytest.mark.django_db
class TestHandleCheckInbox:
    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    @patch("linkedin.api.messaging.fetch_conversations")
    def test_no_connected_deals_reschedules_without_calling_api(self, mock_fetch, mock_api, fake_session):
        task = _make_task(fake_session, Task.TaskType.CHECK_INBOX, {"campaign_id": fake_session.campaign.pk})
        handle_check_inbox(task, fake_session)

        mock_fetch.assert_not_called()
        assert Task.objects.filter(
            task_type=Task.TaskType.CHECK_INBOX, status=Task.Status.PENDING,
        ).exclude(pk=task.pk).exists()

    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.api.messaging.fetch_conversations")
    def test_no_matching_conversation_skips_sync(self, mock_fetch, mock_sync, mock_api, fake_session):
        _make_connected_with_urn(fake_session)
        mock_fetch.return_value = {
            "data": {"messengerConversationsBySyncToken": {"elements": [_conv("urn:someone-else", 1000)]}},
        }

        task = _make_task(fake_session, Task.TaskType.CHECK_INBOX, {"campaign_id": fake_session.campaign.pk})
        handle_check_inbox(task, fake_session)

        mock_sync.assert_not_called()

    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.api.messaging.fetch_conversations")
    def test_new_reply_syncs_and_fast_tracks_follow_up(self, mock_fetch, mock_sync, mock_api, fake_session):
        from django.contrib.contenttypes.models import ContentType

        from chat.models import ChatMessage
        from crm.models import Lead

        _make_connected_with_urn(fake_session)
        mock_fetch.return_value = {
            "data": {
                "messengerConversationsBySyncToken": {
                    "elements": [_conv("urn:li:fsd_profile:ALICE", 1_700_000_000_000)],
                },
            },
        }

        def fake_sync(session, public_id):
            lead = Lead.objects.get(public_identifier=public_id)
            ct = ContentType.objects.get_for_model(lead)
            ChatMessage.objects.create(
                content_type=ct, object_id=lead.pk, content="interested!",
                is_outgoing=False, owner=session.django_user,
                linkedin_urn="urn:test:in1", creation_date=timezone.now(),
            )
        mock_sync.side_effect = fake_sync

        task = _make_task(fake_session, Task.TaskType.CHECK_INBOX, {"campaign_id": fake_session.campaign.pk})
        handle_check_inbox(task, fake_session)

        mock_sync.assert_called_once_with(fake_session, "alice")
        assert Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP, status=Task.Status.PENDING, payload__public_id="alice",
        ).exists()
        # Self-reschedule still happens alongside the fast-tracked follow_up.
        assert Task.objects.filter(
            task_type=Task.TaskType.CHECK_INBOX, status=Task.Status.PENDING,
        ).exclude(pk=task.pk).exists()

    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.api.messaging.fetch_conversations")
    def test_human_takeover_blocks_follow_up_fast_track(self, mock_fetch, mock_sync, mock_api, fake_session):
        from django.contrib.contenttypes.models import ContentType

        from chat.models import ChatMessage
        from crm.models import Lead

        _make_connected_with_urn(fake_session)
        Lead.objects.filter(public_identifier="alice").update(human_takeover=True)

        mock_fetch.return_value = {
            "data": {
                "messengerConversationsBySyncToken": {
                    "elements": [_conv("urn:li:fsd_profile:ALICE", 1_700_000_000_000)],
                },
            },
        }

        def fake_sync(session, public_id):
            lead = Lead.objects.get(public_identifier=public_id)
            ct = ContentType.objects.get_for_model(lead)
            ChatMessage.objects.create(
                content_type=ct, object_id=lead.pk, content="interested!",
                is_outgoing=False, owner=session.django_user,
                linkedin_urn="urn:test:in2", creation_date=timezone.now(),
            )
        mock_sync.side_effect = fake_sync

        task = _make_task(fake_session, Task.TaskType.CHECK_INBOX, {"campaign_id": fake_session.campaign.pk})
        handle_check_inbox(task, fake_session)

        # Discovery still syncs the conversation so it shows up in the admin...
        mock_sync.assert_called_once()
        # ...but does not wake the AI up for a human-takeover lead.
        assert not Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP, payload__public_id="alice",
        ).exists()

    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.api.messaging.fetch_conversations")
    def test_unchanged_conversation_skips_sync(self, mock_fetch, mock_sync, mock_api, fake_session):
        from django.contrib.contenttypes.models import ContentType

        from chat.models import ChatMessage
        from crm.models import Lead

        _make_connected_with_urn(fake_session)
        lead = Lead.objects.get(public_identifier="alice")
        ct = ContentType.objects.get_for_model(lead)
        recent = timezone.now()
        ChatMessage.objects.create(
            content_type=ct, object_id=lead.pk, content="hi",
            is_outgoing=True, owner=fake_session.django_user,
            linkedin_urn="urn:test:out1", creation_date=recent,
        )

        # Remote activity older than what we already have locally.
        older_ms = int((recent - timedelta(days=1)).timestamp() * 1000)
        mock_fetch.return_value = {
            "data": {
                "messengerConversationsBySyncToken": {
                    "elements": [_conv("urn:li:fsd_profile:ALICE", older_ms)],
                },
            },
        }

        task = _make_task(fake_session, Task.TaskType.CHECK_INBOX, {"campaign_id": fake_session.campaign.pk})
        handle_check_inbox(task, fake_session)

        mock_sync.assert_not_called()
