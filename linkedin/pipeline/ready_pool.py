# linkedin/pipeline/ready_pool.py
"""Ready-to-connect pool: fit-score gate between QUALIFIED and READY_TO_CONNECT."""
from __future__ import annotations

import logging

from termcolor import colored

from linkedin.db.deals import get_ready_to_connect_profiles

logger = logging.getLogger(__name__)


def promote_to_ready(session, threshold: int) -> int:
    """Promote QUALIFIED profiles with fit_score >= threshold to READY_TO_CONNECT.

    Returns the number of profiles promoted. A single bulk UPDATE — flat
    cost regardless of pool size, and re-evaluates every time it's called,
    so raising or lowering ``min_fit_score`` mid-campaign takes effect on
    the existing QUALIFIED backlog immediately, not just on new labels.

    Shares the campaign lock with ``run_qualification``: two accounts
    promoting the same QUALIFIED pool concurrently would both count the
    same promotions.
    """
    from linkedin.pipeline.locks import campaign_lock

    with campaign_lock(session.campaign):
        return _promote_to_ready_locked(session, threshold)


def _promote_to_ready_locked(session, threshold: int) -> int:
    from crm.models import Deal
    from linkedin.enums import ProfileState

    promoted = Deal.objects.filter(
        campaign=session.campaign,
        state=ProfileState.QUALIFIED,
        fit_score__gte=threshold,
    ).update(state=ProfileState.READY_TO_CONNECT)

    if promoted:
        logger.info(
            "%d %s (fit_score >= %d)",
            promoted, colored("READY_TO_CONNECT", "yellow", attrs=["bold"]), threshold,
        )
    return promoted


def find_ready_candidate(session) -> dict | None:
    """Return the top-ranked READY_TO_CONNECT profile, or None.

    ``get_ready_to_connect_profiles`` orders by fit_score descending, so
    this is just "whichever this account's allocated pool ranks first" —
    no per-call ranking pass needed.
    """
    profiles = get_ready_to_connect_profiles(session)
    return profiles[0] if profiles else None
