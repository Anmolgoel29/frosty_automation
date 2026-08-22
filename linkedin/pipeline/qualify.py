# linkedin/pipeline/qualify.py
"""Qualify orchestration for the lazy chain."""
from __future__ import annotations

import logging

import numpy as np
from termcolor import colored

from linkedin.ml.qualifier import BayesianQualifier

logger = logging.getLogger(__name__)


# The GP only needs a representative sample of the unlabelled pool to pick
# its next question from; scoring every lead ever discovered is wasted work
# that grows without bound as the campaign runs.
MAX_QUALIFICATION_CANDIDATES = 300


def has_qualification_candidates(session) -> bool:
    """Cheap EXISTS check — "is there anything to qualify at all?".

    Callers that only need a yes/no shouldn't pull the pool's embedding
    blobs across the wire to find out.
    """
    from crm.models import Lead

    return (
        Lead.objects.filter(disqualified=False)
        .exclude(deal__campaign=session.campaign)
        .exists()
    )


def fetch_qualification_candidates(session, *, newest_first: bool = False):
    """Return Lead rows (with embeddings) for leads awaiting qualification.

    One query, capped: the previous version read every unlabelled lead twice
    (once as dicts, once as full rows including the embedding blob) on every
    call, and ``ready_source`` calls this several times per iteration.

    ``newest_first`` exists for exactly one caller: ``qualify_source``'s
    exploit-mode retry loop, which re-fetches after every search to check
    whether the profiles it just found are any good. With the default
    oldest-first order, once a campaign's unlabelled backlog exceeds
    ``MAX_QUALIFICATION_CANDIDATES`` a freshly-discovered profile — always
    the newest row in the table — can never appear in the window at all, so
    that check can never see what search just turned up. It then keeps
    failing for reasons unrelated to the search results, driving the retry
    loop through the entire keyword supply (LLM-regenerated once exhausted)
    on every occurrence. The default stays oldest-first because ordering
    doesn't matter for ``run_qualification``'s actual selection (BALD
    acquisition picks the most informative lead regardless of list order) —
    only which 300 are visible does, and oldest-first there preserves the
    existing FIFO drain of the backlog.
    """
    from crm.models import Lead

    order = "-creation_date" if newest_first else "creation_date"
    candidates = list(
        Lead.objects.filter(disqualified=False, embedding__isnull=False)
        .exclude(deal__campaign=session.campaign)
        .order_by(order)[:MAX_QUALIFICATION_CANDIDATES]
    )
    if candidates:
        return candidates

    # Nothing embedded is waiting — fall back to embedding one lead that was
    # missed at discovery time, so a pool of un-embedded seeds still moves.
    stale = (
        Lead.objects.filter(disqualified=False, embedding__isnull=True)
        .exclude(deal__campaign=session.campaign)
        .order_by("creation_date")
        .first()
    )
    if stale and stale.get_embedding(session) is not None:
        return [stale]

    return []


def run_qualification(session, qualifier: BayesianQualifier) -> str | None:
    """Qualify one unlabelled profile via BALD/auto-decision/LLM. Returns public_id or None.

    Serialised per campaign: the accounts share one unlabelled pool and one GP
    model, so two of them qualifying at once would pick the same lead twice
    (duplicate LLM call, then a ``unique_deal_per_campaign`` violation) and
    refit the same sklearn pipeline concurrently. Candidates are re-read
    *inside* the lock so the second account through sees the first one's label.
    """
    from linkedin.pipeline.locks import campaign_lock

    with campaign_lock(session.campaign):
        return _run_qualification_locked(session, qualifier)


def _run_qualification_locked(session, qualifier: BayesianQualifier) -> str | None:
    from linkedin.ml.qualifier import qualify_with_llm, format_prediction

    candidates = fetch_qualification_candidates(session)
    if not candidates:
        return None

    logger.info(colored("\u25b6 qualify", "blue", attrs=["bold"]))

    # Balance-driven candidate selection
    selection_score = None
    if len(candidates) == 1:
        candidate = candidates[0]
    else:
        embeddings = np.array([c.embedding_array for c in candidates], dtype=np.float32)
        result = qualifier.acquisition_scores(embeddings)

        if result is None:
            candidate = candidates[0]
        else:
            strategy, scores = result
            best_idx = int(np.argmax(scores))
            candidate = candidates[best_idx]
            selection_score = (strategy, float(scores[best_idx]))
            n_neg, n_pos = qualifier.class_counts
            logger.info("Strategy: %s (neg=%d, pos=%d)",
                        colored(strategy, "cyan", attrs=["bold"]), n_neg, n_pos)

    lead_id = candidate.pk
    public_id = candidate.public_identifier
    embedding = candidate.embedding_array

    result = qualifier.predict(embedding)

    if result is not None:
        pred_prob, entropy, std = result
        stats = format_prediction(pred_prob, entropy, std, qualifier.n_obs)
        sel = f", {selection_score[0]}={selection_score[1]:.4f}" if selection_score else ""
        logger.debug("%s (%s%s) — querying LLM", public_id, stats, sel)
    else:
        logger.debug("%s GP not fitted (%d obs) — querying LLM", public_id, qualifier.n_obs)

    profile_text = _fetch_profile_text(session, lead_id, public_id)
    if not profile_text:
        logger.warning("No profile text for lead %d \u2014 disqualifying", lead_id)
        _save_qualification_result(session, qualifier, lead_id, public_id, embedding, 0, "no profile text available")
        return public_id

    campaign = session.campaign
    label, reason = qualify_with_llm(
        profile_text,
        product_docs=campaign.product_docs,
        campaign_objective=campaign.campaign_objective,
    )
    _save_qualification_result(session, qualifier, lead_id, public_id, embedding, label, reason)
    return public_id


def _save_qualification_result(session, qualifier: BayesianQualifier, lead_id: int, public_id: str, embedding: np.ndarray, label: int, reason: str):
    # LLM rejections are tracked as FAILED Deals with "Disqualified" closing reason
    # (campaign-scoped), not as Lead.disqualified (permanent account-level exclusion).
    from linkedin.db.deals import create_disqualified_deal
    from linkedin.db.leads import promote_lead_to_deal

    qualifier.update(embedding, label)

    if label == 1:
        try:
            promote_lead_to_deal(session, public_id, reason=reason)
        except ValueError as e:
            logger.warning("Cannot promote %s: %s \u2014 disqualifying", public_id, e)
            create_disqualified_deal(session, public_id, reason=str(e))
            return
        logger.info("%s %s: %s", public_id, colored("QUALIFIED", "green", attrs=["bold"]), reason)
    else:
        create_disqualified_deal(session, public_id, reason=reason)


def _fetch_profile_text(session, lead_id: int, public_id: str) -> str | None:
    from crm.models import Lead
    from linkedin.ml.profile_text import build_profile_text

    lead = Lead.objects.filter(pk=lead_id).first()
    if not lead:
        return None
    profile_data = lead.get_profile(session)
    if not profile_data:
        return None
    return build_profile_text({"profile": profile_data})
