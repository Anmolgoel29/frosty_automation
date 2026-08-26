# webadmin/dashboard.py
"""Home page — global stat counts, a per-profile breakdown, and a daily trend table."""
from datetime import datetime, time, timedelta, timezone as dt_timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from webadmin.auth import require_login
from webadmin.db import get_session
from webadmin.models import ActionLog, ChatMessage, Deal, Lead, LinkedInProfile
from webadmin.templates_env import templates

router = APIRouter()

DAILY_WINDOW_DAYS = 14


async def _global_stats(session: AsyncSession) -> dict:
    messages_sent = (await session.execute(
        select(func.count()).select_from(ChatMessage).where(ChatMessage.is_outgoing.is_(True)),
    )).scalar_one()
    replies_received = (await session.execute(
        select(func.count()).select_from(ChatMessage).where(ChatMessage.is_outgoing.is_(False)),
    )).scalar_one()
    connect_requests_sent = (await session.execute(
        select(func.count()).select_from(ActionLog).where(ActionLog.action_type == "connect"),
    )).scalar_one()
    connect_requests_accepted = (await session.execute(
        select(func.count()).select_from(Deal).where(Deal.state.in_(["Connected", "Completed"])),
    )).scalar_one()
    profiles_scanned = (await session.execute(
        select(func.count()).select_from(Lead),
    )).scalar_one()
    leads_messaged = (await session.execute(
        select(func.count(func.distinct(ChatMessage.object_id))).where(ChatMessage.is_outgoing.is_(True)),
    )).scalar_one()
    leads_replied = (await session.execute(
        select(func.count(func.distinct(ChatMessage.object_id))).where(ChatMessage.is_outgoing.is_(False)),
    )).scalar_one()

    return {
        "messages_sent": messages_sent,
        "replies_received": replies_received,
        "connect_requests_sent": connect_requests_sent,
        "connect_requests_accepted": connect_requests_accepted,
        "profiles_scanned": profiles_scanned,
        "leads_messaged": leads_messaged,
        "leads_replied": leads_replied,
    }


async def _per_profile_stats(session: AsyncSession) -> list[dict]:
    """Per-account breakdown.

    ChatMessage has no direct linkedin_profile FK — a message only names the
    Lead it belongs to (object_id). The owning account is recovered via
    Deal.assigned_profile (joined on lead_id == ChatMessage.object_id):
    per ARCHITECTURE.md, ownership is sticky and never crosses over, so the
    conversation for a lead only ever lives on the account whose Deal it's
    assigned to.
    """
    outgoing = case((ChatMessage.is_outgoing.is_(True), 1), else_=0)
    incoming = case((ChatMessage.is_outgoing.is_(False), 1), else_=0)
    messaged_lead = case((ChatMessage.is_outgoing.is_(True), ChatMessage.object_id), else_=None)
    replied_lead = case((ChatMessage.is_outgoing.is_(False), ChatMessage.object_id), else_=None)

    chat_stats = (
        select(
            Deal.assigned_profile_id.label("profile_id"),
            func.sum(outgoing).label("messages_sent"),
            func.sum(incoming).label("replies_received"),
            func.count(func.distinct(messaged_lead)).label("leads_messaged"),
            func.count(func.distinct(replied_lead)).label("leads_replied"),
        )
        .select_from(ChatMessage)
        .join(Deal, Deal.lead_id == ChatMessage.object_id)
        .where(Deal.assigned_profile_id.is_not(None))
        .group_by(Deal.assigned_profile_id)
        .subquery()
    )

    connect_sent = (
        select(
            ActionLog.linkedin_profile_id.label("profile_id"),
            func.count().label("connect_requests_sent"),
        )
        .where(ActionLog.action_type == "connect")
        .group_by(ActionLog.linkedin_profile_id)
        .subquery()
    )

    connect_accepted = (
        select(
            Deal.assigned_profile_id.label("profile_id"),
            func.count().label("connect_requests_accepted"),
        )
        .where(Deal.state.in_(["Connected", "Completed"]))
        .group_by(Deal.assigned_profile_id)
        .subquery()
    )

    rows = (await session.execute(
        select(
            LinkedInProfile.id,
            LinkedInProfile.linkedin_username,
            LinkedInProfile.active,
            func.coalesce(chat_stats.c.messages_sent, 0),
            func.coalesce(chat_stats.c.replies_received, 0),
            func.coalesce(chat_stats.c.leads_messaged, 0),
            func.coalesce(chat_stats.c.leads_replied, 0),
            func.coalesce(connect_sent.c.connect_requests_sent, 0),
            func.coalesce(connect_accepted.c.connect_requests_accepted, 0),
        )
        .outerjoin(chat_stats, chat_stats.c.profile_id == LinkedInProfile.id)
        .outerjoin(connect_sent, connect_sent.c.profile_id == LinkedInProfile.id)
        .outerjoin(connect_accepted, connect_accepted.c.profile_id == LinkedInProfile.id)
        .order_by(LinkedInProfile.linkedin_username)
    )).all()

    return [
        {
            "id": row[0],
            "linkedin_username": row[1],
            "active": row[2],
            "messages_sent": row[3],
            "replies_received": row[4],
            "leads_messaged": row[5],
            "leads_replied": row[6],
            "connect_requests_sent": row[7],
            "connect_requests_accepted": row[8],
        }
        for row in rows
    ]


async def _daily_stats(session: AsyncSession, days: int = DAILY_WINDOW_DAYS) -> list[dict]:
    """Daily trend for the last `days` days (UTC calendar days, newest first).

    Connect-request *accepted* has no per-event timestamp anywhere in the
    schema (ActionLog only logs 'connect'/'follow_up' sends, and Deal.update_date
    is bumped by unrelated later writes — chat_summary updates, fit_score, etc.
    — so it can't stand in for "day the invite was accepted"). Left out of this
    table rather than shown as a misleading number; the lifetime total is still
    on the global tile above.
    """
    start_day = datetime.now(dt_timezone.utc).date() - timedelta(days=days - 1)
    window_start = datetime.combine(start_day, time.min, tzinfo=dt_timezone.utc)

    outgoing = case((ChatMessage.is_outgoing.is_(True), 1), else_=0)
    incoming = case((ChatMessage.is_outgoing.is_(False), 1), else_=0)
    messaged_lead = case((ChatMessage.is_outgoing.is_(True), ChatMessage.object_id), else_=None)
    replied_lead = case((ChatMessage.is_outgoing.is_(False), ChatMessage.object_id), else_=None)
    chat_day = func.date(ChatMessage.creation_date)

    chat_rows = (await session.execute(
        select(
            chat_day.label("day"),
            func.sum(outgoing).label("messages_sent"),
            func.sum(incoming).label("replies_received"),
            func.count(func.distinct(messaged_lead)).label("leads_messaged"),
            func.count(func.distinct(replied_lead)).label("leads_replied"),
        )
        .where(ChatMessage.creation_date >= window_start)
        .group_by(chat_day)
    )).all()
    chat_by_day = {row.day: row for row in chat_rows}

    lead_day = func.date(Lead.creation_date)
    lead_rows = (await session.execute(
        select(lead_day.label("day"), func.count().label("profiles_scanned"))
        .where(Lead.creation_date >= window_start)
        .group_by(lead_day)
    )).all()
    scanned_by_day = {row.day: row.profiles_scanned for row in lead_rows}

    connect_day = func.date(ActionLog.created_at)
    connect_rows = (await session.execute(
        select(connect_day.label("day"), func.count().label("connect_requests_sent"))
        .where(ActionLog.action_type == "connect", ActionLog.created_at >= window_start)
        .group_by(connect_day)
    )).all()
    connects_by_day = {row.day: row.connect_requests_sent for row in connect_rows}

    result = []
    for offset in range(days):
        d = start_day + timedelta(days=offset)
        chat = chat_by_day.get(d)
        result.append({
            "day": d.isoformat(),
            "profiles_scanned": scanned_by_day.get(d, 0),
            "leads_messaged": chat.leads_messaged if chat else 0,
            "leads_replied": chat.leads_replied if chat else 0,
            "messages_sent": chat.messages_sent if chat else 0,
            "replies_received": chat.replies_received if chat else 0,
            "connect_requests_sent": connects_by_day.get(d, 0),
        })
    result.reverse()
    return result


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
    context = await _global_stats(session)
    context["profiles"] = await _per_profile_stats(session)
    context["daily"] = await _daily_stats(session)
    context["daily_window_days"] = DAILY_WINDOW_DAYS
    context["flash"] = request.session.pop("flash", None)
    context["user"] = user

    return templates.TemplateResponse(request, "dashboard.html", context)
