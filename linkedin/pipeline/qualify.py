# linkedin/pipeline/qualify.py
"""Qualify orchestration for the lazy chain.

Two-stage cascade:

* ``qualify_cheap`` — cheap/fast LLM, reads only the headline and About
  section already cached on the Lead row. Disqualifies obvious mismatches.
  No network at all.
* ``qualify_with_llm`` — expensive LLM, reads the full dossier
  (``ml/dossier.py``): profile, follower count, recent posts, the whole
  experience section, and every current employer's company page and posts.
  Makes the real qualify/reject call plus a 1-5 fit score.

The cheap stage exists to keep the dossier's several LinkedIn reads off
leads that were never going to qualify.
"""
from __future__ import annotations

import logging

from termcolor import colored

logger = logging.getLogger(__name__)


def has_qualification_candidates(session) -> bool:
    """Cheap EXISTS check — "is there anything to qualify at all?".

    Callers that only need a yes/no shouldn't pull a Lead row across the
    wire to find out.
    """
    from crm.models import Lead

    return (
        Lead.objects.filter(disqualified=False)
        .exclude(deal__campaign=session.campaign)
        .exists()
    )


def fetch_next_qualification_candidate(session):
    """Return the oldest Lead awaiting qualification in this campaign, or None.

    Plain FIFO drain of the backlog. There's no acquisition strategy left to
    pick among a batch — the cheap stage is affordable on every candidate —
    so this pulls exactly the one lead ``run_qualification`` is about to
    process instead of a windowed batch of up to 300.
    """
    from crm.models import Lead

    return (
        Lead.objects.filter(disqualified=False)
        .exclude(deal__campaign=session.campaign)
        .order_by("creation_date")
        .first()
    )


def run_qualification(session) -> str | None:
    """Qualify one unlabelled profile via the cheap→expensive LLM cascade.

    Returns public_id or None. Serialised per campaign: the accounts share
    one unlabelled pool, so two of them qualifying at once could both pick
    the same lead (duplicate LLM spend, then a ``unique_deal_per_campaign``
    violation). The candidate is re-read *inside* the lock so the second
    account through sees the first one's Deal.
    """
    from linkedin.pipeline.locks import campaign_lock

    with campaign_lock(session.campaign):
        return _run_qualification_locked(session)


def _run_qualification_locked(session) -> str | None:
    from linkedin import tracing
    from linkedin.db.deals import create_disqualified_deal
    from linkedin.db.leads import ensure_coarse_fields
    from linkedin.ml.dossier import build_dossier_text
    from linkedin.ml.qualifier import qualify_cheap, qualify_with_llm

    candidate = fetch_next_qualification_candidate(session)
    if not candidate:
        return None

    logger.info(colored("▶ qualify", "blue", attrs=["bold"]))

    public_id = candidate.public_identifier
    campaign = session.campaign

    # A connect task's payload only carries campaign_id — this is where its
    # target lead is actually picked, so backfill it onto the task_span the
    # daemon already opened (session_id too: connect tasks start with none,
    # since it's derived from the lead half of the pair).
    tracing.tag_current_span(
        session_id=tracing.session_id_for(campaign_id=campaign.pk, public_id=public_id),
        lead_public_identifier=public_id,
    )

    # Leads that predate the coarse-field cache reach the cheap stage with
    # nothing to read; repair them here rather than let it rubber-stamp
    # every one of them straight through to the expensive stage.
    ensure_coarse_fields(session, candidate)

    disqualify, cheap_reason = qualify_cheap(
        candidate, campaign.product_docs, campaign.campaign_objective,
    )
    if disqualify:
        # Skips not just the expensive LLM call but the whole dossier scrape
        # behind it — several LinkedIn reads per lead (see ml/dossier.py).
        logger.debug("%s cheap-disqualified — skipping dossier + expensive stage", public_id)
        create_disqualified_deal(session, public_id, reason=cheap_reason)
        return public_id

    dossier_text = build_dossier_text(session, candidate)
    if not dossier_text:
        logger.warning("No profile reachable for lead %d — disqualifying", candidate.pk)
        create_disqualified_deal(session, public_id, reason="profile not reachable")
        return public_id

    qualified, fit_score, reason = qualify_with_llm(
        dossier_text,
        product_docs=campaign.product_docs,
        campaign_objective=campaign.campaign_objective,
    )
    _save_qualification_result(session, public_id, qualified, fit_score, reason)
    return public_id


def _save_qualification_result(session, public_id: str, qualified: bool, fit_score: int, reason: str):
    # LLM rejections are tracked as FAILED Deals with "Disqualified" closing reason
    # (campaign-scoped), not as Lead.disqualified (permanent account-level exclusion).
    from linkedin.db.deals import create_disqualified_deal
    from linkedin.db.leads import promote_lead_to_deal

    if qualified:
        try:
            promote_lead_to_deal(session, public_id, reason=reason, fit_score=fit_score)
        except ValueError as e:
            logger.warning("Cannot promote %s: %s — disqualifying", public_id, e)
            create_disqualified_deal(session, public_id, reason=str(e))
            return
        logger.info(
            "%s %s (fit_score=%d): %s",
            public_id, colored("QUALIFIED", "green", attrs=["bold"]), fit_score, reason,
        )
    else:
        create_disqualified_deal(session, public_id, reason=reason)


