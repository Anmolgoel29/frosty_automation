# webadmin/admins.py
"""The 9 ModelAdmin configs — a 1:1 port of linkedin/admin.py + crm/admin.py,
with the deviations noted inline (Task/ActionLog/ChatMessage drop the Add
button since every field was already readonly there; ChatMessage's
content_type filter/column is dropped since it only ever targets Lead).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webadmin.models import ActionLog, Campaign, ChatMessage, Deal, Lead, LinkedInProfile, SearchKeyword, SiteConfig, Task
from webadmin.registry import AdminAction, ModelAdmin, register


class LLMProvider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    GROQ = "groq"
    MISTRAL = "mistral"
    COHERE = "cohere"
    OPENAI_COMPATIBLE = "openai_compatible"


class ActionType(str, Enum):
    CONNECT = "connect"
    FOLLOW_UP = "follow_up"


class TaskType(str, Enum):
    CONNECT = "connect"
    CHECK_PENDING = "check_pending"
    FOLLOW_UP = "follow_up"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DealState(str, Enum):
    QUALIFIED = "Qualified"
    READY_TO_CONNECT = "Ready to Connect"
    PENDING = "Pending"
    CONNECTED = "Connected"
    COMPLETED = "Completed"
    FAILED = "Failed"


class Outcome(str, Enum):
    CONVERTED = "converted"
    NOT_INTERESTED = "not_interested"
    WRONG_FIT = "wrong_fit"
    NO_BUDGET = "no_budget"
    HAS_SOLUTION = "has_solution"
    BAD_TIMING = "bad_timing"
    UNRESPONSIVE = "unresponsive"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Custom bulk actions — ported verbatim from linkedin/admin.py / crm/admin.py.
# ---------------------------------------------------------------------------

async def _run_now(session: AsyncSession, ids: list[int]) -> str:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(Task).where(Task.id.in_(ids), Task.status == TaskStatus.PENDING.value).values(scheduled_at=now),
    )
    updated = result.rowcount
    skipped = len(ids) - updated
    await session.commit()
    parts = []
    if updated:
        parts.append(
            f"{updated} task(s) rescheduled to run now — the daemon will "
            "pick them up within about a minute.",
        )
    if skipped:
        parts.append(f"{skipped} task(s) skipped — only pending tasks can be run now.")
    return " ".join(parts) or "No tasks updated."


async def _mark_human_takeover(session: AsyncSession, ids: list[int]) -> str:
    disqualified_ids = (
        await session.execute(
            select(Lead.public_identifier).where(Lead.id.in_(ids), Lead.disqualified.is_(True)),
        )
    ).scalars().all()
    result = await session.execute(
        update(Lead).where(Lead.id.in_(ids), Lead.disqualified.is_(False)).values(human_takeover=True),
    )
    updated = result.rowcount
    await session.commit()
    parts = []
    if disqualified_ids:
        parts.append(
            f"Skipped {len(disqualified_ids)} permanently-disqualified lead(s) — "
            f"they have no active AI conversation: {', '.join(disqualified_ids)}",
        )
    if updated:
        parts.append(f"Successfully marked {updated} lead(s) for Human Takeover. AI will stop sending messages.")
    return " ".join(parts) or "No leads updated."


async def _resume_ai(session: AsyncSession, ids: list[int]) -> str:
    result = await session.execute(update(Lead).where(Lead.id.in_(ids)).values(human_takeover=False))
    await session.commit()
    return f"Successfully resumed AI for {result.rowcount} lead(s)."


# ---------------------------------------------------------------------------
# ModelAdmin registrations.
# ---------------------------------------------------------------------------

register(ModelAdmin(
    model=SiteConfig,
    slug="siteconfig",
    label="Site Configuration",
    list_display=["__str__", "chat_llm_provider", "chat_ai_model", "task_llm_provider", "task_ai_model"],
    choices={"chat_llm_provider": LLMProvider, "task_llm_provider": LLMProvider},
    can_add=False,
    can_delete=False,
    hidden_from_nav=True,
))

register(ModelAdmin(
    model=Campaign,
    slug="campaign",
    label="Campaign",
    list_display=["name", "booking_link", "is_freemium", "action_fraction"],
    m2m_fields=["users"],
))

register(ModelAdmin(
    model=LinkedInProfile,
    slug="linkedinprofile",
    label="LinkedIn Profile",
    list_display=["user", "linkedin_username", "active", "legal_accepted"],
    list_filter=["active"],
    raw_id_fields=["user", "self_lead"],
))

register(ModelAdmin(
    model=SearchKeyword,
    slug="searchkeyword",
    label="Search Keyword",
    list_display=["keyword", "campaign", "used", "used_at"],
    list_filter=["used", "campaign"],
    raw_id_fields=["campaign"],
))

register(ModelAdmin(
    model=ActionLog,
    slug="actionlog",
    label="Action Log",
    list_display=["action_type", "linkedin_profile", "campaign", "created_at"],
    list_filter=["action_type", "campaign"],
    raw_id_fields=["linkedin_profile", "campaign"],
    date_hierarchy="created_at",
    readonly_fields=["linkedin_profile", "campaign", "action_type", "created_at"],
    choices={"action_type": ActionType},
    can_add=False,
))

register(ModelAdmin(
    model=Task,
    slug="task",
    label="Task",
    list_display=["task_type", "status", "scheduled_at", "payload", "created_at"],
    list_filter=["task_type", "status"],
    readonly_fields=["task_type", "status", "scheduled_at", "payload", "created_at", "started_at", "completed_at"],
    date_hierarchy="scheduled_at",
    choices={"task_type": TaskType, "status": TaskStatus},
    can_add=False,
    actions=[AdminAction(name="run_now", label="Run now (jump the queue)", handler=_run_now)],
))

register(ModelAdmin(
    model=ChatMessage,
    slug="chatmessage",
    label="Chat Message",
    list_display=["object_id", "owner", "creation_date", "is_outgoing"],
    list_filter=["owner"],
    raw_id_fields=["owner", "answer_to", "topic"],
    date_hierarchy="creation_date",
    readonly_fields=["object_id", "content", "owner", "creation_date"],
    exclude_fields=["content_type_id"],
    can_add=False,
))

register(ModelAdmin(
    model=Lead,
    slug="lead",
    label="Lead",
    list_display=["public_identifier", "linkedin_url", "disqualified", "human_takeover"],
    list_filter=["disqualified", "human_takeover"],
    search_fields=["public_identifier", "linkedin_url", "urn"],
    exclude_fields=["update_date"],
    actions=[
        AdminAction(name="mark_human_takeover", label="Human Takeover (Stop AI messages)", handler=_mark_human_takeover),
        AdminAction(name="resume_ai", label="Resume AI messages", handler=_resume_ai),
    ],
))

register(ModelAdmin(
    model=Deal,
    slug="deal",
    label="Deal",
    list_display=["lead", "campaign", "state", "outcome", "connect_attempts"],
    list_filter=["state", "outcome", "campaign"],
    search_fields=["lead__public_identifier", "reason"],
    exclude_fields=["update_date"],
    choices={"state": DealState, "outcome": Outcome},
    blank_choice_fields=["outcome"],
))
