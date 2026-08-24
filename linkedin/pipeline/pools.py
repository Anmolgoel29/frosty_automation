# linkedin/pipeline/pools.py
"""Pool management via composable generators.

Three generators chain via next(upstream, None):

    find_candidate() = next(ready_source, None)
                            |
                  ready_source  <- this account's allocated leads only;
                            |      deals out the un-owned ready pool round-robin
                            |      (pipeline/allocation.py) when it runs dry
                            |
                 qualify_source  <- pulls from search_source when the backlog is empty
                            |
                  search_source  <- yields keywords (never truly exhausts)

Only ready_source is per-account: search, enrichment and qualification all
feed one campaign-wide pool, whichever account happens to drive them.
"""
from __future__ import annotations

import logging
from typing import Generator

from linkedin.conf import CAMPAIGN_CONFIG
from linkedin.pipeline.allocation import allocate_ready_deals
from linkedin.pipeline.qualify import has_qualification_candidates, run_qualification
from linkedin.pipeline.ready_pool import find_ready_candidate, promote_to_ready
from linkedin.pipeline.search import run_search

logger = logging.getLogger(__name__)


def search_source(session) -> Generator[str, None, None]:
    """Yield keywords from run_search(). Stops when run_search returns None."""
    while True:
        keyword = run_search(session)
        if keyword is None:
            return
        yield keyword


def qualify_source(session) -> Generator[str, None, None]:
    """Yield public_ids from run_qualification(), searching when the backlog is empty.

    The cheap prefilter is affordable on every candidate, so there's no
    acquisition strategy left to ration — this just drains the backlog FIFO
    and tops it up via search whenever it runs dry.
    """
    search = search_source(session)

    while True:
        if not has_qualification_candidates(session):
            if next(search, None) is None:
                return
            if not has_qualification_candidates(session):
                return

        result = run_qualification(session)
        if result is None:
            return
        yield result


def ready_source(session, threshold: float | None = None) -> Generator[dict, None, None]:
    """Yield ready-to-connect candidates *owned by this account*.

    Everything upstream of this generator is shared across the campaign's
    accounts; ``find_ready_candidate`` is the first step that only sees this
    account's own leads. When it comes up empty we deal out whatever is
    sitting un-owned in the ready pool — the rotation may well hand those
    leads to the other accounts, in which case we keep pulling upstream
    until our turn comes round.
    """
    if threshold is None:
        threshold = CAMPAIGN_CONFIG["min_fit_score"]
    qualify = qualify_source(session)

    while True:
        candidate = find_ready_candidate(session)
        if candidate is not None:
            yield candidate
            continue

        if allocate_ready_deals(session.campaign) > 0:
            continue

        promoted = promote_to_ready(session, threshold)
        if promoted > 0:
            continue

        # Pull one qualification from upstream — may produce a fresh QUALIFIED deal
        if next(qualify, None) is not None:
            # Re-check promote after the new label
            promote_to_ready(session, threshold)
            continue

        # Upstream exhausted
        return


def find_candidate(session) -> dict | None:
    """Top profile ready for connection, backfilling if needed."""
    return next(ready_source(session), None)
