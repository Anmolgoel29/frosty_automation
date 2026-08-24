# tests/test_ready_pool.py
import pytest

from linkedin.db.deals import set_profile_state
from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from linkedin.enums import ProfileState
from linkedin.pipeline.allocation import allocate_ready_deals
from linkedin.pipeline.ready_pool import promote_to_ready, find_ready_candidate


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
}


def _make_qualified(session, public_id="alice", fit_score=None):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id, fit_score=fit_score)


@pytest.mark.django_db
class TestPromoteToReady:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_promotes_above_threshold(self, fake_session):
        _make_qualified(fake_session, "alice", fit_score=5)
        _make_qualified(fake_session, "bob", fit_score=3)

        count = promote_to_ready(fake_session, threshold=4)

        assert count == 1

        from crm.models import Deal
        alice_deal = Deal.objects.get(lead__linkedin_url="https://www.linkedin.com/in/alice/")
        bob_deal = Deal.objects.get(lead__linkedin_url="https://www.linkedin.com/in/bob/")
        assert alice_deal.state == ProfileState.READY_TO_CONNECT
        assert bob_deal.state == ProfileState.QUALIFIED

    def test_reevaluates_existing_backlog_on_every_call(self, fake_session):
        """Lowering the threshold mid-campaign promotes leads already sitting
        QUALIFIED, not just future labels — this is the whole point of
        keeping it a live re-evaluation instead of a one-shot check."""
        _make_qualified(fake_session, "bob", fit_score=3)

        assert promote_to_ready(fake_session, threshold=4) == 0
        assert promote_to_ready(fake_session, threshold=3) == 1

    def test_returns_zero_when_no_fit_score(self, fake_session):
        _make_qualified(fake_session, "alice", fit_score=None)
        assert promote_to_ready(fake_session, threshold=4) == 0

    def test_returns_zero_on_empty_pool(self, fake_session):
        assert promote_to_ready(fake_session, threshold=4) == 0


@pytest.mark.django_db
class TestGetReadyCandidate:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_returns_none_when_empty(self, fake_session):
        assert find_ready_candidate(fake_session) is None

    def test_returns_top_ranked_by_fit_score(self, fake_session):
        _make_qualified(fake_session, "alice", fit_score=3)
        _make_qualified(fake_session, "bob", fit_score=5)
        for pid in ("alice", "bob"):
            set_profile_state(fake_session, pid, ProfileState.READY_TO_CONNECT.value)
        # The ready pool is per-account: a lead is only visible here once the
        # round-robin has handed it to this account.
        allocate_ready_deals(fake_session.campaign)

        result = find_ready_candidate(fake_session)
        assert result is not None
        assert result["public_identifier"] == "bob"

    def test_ignores_leads_not_yet_allocated(self, fake_session):
        _make_qualified(fake_session, "alice", fit_score=5)
        set_profile_state(fake_session, "alice", ProfileState.READY_TO_CONNECT.value)

        assert find_ready_candidate(fake_session) is None
