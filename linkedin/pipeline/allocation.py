# linkedin/pipeline/allocation.py
"""Round-robin division of the shared lead pool across a campaign's accounts.

Everything upstream of the connect step is common work: search, enrichment
and qualification all write into one campaign-wide pool of Leads and Deals,
whichever account happens to be driving the pipeline at the time. This
module is the single point where that pool is divided.

A Deal that reaches READY_TO_CONNECT with no owner is handed to exactly one
``LinkedInProfile``, taking each account in turn (``Campaign.assignment_cursor``
is the rotation pointer). From then on ownership is sticky: only the owning
account connects, polls the invite, and runs the conversation. Crossover is
not a fairness question but a correctness one — the pending invite and the
resulting message thread only exist on the account that sent them.
"""
from __future__ import annotations

import logging

from django.db import transaction

from linkedin.enums import ProfileState

logger = logging.getLogger(__name__)

# States where a lead is still un-contacted and may therefore change hands.
# Anything past this point is locked to the account that reached out.
_REASSIGNABLE_STATES = (ProfileState.QUALIFIED, ProfileState.READY_TO_CONNECT)


@transaction.atomic
def allocate_ready_deals(campaign) -> int:
    """Deal out un-owned READY_TO_CONNECT leads, one per account in rotation.

    Returns the number of leads assigned. Idempotent — leads that already
    have an owner are never touched, so calling this on every connect task
    and every reconcile costs one query when there is nothing to do.
    """
    from crm.models import Deal
    from linkedin.models import Campaign

    profiles = campaign.active_profiles()
    if not profiles:
        logger.warning(
            "Campaign %s has no active LinkedIn accounts — leads cannot be allocated",
            campaign,
        )
        return 0

    unassigned = list(
        Deal.objects.select_for_update(of=("self",))
        .filter(
            campaign=campaign,
            state=ProfileState.READY_TO_CONNECT,
            assigned_profile__isnull=True,
        )
        .select_related("lead")
        .order_by("pk")
    )
    if not unassigned:
        return 0

    cursor = (
        Campaign.objects.select_for_update()
        .values_list("assignment_cursor", flat=True)
        .get(pk=campaign.pk)
    )

    for deal in unassigned:
        profile = profiles[cursor % len(profiles)]
        deal.assigned_profile = profile
        deal.save(update_fields=["assigned_profile"])
        cursor += 1
        logger.info(
            "[%s] %s allocated to %s",
            campaign, deal.lead.public_identifier, profile.linkedin_username,
        )

    cursor %= len(profiles)
    Campaign.objects.filter(pk=campaign.pk).update(assignment_cursor=cursor)
    campaign.assignment_cursor = cursor
    return len(unassigned)


def reclaim_unreachable_deals(campaign) -> int:
    """Return un-contacted leads owned by a retired account to the pool.

    An account that was deactivated or detached from the campaign will never
    run another task, so leads it was holding but never reached out to would
    stall forever. They go back to ``assigned_profile=None`` and are dealt
    out again on the next allocation. Contacted leads (PENDING and beyond)
    are deliberately left stranded — see ``_seed_deal_tasks``.
    """
    from crm.models import Deal

    active_ids = [p.pk for p in campaign.active_profiles()]
    reclaimed = (
        Deal.objects.filter(
            campaign=campaign,
            state__in=_REASSIGNABLE_STATES,
            assigned_profile__isnull=False,
        )
        .exclude(assigned_profile_id__in=active_ids)
        .update(assigned_profile=None)
    )
    if reclaimed:
        logger.info(
            "[%s] reclaimed %d un-contacted lead(s) from retired accounts",
            campaign, reclaimed,
        )
    return reclaimed
