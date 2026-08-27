# tests/db/test_profiles.py
import pytest

from linkedin.db.deals import (
    set_profile_state,
    get_qualified_profiles,
    create_disqualified_deal,
)
from linkedin.db.leads import (
    create_enriched_lead,
    promote_lead_to_deal,
    get_leads_for_qualification,
    lead_exists,
)
from linkedin.url_utils import url_to_public_id, public_id_to_url
from linkedin.enums import ProfileState


# ── url_to_public_id (pure function) ──

class TestUrlToPublicId:
    def test_standard_url(self):
        assert url_to_public_id("https://www.linkedin.com/in/johndoe/") == "johndoe"

    def test_url_without_trailing_slash(self):
        assert url_to_public_id("https://www.linkedin.com/in/johndoe") == "johndoe"

    def test_url_with_query_params(self):
        assert url_to_public_id("https://www.linkedin.com/in/johndoe?foo=bar") == "johndoe"

    def test_url_with_extra_path_segments(self):
        assert url_to_public_id("https://www.linkedin.com/in/johndoe/detail/contact-info/") == "johndoe"

    def test_percent_encoded_id(self):
        assert url_to_public_id("https://www.linkedin.com/in/john%20doe/") == "john doe"

    def test_empty_url_returns_none(self):
        assert url_to_public_id("") is None

    def test_non_profile_url_returns_none(self):
        assert url_to_public_id("https://www.linkedin.com/feed/") is None

    def test_only_domain_returns_none(self):
        assert url_to_public_id("https://www.linkedin.com/") is None


# ── public_id_to_url (pure function) ──

class TestPublicIdToUrl:
    def test_standard_id(self):
        assert public_id_to_url("johndoe") == "https://www.linkedin.com/in/johndoe/"

    def test_empty_id(self):
        assert public_id_to_url("") == ""

    def test_id_with_slashes_stripped(self):
        assert public_id_to_url("/johndoe/") == "https://www.linkedin.com/in/johndoe/"


# ── DB operations using fake_session (Django ORM) ──

SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
    "urn": "urn:li:fsd_profile:ABC123",
}


@pytest.mark.django_db
class TestLeadExists:
    def test_exists_after_create(self, fake_session):
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        assert lead_exists("https://www.linkedin.com/in/alice/") is True

    def test_not_exists(self, fake_session):
        assert lead_exists("https://www.linkedin.com/in/nobody/") is False

    def test_invalid_url(self, fake_session):
        assert lead_exists("https://linkedin.com/feed/") is False


@pytest.mark.django_db
class TestCreateEnrichedLead:
    def test_creates_lead_and_caches_urn(self, fake_session):
        from crm.models import Lead
        pk = create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        assert pk is not None
        lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/alice/")
        assert lead.public_identifier == "alice"
        assert lead.urn == "urn:li:fsd_profile:ABC123"

    def test_returns_none_for_duplicate(self, fake_session):
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        pk2 = create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        assert pk2 is None

    def test_no_deal_created(self, fake_session):
        from crm.models import Deal
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        assert Deal.objects.count() == 0


@pytest.mark.django_db
class TestEnsureCoarseFields:
    """One-shot repair of leads whose headline/about predate those columns.

    Blank coarse fields make the cheap prefilter a no-op (it is told to pass
    through when they're empty), so those rows must be re-scraped once.
    """

    def _legacy_lead(self, **overrides):
        """A Lead as the pre-cascade code left it: no coarse fields, no marker."""
        from crm.models import Lead
        fields = {
            "linkedin_url": "https://www.linkedin.com/in/alice/",
            "public_identifier": "alice",
            **overrides,
        }
        return Lead.objects.create(**fields)

    def test_rescrapes_and_stamps_blank_legacy_lead(self, fake_session):
        from unittest.mock import patch
        from linkedin.db.leads import ensure_coarse_fields

        lead = self._legacy_lead()
        scraped = {**SAMPLE_PROFILE, "summary": "I run an agency."}

        with patch("crm.models.Lead.get_profile", return_value=scraped) as mock_scrape:
            assert ensure_coarse_fields(fake_session, lead) is True

        mock_scrape.assert_called_once()
        lead.refresh_from_db()
        assert lead.headline == "Engineer"
        assert lead.about == "I run an agency."
        assert lead.coarse_scraped_at is not None

    def test_stamped_lead_is_never_rescraped_again(self, fake_session):
        """The marker — not the field contents — is what stops the repair.

        A profile with genuinely no headline and no About must not be
        re-scraped on every pass through qualification.
        """
        from unittest.mock import patch
        from linkedin.db.leads import ensure_coarse_fields

        lead = self._legacy_lead()
        with patch("crm.models.Lead.get_profile", return_value={"positions": []}):
            ensure_coarse_fields(fake_session, lead)

        lead.refresh_from_db()
        assert lead.coarse_scraped_at is not None
        assert lead.headline == "" and lead.about == ""

        with patch("crm.models.Lead.get_profile") as mock_scrape:
            assert ensure_coarse_fields(fake_session, lead) is False
        mock_scrape.assert_not_called()

    def test_lead_with_existing_coarse_data_is_stamped_without_scraping(self, fake_session):
        from unittest.mock import patch
        from linkedin.db.leads import ensure_coarse_fields

        lead = self._legacy_lead(headline="Founder")

        with patch("crm.models.Lead.get_profile") as mock_scrape:
            assert ensure_coarse_fields(fake_session, lead) is True

        mock_scrape.assert_not_called()
        lead.refresh_from_db()
        assert lead.coarse_scraped_at is not None

    def test_unreachable_profile_leaves_marker_null(self, fake_session):
        """Private/deleted profiles fall through to the dossier stage, which
        disqualifies them on the same unreachable profile."""
        from unittest.mock import patch
        from linkedin.db.leads import ensure_coarse_fields

        lead = self._legacy_lead()

        with patch("crm.models.Lead.get_profile", return_value=None):
            assert ensure_coarse_fields(fake_session, lead) is False

        lead.refresh_from_db()
        assert lead.coarse_scraped_at is None

    def test_newly_discovered_leads_are_stamped_at_creation(self, fake_session):
        from crm.models import Lead

        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        lead = Lead.objects.get(public_identifier="alice")
        assert lead.coarse_scraped_at is not None

    def test_stamped_lead_needs_no_scrape_in_qualification(self, fake_session):
        """A lead created under the new standard never triggers the repair."""
        from unittest.mock import patch
        from crm.models import Lead
        from linkedin.db.leads import ensure_coarse_fields

        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        lead = Lead.objects.get(public_identifier="alice")

        with patch("crm.models.Lead.get_profile") as mock_scrape:
            assert ensure_coarse_fields(fake_session, lead) is True
        mock_scrape.assert_not_called()


@pytest.mark.django_db
class TestPromoteLeadToDeal:
    def test_creates_deal(self, fake_session):
        from crm.models import Deal
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        deal = promote_lead_to_deal(fake_session, "alice")
        assert deal is not None
        assert deal.state == ProfileState.QUALIFIED
        assert Deal.objects.count() == 1

@pytest.mark.django_db
class TestGetLeadsForQualification:
    def test_returns_enriched_leads(self, fake_session):
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        leads = get_leads_for_qualification(fake_session)
        assert len(leads) == 1
        assert leads[0]["public_identifier"] == "alice"
        assert leads[0]["lead_id"] is not None

    def test_excludes_disqualified(self, fake_session):
        """disqualified=True (self-profile) leads are excluded."""
        from crm.models import Lead
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        Lead.objects.filter(linkedin_url="https://www.linkedin.com/in/alice/").update(disqualified=True)
        leads = get_leads_for_qualification(fake_session)
        assert len(leads) == 0

    def test_excludes_promoted(self, fake_session):
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        promote_lead_to_deal(fake_session, "alice")
        leads = get_leads_for_qualification(fake_session)
        assert len(leads) == 0

    def test_multiple_leads(self, fake_session):
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/bob/",
            {**SAMPLE_PROFILE, "urn": "urn:li:fsd_profile:BOB456"},
        )
        assert len(get_leads_for_qualification(fake_session)) == 2


@pytest.mark.django_db
class TestSetProfileState:
    def test_set_state_on_deal(self, fake_session):
        from crm.models import Deal
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        promote_lead_to_deal(fake_session, "alice")
        set_profile_state(fake_session, "alice", ProfileState.PENDING.value)
        deal = Deal.objects.get(lead__linkedin_url="https://www.linkedin.com/in/alice/")
        assert deal.state == ProfileState.PENDING

    def test_set_state_requires_deal(self, fake_session):
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        with pytest.raises(ValueError, match="No Deal"):
            set_profile_state(fake_session, "alice", ProfileState.QUALIFIED.value)


# ── get_qualified_profiles (Deals at "Qualified" state) ──

@pytest.mark.django_db
class TestGetQualifiedProfiles:
    def _promote(self, session, public_id="alice"):
        url = f"https://www.linkedin.com/in/{public_id}/"
        create_enriched_lead(session, url, SAMPLE_PROFILE)
        promote_lead_to_deal(session, public_id)

    def test_returns_qualified(self, fake_session):
        self._promote(fake_session)
        profiles = get_qualified_profiles(fake_session)
        assert len(profiles) == 1
        assert profiles[0]["public_identifier"] == "alice"

    def test_excludes_other_states(self, fake_session):
        self._promote(fake_session)
        set_profile_state(fake_session, "alice", ProfileState.PENDING.value)
        profiles = get_qualified_profiles(fake_session)
        assert len(profiles) == 0


# ── create_disqualified_deal ──

@pytest.mark.django_db
class TestCreateDisqualifiedDeal:
    def test_creates_failed_deal(self, fake_session):
        from crm.models import Deal, Outcome
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        deal = create_disqualified_deal(fake_session, "alice", reason="Bad fit")
        assert deal is not None
        assert deal.state == ProfileState.FAILED
        assert deal.outcome == Outcome.WRONG_FIT
        assert deal.reason == "Bad fit"

    def test_excludes_from_qualification(self, fake_session):
        """A lead with a disqualified Deal in this campaign is excluded."""
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        create_disqualified_deal(fake_session, "alice", reason="Bad fit")
        leads = get_leads_for_qualification(fake_session)
        assert len(leads) == 0

    def test_returns_existing_deal(self, fake_session):
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        deal1 = create_disqualified_deal(fake_session, "alice", reason="Bad fit")
        deal2 = create_disqualified_deal(fake_session, "alice", reason="Other")
        assert deal1.pk == deal2.pk


# ── Multi-campaign qualification scoping ──

@pytest.mark.django_db
class TestMultiCampaignQualification:
    def _make_other_session(self, fake_session):
        """Create a second campaign/session."""
        from linkedin.models import Campaign
        from tests.conftest import FakeAccountSession

        campaign2 = Campaign.objects.create(name="Other Campaign")
        campaign2.users.add(fake_session.django_user)
        return FakeAccountSession(
            django_user=fake_session.django_user,
            linkedin_profile=fake_session.linkedin_profile,
            campaign=campaign2,
        )

    def test_disqualified_in_other_campaign_still_eligible(self, fake_session):
        """A lead rejected by campaign A is still eligible for campaign B."""
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        create_disqualified_deal(fake_session, "alice", reason="Bad fit")

        other_session = self._make_other_session(fake_session)
        leads = get_leads_for_qualification(other_session)
        assert len(leads) == 1
        assert leads[0]["public_identifier"] == "alice"

    def test_promoted_in_other_campaign_still_eligible(self, fake_session):
        """A lead promoted in campaign A is still eligible for campaign B."""
        create_enriched_lead(
            fake_session,
            "https://www.linkedin.com/in/alice/",
            SAMPLE_PROFILE,
        )
        promote_lead_to_deal(fake_session, "alice")

        other_session = self._make_other_session(fake_session)
        leads = get_leads_for_qualification(other_session)
        assert len(leads) == 1
