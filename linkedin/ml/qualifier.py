# linkedin/ml/qualifier.py
"""Lead qualification: a cheap disqualify-only prefilter followed by a full
LLM qualify/reject + fit-score call. See pipeline/qualify.py for orchestration.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import jinja2
import numpy as np
from pydantic import BaseModel, Field
from scipy.stats import norm

from linkedin.conf import PROMPTS_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Qualifier protocol — implemented by KitQualifier (freemium campaigns)
# ---------------------------------------------------------------------------

@runtime_checkable
class Qualifier(Protocol):
    """Common interface for pre-trained-model qualifiers.

    ``rank_profiles`` returns profiles sorted by score (descending).
    Returns ``[]`` on cold start or when ranking is impossible.

    ``explain`` returns a human-readable scoring summary for a single profile.
    """

    def rank_profiles(self, profiles: list, session) -> list: ...
    def explain(self, profile: dict, session) -> str: ...


# ---------------------------------------------------------------------------
# Stage 1 — cheap disqualify-only prefilter
# ---------------------------------------------------------------------------

class CheapDisqualifyDecision(BaseModel):
    """Structured output for the cheap qualification prefilter."""
    disqualify: bool = Field(description="True only if this is a clear, obvious mismatch")
    reason: str = Field(description="Brief explanation for the decision")


def qualify_cheap(
        lead, product_docs: str, campaign_objective: str,
) -> tuple[CheapDisqualifyDecision, str]:
    """Cheap/fast LLM call that only ever disqualifies obvious mismatches.

    Sees exactly two fields — the lead's headline and About section, both
    cached on the Lead row at discovery time (see
    db/leads.py:create_enriched_lead). No scrape, no network.

    Returns (decision, model_id). ``decision.disqualify`` False means "let
    the expensive stage decide", not "qualified". ``model_id`` is the
    ``provider:model`` that actually made the call — read off the built
    model rather than re-read from SiteConfig, so the decision log names
    the model without costing a query.
    """
    from pydantic_ai import Agent

    from linkedin.llm import get_llm_model, run_agent_sync

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("qualify_lead_cheap.j2")

    prompt = template.render(
        product_docs=product_docs,
        campaign_objective=campaign_objective,
        headline=lead.headline,
        about=lead.about,
    )

    model = get_llm_model("cheap")
    agent = Agent(
        model,
        output_type=CheapDisqualifyDecision,
        model_settings={"temperature": 0.0, "timeout": 30},
    )
    decision = run_agent_sync(agent.run(prompt)).output
    return decision, model.model_id


# ---------------------------------------------------------------------------
# Stage 2 — full-dossier qualify/reject + fit score
# ---------------------------------------------------------------------------

class QualificationDecision(BaseModel):
    """Structured LLM output for lead qualification."""
    qualified: bool = Field(description="True if the profile is a good prospect, False otherwise")
    fit_score: int = Field(
        ge=1, le=5,
        description="1-5 rating of how strong a prospect this is; 1 if not qualified",
    )
    reason: str = Field(description="Brief explanation for the decision")


def qualify_with_llm(
        profile_text: str, product_docs: str, campaign_objective: str,
) -> tuple[QualificationDecision, str]:
    """Call the expensive LLM to qualify a lead on its full dossier.

    ``profile_text`` is the rendered dossier from
    ``ml/dossier.py:render_dossier`` — labelled profile, posts, full
    experience and current-employer company pages.

    Returns (decision, model_id) — see ``qualify_cheap`` for why the model
    identity comes back alongside the decision.
    """
    from pydantic_ai import Agent

    from linkedin.llm import get_llm_model, run_agent_sync

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("qualify_lead.j2")

    prompt = template.render(
        product_docs=product_docs,
        campaign_objective=campaign_objective,
        profile_text=profile_text,
    )

    model = get_llm_model("expensive")
    agent = Agent(
        model,
        output_type=QualificationDecision,
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    decision = run_agent_sync(agent.run(prompt)).output

    return decision, model.model_id


# ---------------------------------------------------------------------------
# Shared GP numerics — used only by KitQualifier (pre-trained freemium kits)
# ---------------------------------------------------------------------------

def _prob_above_half(mean, std):
    """P(f > 0.5) from a GP posterior."""
    return norm.sf(0.5, loc=mean, scale=std)


def _gpr_predict(pipe, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Transform through all steps except GPR, then predict with return_std."""
    from sklearn.pipeline import Pipeline

    X = np.asarray(X, dtype=np.float64)
    if X.ndim == 1:
        X = X.reshape(1, -1)
    X_transformed = Pipeline(pipe.steps[:-1]).transform(X)
    return pipe.named_steps['gpr'].predict(X_transformed, return_std=True)


def _load_profile_embeddings(profiles: list, session, *, skip_missing: bool = False):
    """Load embeddings for a list of profile dicts.

    Returns list of (profile, embedding) pairs. One bulk read for the whole
    batch; only leads with no stored embedding fall back to the lazy
    per-lead path (which scrapes).
    """
    from crm.models import Lead

    lead_ids = [p.get("lead_id") for p in profiles if p.get("lead_id") is not None]
    stored = dict(
        Lead.objects.filter(pk__in=lead_ids, embedding__isnull=False)
        .values_list("pk", "embedding")
    )

    result = []
    for p in profiles:
        raw = stored.get(p.get("lead_id"))
        if raw is not None:
            result.append((p, np.frombuffer(bytes(raw), dtype=np.float32).copy()))
            continue

        lead = Lead.objects.filter(pk=p.get("lead_id")).first()
        emb = lead.get_embedding(session) if lead else None
        if emb is None:
            if skip_missing:
                continue
            pid = p.get("public_identifier", "?")
            raise RuntimeError(f"No embedding found for profile {pid}")
        result.append((p, emb))
    return result


def _rank_by_score(profiles: list, pipeline, session, *, skip_missing: bool = False) -> list:
    """Rank profiles by raw pipeline.predict() score (descending)."""
    scored = _load_profile_embeddings(profiles, session, skip_missing=skip_missing)
    if not scored:
        return []

    X = np.array([emb for _, emb in scored], dtype=np.float64)
    scores = pipeline.predict(X)

    ranked = sorted(zip(scores, [p for p, _ in scored]), key=lambda t: t[0], reverse=True)
    return [p for _, p in ranked]


# ---------------------------------------------------------------------------
# KitQualifier  (pre-trained kit model for freemium campaigns)
# ---------------------------------------------------------------------------

class KitQualifier:
    """Qualifier for freemium campaigns backed by a pre-trained GPR kit model.

    Wraps a Pipeline(StandardScaler, GPR) loaded from a campaign kit.
    Ranks by raw GP mean and exposes posterior stats for explanation.
    """

    def __init__(self, kit_model):
        self._model = kit_model

    def rank_profiles(self, profiles: list, session) -> list:
        """Rank profiles by raw model score (descending), skipping missing embeddings."""
        if not profiles:
            return []
        return _rank_by_score(profiles, self._model, session, skip_missing=True)

    def explain(self, profile: dict, session) -> str:
        """Human-readable compact scoring explanation."""
        from crm.models import Lead

        lead = Lead.objects.filter(pk=profile.get("lead_id")).first()
        emb = lead.get_embedding(session) if lead else None
        if emb is None:
            return "No embedding found for profile"
        mean, std = _gpr_predict(self._model, emb)
        gp_mean = float(mean[0])
        p_above = float(_prob_above_half(mean, std)[0])
        return f"mean={gp_mean:.3f}, P(f>0.5)={p_above:.3f}"
