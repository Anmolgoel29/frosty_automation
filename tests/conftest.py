# tests/conftest.py
import pytest

from tests.factories import UserFactory


class FakeAccountSession:
    """Minimal stand-in for AccountSession — exposes django_user + campaign."""

    def __init__(self, django_user, linkedin_profile, campaign):
        self.django_user = django_user
        self.linkedin_profile = linkedin_profile
        self.campaign = campaign
        self.self_profile = {
            "first_name": "Diego",
            "last_name": "Ramirez",
            "urn": "urn:li:fsd_profile:TEST",
        }

    @property
    def campaigns(self):
        from linkedin.models import Campaign
        return list(Campaign.objects.filter(users=self.django_user))

    def ensure_browser(self):
        pass


def make_fake_session(username: str, campaign=None) -> FakeAccountSession:
    """Attach a LinkedIn account to *campaign* and wrap it in a fake session."""
    from linkedin.models import Campaign, LinkedInProfile

    user = UserFactory(username=username)

    if campaign is None:
        campaign = Campaign.objects.first()
        if campaign is None:
            campaign = Campaign.objects.create(name="LinkedIn Outreach")
    campaign.users.add(user)

    linkedin_profile, _ = LinkedInProfile.objects.get_or_create(
        user=user,
        defaults={
            "linkedin_username": f"{username}@example.com",
            "linkedin_password": "testpass",
        },
    )

    return FakeAccountSession(
        django_user=user, linkedin_profile=linkedin_profile, campaign=campaign,
    )


def sessions_map(*sessions) -> dict:
    """The ``{profile_pk: session}`` map the daemon and reconcile() take."""
    return {s.linkedin_profile.pk: s for s in sessions}


@pytest.fixture
def fake_session(db):
    """An AccountSession-like object backed by the Django test DB."""
    return make_fake_session("testuser")


@pytest.fixture
def second_session(db, fake_session):
    """A second LinkedIn account running the same campaign as ``fake_session``."""
    return make_fake_session("seconduser", campaign=fake_session.campaign)
