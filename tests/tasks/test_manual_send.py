# tests/tasks/test_manual_send.py
from unittest.mock import patch

import pytest
from django.utils import timezone

from linkedin.db.deals import set_profile_state
from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Task
from linkedin.tasks.manual_send import handle_manual_send


def _make_connected(session, public_id="alice"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    profile = {"public_identifier": public_id, "headline": "Engineer", "positions": []}
    create_enriched_lead(session, url, profile)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, ProfileState.CONNECTED.value)
    Task.objects.all().delete()


def _make_task(session, payload):
    return Task.objects.create(
        task_type=Task.TaskType.MANUAL_SEND,
        linkedin_profile=session.linkedin_profile,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload=payload,
    )


@pytest.mark.django_db
class TestHandleManualSend:
    def test_reschedules_on_rate_limit(self, fake_session):
        _make_connected(fake_session)
        fake_session.linkedin_profile.follow_up_daily_limit = 0
        fake_session.linkedin_profile.save(update_fields=["follow_up_daily_limit"])
        from crm.models import Lead
        lead = Lead.objects.get(public_identifier="alice")

        task = _make_task(fake_session, {"lead_id": lead.pk, "message": "hi"})
        handle_manual_send(task, fake_session)

        assert Task.objects.filter(
            task_type=Task.TaskType.MANUAL_SEND, status=Task.Status.PENDING,
        ).exclude(pk=task.pk).exists()
        assert not lead.__class__.objects.get(pk=lead.pk).human_takeover

    def test_noop_when_lead_missing(self, fake_session):
        task = _make_task(fake_session, {"lead_id": 999999, "message": "hi"})
        handle_manual_send(task, fake_session)
        assert ActionLog.objects.count() == 0

    @patch("linkedin.db.chat.tag_last_outgoing")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.actions.message.send_raw_message", return_value=False)
    def test_send_failure_does_not_record_action_or_takeover(self, mock_send, mock_sync, mock_tag, fake_session):
        from crm.models import Lead

        _make_connected(fake_session)
        lead = Lead.objects.get(public_identifier="alice")

        task = _make_task(fake_session, {"lead_id": lead.pk, "message": "hi"})
        handle_manual_send(task, fake_session)

        mock_send.assert_called_once()
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 0
        assert not Lead.objects.get(pk=lead.pk).human_takeover

    @patch("linkedin.db.chat.tag_last_outgoing")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    def test_success_records_action_and_sets_takeover(self, mock_send, mock_sync, mock_tag, fake_session):
        from crm.models import Lead

        _make_connected(fake_session)
        lead = Lead.objects.get(public_identifier="alice")

        task = _make_task(fake_session, {"lead_id": lead.pk, "message": "Hello!"})
        handle_manual_send(task, fake_session)

        mock_send.assert_called_once()
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 1
        assert Lead.objects.get(pk=lead.pk).human_takeover
        # Synced once before sending, once after (to capture the real linkedin_urn).
        assert mock_sync.call_count == 2
        mock_tag.assert_called_once_with("alice", "human")

    @patch("linkedin.db.chat.tag_last_outgoing")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    def test_success_does_not_re_set_already_true_takeover(self, mock_send, mock_sync, mock_tag, fake_session):
        """webadmin already sets human_takeover eagerly at click time — the
        handler's own set is belt-and-suspenders and should be a no-op save
        when it's already True, not an unconditional rewrite."""
        from crm.models import Lead

        _make_connected(fake_session)
        Lead.objects.filter(public_identifier="alice").update(human_takeover=True)
        lead = Lead.objects.get(public_identifier="alice")

        task = _make_task(fake_session, {"lead_id": lead.pk, "message": "Hello!"})
        handle_manual_send(task, fake_session)

        assert Lead.objects.get(pk=lead.pk).human_takeover
