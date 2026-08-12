# tests/test_client_isolation.py
"""One database, many clients — and no leakage between them."""
import pytest

from linkedin.db.leads import (
    create_enriched_lead,
    disqualify_lead,
    get_leads_for_qualification,
    lead_exists,
    promote_lead_to_deal,
)
from linkedin.enums import ProfileState
from tests.conftest import make_session
from tests.factories import CampaignFactory

SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
    "public_identifier": "alice",
}

ALICE_URL = "https://www.linkedin.com/in/alice/"


def _public_ids(session):
    return {p["public_identifier"] for p in get_leads_for_qualification(session)}


@pytest.mark.django_db
class TestLeadsArePerClient:
    def test_same_person_gets_a_row_per_client(self, fake_session, other_client_session):
        from crm.models import Lead

        mine = create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE)
        theirs = create_enriched_lead(other_client_session, ALICE_URL, SAMPLE_PROFILE)

        assert mine is not None and theirs is not None
        assert mine != theirs
        assert Lead.objects.filter(public_identifier="alice").count() == 2

    def test_duplicate_within_one_client_is_skipped(self, fake_session):
        assert create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE) is not None
        assert create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE) is None

    def test_lead_exists_is_scoped_to_the_client(self, fake_session, other_client_session):
        create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE)

        assert lead_exists(fake_session.client, ALICE_URL) is True
        assert lead_exists(other_client_session.client, ALICE_URL) is False

    def test_qualification_pool_excludes_other_clients_leads(
        self, fake_session, other_client_session,
    ):
        create_enriched_lead(other_client_session, ALICE_URL, SAMPLE_PROFILE)

        assert _public_ids(fake_session) == set()
        assert _public_ids(other_client_session) == {"alice"}

    def test_disqualifying_for_one_client_leaves_the_other_alone(
        self, fake_session, other_client_session,
    ):
        create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE)
        create_enriched_lead(other_client_session, ALICE_URL, SAMPLE_PROFILE)

        disqualify_lead(fake_session.client, "alice")

        assert _public_ids(fake_session) == set()
        assert _public_ids(other_client_session) == {"alice"}


@pytest.mark.django_db
class TestCrossCampaignOverlapWithinAClient:
    """The original complaint: two of a client's profiles at the same door."""

    def test_lead_worked_by_one_campaign_is_hidden_from_another(self, fake_session):
        client = fake_session.client
        other_campaign = CampaignFactory(client=client, name="Second campaign")
        other = make_session(campaign=other_campaign)

        create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE)
        assert _public_ids(other) == {"alice"}

        # First campaign takes the prospect on.
        promote_lead_to_deal(fake_session, "alice")

        assert _public_ids(other) == set()

    def test_a_failed_deal_does_not_reserve_the_lead(self, fake_session):
        """Wrong for one campaign says nothing about the next."""
        from crm.models import Deal

        client = fake_session.client
        other = make_session(campaign=CampaignFactory(client=client, name="Second campaign"))

        create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE)
        deal = promote_lead_to_deal(fake_session, "alice")
        Deal.objects.filter(pk=deal.pk).update(state=ProfileState.FAILED)

        assert _public_ids(other) == {"alice"}

    def test_own_campaign_excludes_its_own_dealt_leads(self, fake_session):
        create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE)
        promote_lead_to_deal(fake_session, "alice")

        assert _public_ids(fake_session) == set()


@pytest.mark.django_db
class TestSharedPoolWithinACampaign:
    def test_both_profiles_see_the_same_qualification_pool(
        self, fake_session, second_session,
    ):
        """Two profiles on one campaign qualify into one pool, not two."""
        create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE)

        assert _public_ids(fake_session) == {"alice"}
        assert _public_ids(second_session) == {"alice"}

    def test_promoting_removes_it_from_both(self, fake_session, second_session):
        create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE)
        promote_lead_to_deal(fake_session, "alice")

        assert _public_ids(fake_session) == set()
        assert _public_ids(second_session) == set()

    def test_concurrent_promotion_is_idempotent(self, fake_session, second_session):
        """Both workers racing to promote the same lead yield one deal."""
        from crm.models import Deal

        create_enriched_lead(fake_session, ALICE_URL, SAMPLE_PROFILE)

        first = promote_lead_to_deal(fake_session, "alice")
        second = promote_lead_to_deal(second_session, "alice")

        assert first.pk == second.pk
        assert Deal.objects.filter(campaign=fake_session.campaign).count() == 1


@pytest.mark.django_db
class TestSearchKeywordsArePerCampaign:
    def test_two_profiles_never_take_the_same_keyword(self, fake_session, second_session):
        from linkedin.models import SearchKeyword
        from linkedin.pipeline.search import _claim_keyword

        campaign = fake_session.campaign
        SearchKeyword.objects.create(campaign=campaign, keyword="one")
        SearchKeyword.objects.create(campaign=campaign, keyword="two")

        first = _claim_keyword(campaign)
        second = _claim_keyword(campaign)

        assert {first, second} == {"one", "two"}
        assert _claim_keyword(campaign) is None

    def test_keywords_do_not_cross_campaigns(self, fake_session, other_client_session):
        from linkedin.models import SearchKeyword
        from linkedin.pipeline.search import _claim_keyword

        SearchKeyword.objects.create(campaign=fake_session.campaign, keyword="mine")

        assert _claim_keyword(other_client_session.campaign) is None
        assert _claim_keyword(fake_session.campaign) == "mine"
