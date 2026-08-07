# webadmin/dashboard.py
"""Home page — ports the 4 stat counts from linkedin/dashboard.py."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from webadmin.auth import require_login
from webadmin.db import get_session
from webadmin.models import ActionLog, ChatMessage, Deal
from webadmin.templates_env import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
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

    return templates.TemplateResponse(request, "dashboard.html", {
        "messages_sent": messages_sent,
        "replies_received": replies_received,
        "connect_requests_sent": connect_requests_sent,
        "connect_requests_accepted": connect_requests_accepted,
        "flash": request.session.pop("flash", None),
        "user": user,
    })
