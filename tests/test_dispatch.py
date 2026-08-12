# tests/test_dispatch.py
"""Round-robin dispatch of a campaign's shared prospect pool."""
import pytest

from linkedin.enums import ProfileState
from linkedin.models import ActionLog
from linkedin.pipeline.dispatch import dispatch_campaign_pool, release_stale_assignments
from tests.conftest import make_session
from tests.factories import DealFactory, LeadFactory, LinkedInProfileFactory


def _pool(campaign, n, state=ProfileState.READY_TO_CONNECT):
    """Put *n* unassigned deals in the campaign's pool, oldest first."""
    return [
        DealFactory(
            lead=LeadFactory(client=campaign.client),
            campaign=campaign,
            state=state,
        )
        for _ in range(n)
    ]


def _assignments(campaign):
    """Deal → assigned profile id, in pool order."""
    from crm.models import Deal

    return list(
        Deal.objects.filter(campaign=campaign)
        .order_by("creation_date", "pk")
        .values_list("assigned_profile_id", flat=True),
    )


@pytest.mark.django_db
class TestRoundRobin:
    def test_alternates_between_profiles(self, fake_session, second_session):
        campaign = fake_session.campaign
        a, b = fake_session.linkedin_profile, second_session.linkedin_profile
        _pool(campaign, 4)

        dispatch_campaign_pool(campaign)

        assert _assignments(campaign) == [a.pk, b.pk, a.pk, b.pk]

    def test_cursor_persists_across_dispatches(self, fake_session, second_session):
        """A second dispatch resumes the rotation instead of restarting it."""
        campaign = fake_session.campaign
        a, b = fake_session.linkedin_profile, second_session.linkedin_profile

        _pool(campaign, 1)
        dispatch_campaign_pool(campaign)
        _pool(campaign, 1)
        dispatch_campaign_pool(campaign)

        assert _assignments(campaign) == [a.pk, b.pk]

    def test_three_profiles_rotate_in_pk_order(self, fake_session, second_session):
        campaign = fake_session.campaign
        third = make_session(campaign=campaign)
        ids = [
            fake_session.linkedin_profile.pk,
            second_session.linkedin_profile.pk,
            third.linkedin_profile.pk,
        ]
        _pool(campaign, 6)

        dispatch_campaign_pool(campaign)

        assert _assignments(campaign) == ids * 2

    def test_only_dispatches_ready_to_connect(self, fake_session):
        campaign = fake_session.campaign
        _pool(campaign, 2, state=ProfileState.QUALIFIED)

        assert dispatch_campaign_pool(campaign) == {}
        assert _assignments(campaign) == [None, None]

    def test_leaves_already_assigned_deals_alone(self, fake_session, second_session):
        campaign = fake_session.campaign
        a = fake_session.linkedin_profile
        deal = _pool(campaign, 1)[0]
        deal.assigned_profile = a
        deal.save()

        dispatch_campaign_pool(campaign)

        deal.refresh_from_db()
        assert deal.assigned_profile_id == a.pk


@pytest.mark.django_db
class TestCapacitySkip:
    def test_skips_inactive_profile(self, fake_session, second_session):
        campaign = fake_session.campaign
        a = fake_session.linkedin_profile
        second_session.linkedin_profile.active = False
        second_session.linkedin_profile.save(update_fields=["active"])
        _pool(campaign, 3)

        dispatch_campaign_pool(campaign)

        assert _assignments(campaign) == [a.pk] * 3

    def test_skips_profile_whose_client_is_paused(self, fake_session, second_session):
        campaign = fake_session.campaign
        a = fake_session.linkedin_profile
        client = campaign.client
        client.active = False
        client.save(update_fields=["active"])
        _pool(campaign, 2)

        # Both profiles belong to the paused client — nobody is eligible.
        assert dispatch_campaign_pool(campaign) == {}
        assert _assignments(campaign) == [None, None]

    def test_never_exceeds_a_profiles_daily_budget(self, fake_session):
        """A profile is only handed what it could actually act on today."""
        campaign = fake_session.campaign
        profile = fake_session.linkedin_profile
        profile.connect_daily_limit = 2
        profile.save(update_fields=["connect_daily_limit"])
        _pool(campaign, 5)

        dispatch_campaign_pool(campaign)

        assert _assignments(campaign).count(profile.pk) == 2

    def test_exhausted_profile_passes_its_turn(self, fake_session, second_session):
        """B has no budget left, so A takes the whole pool — nothing idles."""
        campaign = fake_session.campaign
        a, b = fake_session.linkedin_profile, second_session.linkedin_profile
        b.connect_daily_limit = 1
        b.save(update_fields=["connect_daily_limit"])
        ActionLog.objects.create(
            linkedin_profile=b, campaign=campaign,
            action_type=ActionLog.ActionType.CONNECT,
        )
        _pool(campaign, 3)

        dispatch_campaign_pool(campaign)

        assert _assignments(campaign) == [a.pk] * 3

    def test_skips_profile_outside_its_working_hours(self, fake_session, second_session):
        campaign = fake_session.campaign
        a, b = fake_session.linkedin_profile, second_session.linkedin_profile
        # A window that has already closed for every hour of the day.
        b.active_hours_enabled = True
        b.active_start_hour = 0
        b.active_end_hour = 0
        b.save(update_fields=["active_hours_enabled", "active_start_hour", "active_end_hour"])
        _pool(campaign, 2)

        dispatch_campaign_pool(campaign)

        assert _assignments(campaign) == [a.pk, a.pk]


@pytest.mark.django_db
class TestReleaseStaleAssignments:
    def test_releases_deals_held_by_a_deactivated_profile(self, fake_session):
        campaign = fake_session.campaign
        profile = fake_session.linkedin_profile
        deal = _pool(campaign, 1)[0]
        deal.assigned_profile = profile
        deal.save()

        profile.active = False
        profile.save(update_fields=["active"])

        assert release_stale_assignments(campaign) == 1
        deal.refresh_from_db()
        assert deal.assigned_profile_id is None

    def test_keeps_assignments_past_the_pool(self, fake_session):
        """Once contacted, the deal stays with whoever sent the invite."""
        campaign = fake_session.campaign
        profile = fake_session.linkedin_profile
        deal = _pool(campaign, 1, state=ProfileState.PENDING)[0]
        deal.assigned_profile = profile
        deal.save()

        profile.active = False
        profile.save(update_fields=["active"])

        assert release_stale_assignments(campaign) == 0
        deal.refresh_from_db()
        assert deal.assigned_profile_id == profile.pk

    def test_releases_when_profile_leaves_the_campaign(self, fake_session):
        campaign = fake_session.campaign
        profile = fake_session.linkedin_profile
        deal = _pool(campaign, 1)[0]
        deal.assigned_profile = profile
        deal.save()

        campaign.profiles.remove(profile)

        assert release_stale_assignments(campaign) == 1
        deal.refresh_from_db()
        assert deal.assigned_profile_id is None

    def test_released_deal_is_redispatched_to_a_survivor(self, fake_session, second_session):
        campaign = fake_session.campaign
        gone, survivor = fake_session.linkedin_profile, second_session.linkedin_profile
        deal = _pool(campaign, 1)[0]
        deal.assigned_profile = gone
        deal.save()

        gone.active = False
        gone.save(update_fields=["active"])

        release_stale_assignments(campaign)
        dispatch_campaign_pool(campaign)

        deal.refresh_from_db()
        assert deal.assigned_profile_id == survivor.pk


@pytest.mark.django_db
class TestPoolIsolation:
    def test_another_clients_pool_is_untouched(self, fake_session, other_client_session):
        """Dispatch never reaches across tenants."""
        mine, theirs = fake_session.campaign, other_client_session.campaign
        _pool(mine, 2)
        _pool(theirs, 2)

        dispatch_campaign_pool(mine)

        assert all(pid is not None for pid in _assignments(mine))
        assert _assignments(theirs) == [None, None]

    def test_profile_of_another_client_never_gets_work(self, fake_session):
        """The tenant boundary holds even if membership says otherwise.

        Nothing should put another client's profile on this campaign, but
        if something does, it must not start messaging this client's
        prospects.
        """
        campaign = fake_session.campaign
        outsider = LinkedInProfileFactory()  # belongs to a different client
        campaign.profiles.add(outsider)
        _pool(campaign, 4)

        dispatch_campaign_pool(campaign)

        assert outsider.pk not in _assignments(campaign)
        assert set(_assignments(campaign)) == {fake_session.linkedin_profile.pk}
