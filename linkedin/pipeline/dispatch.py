# linkedin/pipeline/dispatch.py
"""Hand prospects from a campaign's shared pool to its profiles, round-robin.

Every profile on a campaign qualifies into one pool. Nobody reaches out to
anyone until this module says so, which is what stops two of a client's
profiles from turning up at the same person's door.

The rotation is a cursor on the Campaign, advanced one step per assignment
and persisted, so it survives restarts and picks up where it left off. A
profile that can't act right now — deactivated, out of quota, or outside
its working window — is skipped and its turn passes to the next one, so a
throttled profile parks its share of the pool instead of leaving prospects
to go stale.

Deals leave the pool for good once they enter PENDING: the profile that
sent the invite is the only one who can see the reply. Deals still in the
pool whose owner has gone away are released back (see
``release_stale_assignments``).
"""
from __future__ import annotations

import logging
from collections import Counter

from django.db import transaction
from django.utils import timezone
from termcolor import colored

from linkedin.db.deals import POOLED_STATES
from linkedin.enums import ProfileState
from linkedin.models import ActionLog
from linkedin.schedule import is_active_now

logger = logging.getLogger(__name__)


def _dispatchable_profiles(campaign) -> tuple[list, list[int]]:
    """Eligible profiles for this campaign paired with today's spare capacity.

    Capacity is the profile's remaining connect budget, so one dispatch
    never hands a profile more prospects than it could act on today. A
    profile outside its working window reports zero: it would just sit on
    them.
    """
    profiles = campaign.eligible_profiles()
    capacity = []
    for profile in profiles:
        if not is_active_now(profile):
            capacity.append(0)
            continue
        capacity.append(profile.remaining_today(ActionLog.ActionType.CONNECT))
    return profiles, capacity


def dispatch_campaign_pool(campaign) -> dict[str, int]:
    """Assign the campaign's unassigned READY_TO_CONNECT deals to its profiles.

    Returns ``{profile_label: n_assigned}`` for logging. Safe to call from
    several workers at once: the pool rows are locked with
    ``FOR UPDATE SKIP LOCKED``, so a concurrent caller sees only what this
    one didn't take.
    """
    from crm.models import Deal

    profiles, capacity = _dispatchable_profiles(campaign)
    if not profiles or not any(capacity):
        return {}

    n = len(profiles)
    assigned: Counter[str] = Counter()

    with transaction.atomic():
        # Re-read the cursor inside the lock so two dispatchers racing on the
        # same campaign don't both start from the same rotation position.
        locked_campaign = (
            type(campaign).objects.select_for_update().get(pk=campaign.pk)
        )
        cursor = locked_campaign.dispatch_cursor

        # No select_related here: FOR UPDATE would lock the joined crm_lead
        # rows too, and the loop below only touches Deal columns.
        pool = list(
            Deal.objects.select_for_update(skip_locked=True)
            .filter(
                campaign=campaign,
                state=ProfileState.READY_TO_CONNECT,
                assigned_profile__isnull=True,
            )
            .order_by("creation_date", "pk")[: sum(capacity)],
        )

        now = timezone.now()
        for deal in pool:
            chosen = _next_with_capacity(cursor, capacity, n)
            if chosen is None:
                break  # everyone is full — the rest stays pooled for later
            profile = profiles[chosen]
            deal.assigned_profile = profile
            deal.assigned_at = now
            deal.save(update_fields=["assigned_profile", "assigned_at"])
            capacity[chosen] -= 1
            cursor = chosen + 1
            assigned[str(profile)] += 1

        locked_campaign.dispatch_cursor = cursor % n
        locked_campaign.save(update_fields=["dispatch_cursor"])

    if assigned:
        logger.info(
            "[%s] %s %s",
            campaign,
            colored("⇄ dispatch", "yellow", attrs=["bold"]),
            ", ".join(f"{label}×{count}" for label, count in assigned.items()),
        )
    return dict(assigned)


def _next_with_capacity(cursor: int, capacity: list[int], n: int) -> int | None:
    """Index of the next profile at or after *cursor* that still has room."""
    for step in range(n):
        idx = (cursor + step) % n
        if capacity[idx] > 0:
            return idx
    return None


def release_stale_assignments(campaign) -> int:
    """Return pooled deals to the pool when their assignee can't work them.

    Covers a profile that was deactivated, had its client paused, or was
    taken off the campaign while holding assignments. Only deals still in
    ``POOLED_STATES`` move — past that the prospect has already been
    contacted by that profile and the assignment is theirs to keep.
    """
    from crm.models import Deal

    eligible_ids = [p.pk for p in campaign.eligible_profiles()]

    released = (
        Deal.objects.filter(
            campaign=campaign,
            state__in=POOLED_STATES,
            assigned_profile__isnull=False,
        )
        .exclude(assigned_profile_id__in=eligible_ids)
        .update(assigned_profile=None, assigned_at=None)
    )
    if released:
        logger.info("[%s] released %d assignment(s) back to the pool", campaign, released)
    return released
