# linkedin/agents/follow_up.py
"""Follow-up agent: reads conversation, returns a structured decision.

Single LLM call with structured output — no tool-calling loop.
The handler in tasks/follow_up.py executes the decision.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

import jinja2
from pydantic import BaseModel, Field, model_validator

from linkedin.conf import PROMPTS_DIR
from linkedin.llm import get_llm_model, run_agent_sync

logger = logging.getLogger(__name__)


class FollowUpDecision(BaseModel):
    """Structured output from the follow-up agent."""

    action: Literal["send_message", "mark_completed", "wait"] = Field(
        description="What to do next for this lead.",
    )
    message: str | None = Field(
        default=None,
        description="The message to send. Required when action='send_message'.",
    )
    outcome: Literal[
        "converted", "not_interested", "wrong_fit", "no_budget",
        "has_solution", "bad_timing", "unresponsive",
    ] | None = Field(
        default=None,
        description="Why the conversation ended. Required when action='mark_completed'.",
    )
    follow_up_hours: float = Field(
        description="Hours until next follow-up. Always required — you decide the pace.",
    )

    @model_validator(mode="after")
    def _check_required_fields(self):
        if self.action == "send_message" and not self.message:
            raise ValueError("message is required when action='send_message'")
        if self.action == "mark_completed" and not self.outcome:
            raise ValueError("outcome is required when action='mark_completed'")
        return self


# The agent reads the *whole* thread verbatim — no summarisation layer. These
# two caps exist only so a pathological thread can't blow the context window;
# a normal LinkedIn DM conversation is well under both. When either bites we
# keep the newest messages and say so in the transcript header, rather than
# quietly handing the agent a truncated history it thinks is complete.
MAX_TRANSCRIPT_MESSAGES = 200
MAX_TRANSCRIPT_CHARS = 60_000


def _humanize_age(when: datetime, now: datetime) -> str:
    """Render `when` as a coarse age relative to `now` (e.g. ``3d ago``)."""
    delta = now - when
    if delta < timedelta(hours=1):
        return f"{max(int(delta.total_seconds() // 60), 1)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _format_transcript(messages: list, now: datetime, *, self_name: str, omitted: int = 0) -> str:
    """Render the conversation as a numbered, speaker-tagged, dated transcript.

    One block per message: an index, who sent it, an absolute timestamp and a
    relative age, then the body indented underneath. Multi-line message bodies
    keep their line breaks — the indent is what keeps them from reading as new
    turns.
    """
    turns = [(m, (m.content or "").strip()) for m in messages]
    turns = [(m, c) for m, c in turns if c]
    if not turns:
        return "(no messages have been exchanged yet — this thread is empty)"

    lines: list[str] = []
    if omitted:
        lines.append(
            f"[{omitted} older message(s) omitted — thread too long to include in full. "
            f"The messages below are the most recent ones, in order.]"
        )
    for idx, (m, content) in enumerate(turns, start=1):
        speaker = f"YOU ({self_name})" if m.is_outgoing else "LEAD"
        when = (
            f"{m.creation_date:%Y-%m-%d %H:%M} ({_humanize_age(m.creation_date, now)})"
            if m.creation_date else "time unknown"
        )
        body = "\n".join(f"    {line}" for line in content.splitlines())
        lines.append(f"[{idx}] {speaker} — {when}\n{body}")
    return "\n\n".join(lines)


def _days_since_last_outgoing(messages: list, now: datetime) -> int | None:
    """Whole days since the most recent outgoing message, or None if there are none."""
    timestamps = [m.creation_date for m in messages if m.is_outgoing and m.creation_date]
    if not timestamps:
        return None
    return max((now - max(timestamps)).days, 0)


def _count_unanswered_outgoing(messages: list) -> int:
    """Trailing run of outgoing messages with no lead reply after them."""
    count = 0
    for m in reversed(messages):
        if m.is_outgoing:
            count += 1
        else:
            break
    return count


def _format_facts(summary: dict | None) -> str:
    """Render a `{facts: [...]}` summary blob as a bullet list."""
    facts = (summary or {}).get("facts") or []
    if not facts:
        return "(none yet)"
    return "\n".join(f"- {f}" for f in facts)


def _replace_em_dashes(text: str) -> str:
    """Replace em dashes with plain hyphens — LLMs overuse them; humans don't type them."""
    return text.replace("—", "-")


def _load_conversation(deal) -> tuple[list, int]:
    """Full ChatMessage history for `deal.lead`, chronological, plus omitted count.

    Returns the newest `MAX_TRANSCRIPT_MESSAGES` rows (trimmed further if they
    exceed `MAX_TRANSCRIPT_CHARS`) and how many older rows were dropped to get
    there — the caller surfaces that number in the transcript header so the
    agent is never told a partial history is complete. Both caps are far above
    a real LinkedIn thread, so `omitted` is 0 in practice.
    """
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(deal.lead.__class__)
    base = ChatMessage.objects.filter(content_type=ct, object_id=deal.lead_id)
    # Fetch one row past the cap: if it comes back the thread overflows, and
    # only then do we pay for a COUNT to report how many were left out.
    rows = list(base.order_by("-creation_date", "-pk")[:MAX_TRANSCRIPT_MESSAGES + 1])
    overflowed = len(rows) > MAX_TRANSCRIPT_MESSAGES
    if overflowed:
        rows = rows[:MAX_TRANSCRIPT_MESSAGES]

    # `rows` is newest-first, so walking it forward and stopping on the
    # character budget drops the *oldest* turns — the ones least likely to
    # matter for the next reply. Blank bodies (attachment-only turns) are
    # skipped so the transcript's numbering stays contiguous.
    budget = MAX_TRANSCRIPT_CHARS
    kept: list = []
    for m in rows:
        content = (m.content or "").strip()
        if not content:
            continue
        if kept and budget - len(content) < 0:
            overflowed = True
            break
        budget -= len(content)
        kept.append(m)
    kept.reverse()

    omitted = base.count() - len(kept) if overflowed else 0
    return kept, max(omitted, 0)


def _render_system_prompt(session, deal, messages: list, omitted: int = 0) -> str:
    """Render the agent system prompt from the Jinja2 template."""
    from django.utils import timezone

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("follow_up_agent.j2")

    campaign = deal.campaign
    self_prof = session.self_profile
    self_name = f"{self_prof.get('first_name', '')} {self_prof.get('last_name', '')}".strip() or session.django_user.username

    now = timezone.now()
    return template.render(
        self_name=self_name,
        lead_handle=deal.lead.public_identifier,
        contact_email=session.linkedin_profile.linkedin_username,
        product_docs=campaign.product_docs or "",
        campaign_objective=campaign.campaign_objective or "",
        booking_link=campaign.booking_link or "",
        profile_summary=_format_facts(deal.profile_summary),
        transcript=_format_transcript(messages, now, self_name=self_name, omitted=omitted),
        message_count=len(messages),
        today=now.strftime("%Y-%m-%d"),
        days_since_last_outgoing=_days_since_last_outgoing(messages, now),
        unanswered_outgoing=_count_unanswered_outgoing(messages),
    )


def run_follow_up_agent(session, deal) -> FollowUpDecision:
    """Read conversation and return a structured follow-up decision.

    Assumes the caller has already synced the conversation, so the local
    ``ChatMessage`` rows are current. The agent is handed the full thread
    verbatim plus ``deal.profile_summary``, and asked to decide.
    """
    from pydantic_ai import Agent

    public_id = deal.lead.public_identifier
    deal.refresh_from_db(fields=["profile_summary"])

    messages, omitted = _load_conversation(deal)
    logger.info(
        "follow_up agent for %s: %d message(s) in transcript (%d omitted)",
        public_id, len(messages), omitted,
    )
    system_prompt = _render_system_prompt(session, deal, messages, omitted)

    agent = Agent(
        get_llm_model("expensive"),
        output_type=FollowUpDecision,
        # Low temperature: this model composes text that goes out under the
        # user's own name. Sampling diversity here buys nothing and is what
        # lets it drift off the transcript.
        model_settings={"temperature": 0.2, "timeout": 60, "thinking": "xhigh"},
    )
    decision = run_agent_sync(agent.run(system_prompt)).output
    if decision is None:
        raise RuntimeError(f"LLM returned unparseable response for follow-up of {public_id}")
    if decision.message:
        decision.message = _replace_em_dashes(decision.message)

    logger.info("follow_up agent for %s: %s", public_id, decision.action)
    return decision


if __name__ == "__main__":
    from crm.models import Deal
    from linkedin.browser.registry import cli_parser, cli_session
    from linkedin.db.chat import sync_conversation
    from linkedin.db.summaries import materialize_profile_summary_if_missing
    from linkedin.models import Task

    parser = cli_parser("Run the follow-up agent for a profile")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", help="Public identifier of the target profile")
    group.add_argument("--task-id", type=int, help="Task ID to run the agent for")
    args = parser.parse_args()
    session = cli_session(args)
    session.ensure_browser()

    if args.task_id:
        task = Task.objects.get(pk=args.task_id)
        public_id = task.payload["public_id"]
        campaign_id = task.payload["campaign_id"]
        from linkedin.models import Campaign
        campaign = Campaign.objects.get(pk=campaign_id)
        session.campaign = campaign
    else:
        public_id = args.profile

    deal = (
        Deal.objects.filter(lead__public_identifier=public_id, campaign=session.campaign)
        .select_related("lead", "campaign")
        .first()
    )
    if not deal:
        logger.error("No Deal found for %s", public_id)
        raise SystemExit(1)

    logger.info("Running follow-up agent as %s for %s", session, public_id)
    logger.info("Campaign: %s", session.campaign)

    sync_conversation(session, public_id)
    materialize_profile_summary_if_missing(deal, session)
    decision = run_follow_up_agent(session, deal)

    logger.info("Profile facts: %s", _format_facts(deal.profile_summary))
    logger.info("Action: %s", decision.action)
    if decision.message:
        logger.info("Message: %s", decision.message)
    if decision.outcome:
        logger.info("Outcome: %s", decision.outcome)
    logger.info("Follow-up in: %sh", decision.follow_up_hours)
