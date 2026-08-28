# linkedin/pipeline/search.py
"""Search keyword management and LinkedIn People search."""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from termcolor import colored

logger = logging.getLogger(__name__)


def _claim_keyword(campaign):
    """Atomically take the next unused keyword for this campaign.

    This is how search work is divided between the accounts: the keyword
    queue is campaign-wide and every account pulls from it, so they cover
    different searches instead of duplicating each other. ``SKIP LOCKED``
    keeps that true when several accounts claim at the same moment \u2014 without
    it, both would read the same row as unused and search it twice.
    """
    from linkedin.models import SearchKeyword

    with transaction.atomic():
        kw = (
            SearchKeyword.objects.select_for_update(skip_locked=True)
            .filter(campaign=campaign, used=False)
            .order_by("pk")
            .first()
        )
        if kw is None:
            return None
        kw.used = True
        kw.used_at = timezone.now()
        kw.save(update_fields=["used", "used_at"])
        return kw


def run_search(session) -> str | None:
    """Use the next search keyword to discover new profiles. Returns keyword or None."""
    from linkedin.actions.search import search_people
    from linkedin.pipeline.search_keywords import generate_search_keywords
    from linkedin.models import SearchKeyword

    campaign = session.campaign

    kw = _claim_keyword(campaign)
    if kw is None:
        # Pool exhausted \u2014 top it up. Generation is serialised per campaign so
        # two accounts don't ask the LLM for keywords at the same moment and
        # get overlapping batches.
        from linkedin.pipeline.locks import campaign_lock

        with campaign_lock(campaign):
            kw = _claim_keyword(campaign)
            if kw is None:
                used = list(
                    SearchKeyword.objects.filter(campaign=campaign, used=True)
                    .values_list("keyword", flat=True)
                )
                fresh = generate_search_keywords(
                    product_docs=campaign.product_docs,
                    campaign_objective=campaign.campaign_objective,
                    exclude_keywords=used if used else None,
                )
                if not fresh:
                    return None

                objs = [SearchKeyword(campaign=campaign, keyword=k) for k in fresh]
                SearchKeyword.objects.bulk_create(objs, ignore_conflicts=True)
                kw = _claim_keyword(campaign)

    if kw is None:
        return None

    where = campaign.geo_labels()
    logger.info(
        colored("\u25b6 search", "magenta", attrs=["bold"]) + " keyword=%r location=%s",
        kw.keyword, where or "worldwide",
    )
    search_people(session, kw.keyword)
    return kw.keyword
