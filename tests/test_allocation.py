# tests/test_allocation.py
"""Round-robin hand-off of the shared lead pool to the campaign's accounts."""
import pytest

from crm.models import Deal
from linkedin.db.deals import get_qualified_profiles, get_ready_to_connect_profiles, set_profile_state
from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from linkedin.enums import ProfileState
from linkedin.pipeline.allocation import allocate_ready_deals, reclaim_unreachable_deals


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
}


def _make_qualified(session, public_id):
    create_enriched_lead(session, f"https://www.linkedin.com/in/{public_id}/", SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)


def _make_ready(session, public_id):
    _make_qualified(session, public_id)
    set_profile_state(session, public_id, ProfileState.READY_TO_CONNECT.value)


def _owner_of(public_id) -> str | None:
    deal = Deal.objects.select_related("assigned_profile").get(lead__public_identifier=public_id)
    return deal.assigned_profile.linkedin_username if deal.assigned_profile else None


@pytest.mark.django_db
class TestAllocateReadyDeals:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_deals_leads_out_in_rotation(self, fake_session, second_session):
        for pid in ("alice", "bob", "carol", "dave"):
            _make_ready(fake_session, pid)

        assert allocate_ready_deals(fake_session.campaign) == 4

        first = fake_session.linkedin_profile.linkedin_username
        second = second_session.linkedin_profile.linkedin_username
        assert [_owner_of(p) for p in ("alice", "bob", "carol", "dave")] == [
            first, second, first, second,
        ]

    def test_rotation_continues_across_calls(self, fake_session, second_session):
        """The cursor is persisted, so a lead arriving later doesn't restart
        the rotation at the first account."""
        _make_ready(fake_session, "alice")
        allocate_ready_deals(fake_session.campaign)

        _make_ready(fake_session, "bob")
        fake_session.campaign.refresh_from_db()
        allocate_ready_deals(fake_session.campaign)

        assert _owner_of("alice") == fake_session.linkedin_profile.linkedin_username
        assert _owner_of("bob") == second_session.linkedin_profile.linkedin_username

    def test_never_moves_an_owned_lead(self, fake_session, second_session):
        _make_ready(fake_session, "alice")
        allocate_ready_deals(fake_session.campaign)
        owner = _owner_of("alice")

        assert allocate_ready_deals(fake_session.campaign) == 0
        assert _owner_of("alice") == owner

    def test_ignores_leads_still_in_the_shared_pool(self, fake_session, second_session):
        """Qualification is common work — only READY_TO_CONNECT is dealt out."""
        _make_qualified(fake_session, "alice")

        assert allocate_ready_deals(fake_session.campaign) == 0
        assert _owner_of("alice") is None

    def test_noop_without_active_accounts(self, fake_session):
        _make_ready(fake_session, "alice")
        fake_session.linkedin_profile.active = False
        fake_session.linkedin_profile.save(update_fields=["active"])

        assert allocate_ready_deals(fake_session.campaign) == 0
        assert _owner_of("alice") is None


@pytest.mark.django_db
class TestOwnership:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_ready_pool_is_scoped_to_one_account(self, fake_session, second_session):
        _make_ready(fake_session, "alice")
        _make_ready(fake_session, "bob")
        allocate_ready_deals(fake_session.campaign)

        mine = [p["public_identifier"] for p in get_ready_to_connect_profiles(fake_session)]
        theirs = [p["public_identifier"] for p in get_ready_to_connect_profiles(second_session)]

        assert mine == ["alice"]
        assert theirs == ["bob"]

    def test_qualification_pool_stays_shared(self, fake_session, second_session):
        """Both accounts see the same un-owned pool before the hand-off."""
        _make_qualified(fake_session, "alice")

        assert [p["public_identifier"] for p in get_qualified_profiles(fake_session)] == ["alice"]
        assert [p["public_identifier"] for p in get_qualified_profiles(second_session)] == ["alice"]

    def test_reaching_out_claims_an_unowned_lead(self, fake_session, second_session):
        """Seeded leads never pass through allocation — the account that sends
        the invite still ends up owning them."""
        _make_qualified(second_session, "alice")

        set_profile_state(second_session, "alice", ProfileState.PENDING.value)

        assert _owner_of("alice") == second_session.linkedin_profile.linkedin_username

    def test_ownership_survives_later_transitions(self, fake_session, second_session):
        _make_ready(fake_session, "alice")
        allocate_ready_deals(fake_session.campaign)
        owner = _owner_of("alice")

        # A send failure bounces the deal back to QUALIFIED; it must not
        # re-enter the shared pool and cross over to the other account.
        set_profile_state(fake_session, "alice", ProfileState.QUALIFIED.value)
        assert _owner_of("alice") == owner

        set_profile_state(fake_session, "alice", ProfileState.READY_TO_CONNECT.value)
        assert allocate_ready_deals(fake_session.campaign) == 0
        assert _owner_of("alice") == owner


@pytest.mark.django_db
class TestReclaim:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_returns_uncontacted_leads_from_a_retired_account(self, fake_session, second_session):
        _make_ready(fake_session, "alice")
        _make_ready(fake_session, "bob")
        allocate_ready_deals(fake_session.campaign)
        assert _owner_of("bob") == second_session.linkedin_profile.linkedin_username

        second_session.linkedin_profile.active = False
        second_session.linkedin_profile.save(update_fields=["active"])

        assert reclaim_unreachable_deals(fake_session.campaign) == 1
        assert _owner_of("bob") is None
        assert _owner_of("alice") == fake_session.linkedin_profile.linkedin_username

    def test_leaves_contacted_leads_stranded(self, fake_session, second_session):
        """A sent invite can only be seen by the account that sent it, so a
        contacted lead is never handed to anyone else."""
        _make_qualified(second_session, "alice")
        set_profile_state(second_session, "alice", ProfileState.PENDING.value)

        second_session.linkedin_profile.active = False
        second_session.linkedin_profile.save(update_fields=["active"])

        assert reclaim_unreachable_deals(fake_session.campaign) == 0
        assert _owner_of("alice") == second_session.linkedin_profile.linkedin_username
