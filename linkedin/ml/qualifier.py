# linkedin/ml/qualifier.py
"""Lead qualification: a cheap disqualify-only prefilter followed by a full
LLM qualify/reject + fit-score call. See pipeline/qualify.py for orchestration.
"""
from __future__ import annotations

import logging

import jinja2
from pydantic import BaseModel, Field

from linkedin.conf import PROMPTS_DIR

logger = logging.getLogger(__name__)


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
