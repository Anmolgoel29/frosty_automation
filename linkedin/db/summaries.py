"""mem0-style fact-list summary for the Deal profile.

Single LLM boundary for the lazy summary pipeline. The summary is stored as a
JSON fact list on `Deal.profile_summary`. It is a campaign-scoped derived
cache: deleting it and re-running the lazy path rebuilds it from source (a
Voyager re-scrape).

There is deliberately no chat summariser. Conversation history is never
compressed — `linkedin/agents/follow_up.py` reads the full `ChatMessage`
thread verbatim on every run. See that module and `follow_up_agent.j2`.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Vendored fact-extraction prompt — modeled on mem0's FACT_RETRIEVAL_PROMPT.
# Kept inline so we don't pull mem0ai's transitive deps (qdrant, grpcio,
# sqlalchemy, posthog, ~12 MB) just for one constant string.
_FACT_EXTRACTION_PROMPT = """\
You are an information-extraction assistant. Your job is to read the input
text and produce a flat list of atomic, self-contained factual statements
about the lead (the person we are talking to).

Rules:
- Each fact must be a complete sentence that stands on its own.
- Prefer concrete, durable facts (identity, role, employer, location, career
  arc, stated goals, expressed concerns) over fleeting commentary.
- Do not invent facts. If the text does not assert it, do not include it.
- Do not duplicate facts. Merge near-duplicates.
- Keep each fact short (under ~25 words).
- Return between 0 and 30 facts. Empty list is acceptable when there is
  nothing useful to extract.

Output a JSON object matching the schema you have been given.
"""


class FactList(BaseModel):
    """Structured LLM output for fact extraction."""

    facts: list[str] = Field(
        default_factory=list,
        description="Atomic, self-contained factual statements extracted from the input text.",
    )


# ── LLM boundary ──

def extract_facts(text: str, *, context: str = "") -> list[str]:
    """Extract a flat list of atomic facts from `text`.

    `context` is an optional preamble (campaign objective, product docs) that
    biases what counts as a relevant fact. Returns `[]` for empty inputs.
    """
    if not text or not text.strip():
        return []

    from pydantic_ai import Agent

    from linkedin.llm import get_llm_model, run_agent_sync

    system = _FACT_EXTRACTION_PROMPT
    if context:
        system = f"{system}\n\nContext for relevance:\n{context}"

    agent = Agent(
        get_llm_model("expensive"),
        system_prompt=system,
        output_type=FactList,
        model_settings={"temperature": 0.0, "timeout": 60},
    )
    result: FactList = run_agent_sync(agent.run(text)).output
    return list(result.facts)


# ── Profile summary ──

def materialize_profile_summary_if_missing(deal, session) -> None:
    """Build `deal.profile_summary` lazily on first follow-up touch.

    Re-scrapes the lead via Voyager once per `(lead, campaign)` lifetime,
    extracts facts conditioned on the campaign objective + product docs,
    persists them on the Deal. No-op if already built.
    """
    if deal.profile_summary:
        return

    lead = deal.lead
    profile = lead.get_profile(session)
    if not profile:
        logger.warning(
            "materialize_profile_summary: empty profile for deal=%s lead=%s",
            deal.pk, lead.public_identifier,
        )
        return

    from linkedin.ml.profile_text import build_profile_text

    profile_text = build_profile_text({"profile": profile})
    context_parts = []
    campaign = deal.campaign
    if getattr(campaign, "campaign_objective", None):
        context_parts.append(f"Campaign objective: {campaign.campaign_objective}")
    if getattr(campaign, "product_docs", None):
        context_parts.append(f"Product context: {campaign.product_docs}")
    context = "\n\n".join(context_parts)

    facts = extract_facts(profile_text, context=context)
    deal.profile_summary = {"facts": facts}
    deal.save(update_fields=["profile_summary"])
    logger.info(
        "profile_summary built for deal=%s lead=%s (%d facts)",
        deal.pk, lead.public_identifier, len(facts),
    )
