# webadmin/chat_views.py
"""Chat list + per-lead thread views, and the manual-send endpoint.

Not a ModelAdmin resource — this is a hand-written router in the same style
as dashboard.py, since the generic list/add/edit/delete engine in
webadmin/registry.py + views.py has no custom-detail-page mechanism and
ChatMessage has no direct Lead/LinkedInProfile FK to build one generically
from (see ARCHITECTURE.md's Admin panel section).

Manual sends never touch LinkedIn from this process — this process has no
browser. Sending a message is: insert a `manual_send` Task row into the
mirrored `linkedin_task` table (the daemon picks it up on its next
QUEUE_POLL_INTERVAL poll, same as TaskAdmin.run_now), and eagerly flip
Lead.human_takeover so a still-due AI follow_up can't fire a conflicting
message in the window before the daemon gets to it.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webadmin.auth import require_login
from webadmin.csrf import validate_csrf
from webadmin.db import get_session
from webadmin.models import ChatMessage, Deal, Lead, LinkedInProfile, Task
from webadmin.templates_env import templates

router = APIRouter(prefix="/chat")

PREVIEW_LENGTH = 120


async def _apply_csrf_check(request: Request, form) -> None:
    validate_csrf(request, form.get("csrf_token"))


async def _latest_deal_for_lead(session: AsyncSession, lead_id: int) -> Deal | None:
    """Most-recently-updated Deal for a lead — used to find its owning account.

    A lead can in principle have Deals in more than one campaign; chat is
    lead-scoped (not campaign-scoped), so this picks whichever Deal was
    touched most recently as the one that owns the conversation right now.
    """
    return (
        await session.execute(
            select(Deal).where(Deal.lead_id == lead_id).order_by(Deal.update_date.desc()).limit(1),
        )
    ).scalars().first()


async def _list_active_threads(session: AsyncSession) -> list[dict]:
    """One row per CONNECTED lead, with its latest message as a preview.

    Postgres DISTINCT ON (this stack is Postgres-only, see ARCHITECTURE.md's
    Database section) picks the latest ChatMessage per lead in one query.
    "Needs reply" is approximated as "the latest message is incoming" — the
    same proxy linkedin/tasks/follow_up.py:_too_soon_to_nudge already uses;
    there's no real LinkedIn read/unread signal synced locally.
    """
    latest = (
        select(
            ChatMessage.object_id.label("lead_id"),
            ChatMessage.content.label("last_content"),
            ChatMessage.creation_date.label("last_activity"),
            ChatMessage.is_outgoing.label("last_is_outgoing"),
        )
        .distinct(ChatMessage.object_id)
        .order_by(ChatMessage.object_id, ChatMessage.creation_date.desc())
        .subquery()
    )
    stmt = (
        select(
            Lead.id, Lead.public_identifier, Lead.human_takeover,
            LinkedInProfile.linkedin_username,
            latest.c.last_content, latest.c.last_activity, latest.c.last_is_outgoing,
        )
        .select_from(Deal)
        .join(Lead, Lead.id == Deal.lead_id)
        .join(latest, latest.c.lead_id == Deal.lead_id)
        .outerjoin(LinkedInProfile, LinkedInProfile.id == Deal.assigned_profile_id)
        .where(Deal.state == "Connected")
        .order_by(latest.c.last_activity.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "lead_id": r[0],
            "public_identifier": r[1],
            "human_takeover": r[2],
            "account": r[3] or "—",
            "preview": (r[4] or "")[:PREVIEW_LENGTH],
            "last_activity": r[5],
            "needs_reply": r[6] is False,
        }
        for r in rows
    ]


@router.get("", response_class=HTMLResponse)
async def chat_list(request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
    threads = await _list_active_threads(session)
    return templates.TemplateResponse(request, "chat_list.html", {
        "threads": threads,
        "flash": request.session.pop("flash", None),
        "user": user,
    })


def _annotate_replied(rows) -> list[dict]:
    """Chronological ChatMessage rows -> dicts carrying a `replied` flag.

    An outgoing message is "replied" once any incoming message follows it in
    the conversation — not necessarily the very next message, since a burst
    of outgoing nudges before one incoming reply all count as answered.
    `None` for incoming messages, since "replied" isn't a meaningful concept
    for them. Walking in reverse means one pass computes it for the whole
    list, tracking only "has an incoming message been seen yet" as we go.
    """
    out = []
    seen_incoming = False
    for m in reversed(rows):
        out.append({
            "id": m.id,
            "content": m.content,
            "is_outgoing": m.is_outgoing,
            "authored_by": m.authored_by,
            "creation_date": m.creation_date,
            "replied": seen_incoming if m.is_outgoing else None,
        })
        if not m.is_outgoing:
            seen_incoming = True
    out.reverse()
    return out


@router.get("/{lead_id}", response_class=HTMLResponse)
async def chat_thread(lead_id: int, request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found.")

    deal = await _latest_deal_for_lead(session, lead_id)
    rows = (
        await session.execute(
            select(ChatMessage).where(ChatMessage.object_id == lead_id).order_by(ChatMessage.creation_date),
        )
    ).scalars().all()

    return templates.TemplateResponse(request, "chat_thread.html", {
        "lead": lead,
        "deal": deal,
        "messages": _annotate_replied(rows),
        "flash": request.session.pop("flash", None),
        "user": user,
    })


@router.get("/{lead_id}/messages.json")
async def chat_messages_json(
    lead_id: int, after_id: int = 0,
    session: AsyncSession = Depends(get_session), user=Depends(require_login),
):
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found.")

    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.object_id == lead_id, ChatMessage.id > after_id)
            .order_by(ChatMessage.creation_date),
        )
    ).scalars().all()

    # "replied" here is only correct within this batch (a suffix of the full
    # conversation, since it's filtered on id > after_id) — an outgoing
    # message already sent to the client in an earlier poll needs its status
    # flipped client-side when a later incoming message arrives; see
    # chat_thread.html's poll handler.
    annotated = _annotate_replied(rows)

    return JSONResponse({
        "human_takeover": lead.human_takeover,
        "messages": [
            {**m, "creation_date": m["creation_date"].isoformat() if m["creation_date"] else None}
            for m in annotated
        ],
    })


@router.post("/{lead_id}/human_takeover")
async def human_takeover_on(lead_id: int, request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
    form = await request.form()
    await _apply_csrf_check(request, form)
    await session.execute(update(Lead).where(Lead.id == lead_id).values(human_takeover=True))
    await session.commit()
    request.session["flash"] = "Human takeover enabled — the AI will stop messaging this lead."
    return RedirectResponse(f"/admin/chat/{lead_id}", status_code=303)


@router.post("/{lead_id}/resume_ai")
async def human_takeover_off(lead_id: int, request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
    form = await request.form()
    await _apply_csrf_check(request, form)
    await session.execute(update(Lead).where(Lead.id == lead_id).values(human_takeover=False))
    await session.commit()
    request.session["flash"] = "AI follow-up resumed for this lead."
    return RedirectResponse(f"/admin/chat/{lead_id}", status_code=303)


@router.post("/{lead_id}/send")
async def send_message(
    lead_id: int, request: Request,
    message: str = Form(...), csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session), user=Depends(require_login),
):
    validate_csrf(request, csrf_token)

    message = message.strip()
    if not message:
        request.session["flash"] = "Message was empty — nothing sent."
        return RedirectResponse(f"/admin/chat/{lead_id}", status_code=303)

    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found.")

    deal = await _latest_deal_for_lead(session, lead_id)
    if deal is None or deal.assigned_profile_id is None:
        request.session["flash"] = (
            "This lead has no account assigned yet — it hasn't been connected with, "
            "so there's no browser session to send from."
        )
        return RedirectResponse(f"/admin/chat/{lead_id}", status_code=303)

    now = datetime.now(dt_timezone.utc)
    session.add(Task(
        task_type="manual_send",
        linkedin_profile_id=deal.assigned_profile_id,
        status="pending",
        scheduled_at=now,
        # campaign_id is required: daemon.py's _run_task looks it up
        # unconditionally before dispatching to any handler and fails the
        # task if it's missing from the payload.
        payload={"lead_id": lead.id, "message": message, "campaign_id": deal.campaign_id},
        created_at=now,
    ))
    # Set eagerly, in the same request, not only when the daemon eventually
    # runs the task (~60s later) — otherwise an already-due AI follow_up
    # could still fire in that window and send a conflicting message right
    # after the admin decided to take over.
    await session.execute(update(Lead).where(Lead.id == lead_id).values(human_takeover=True))
    await session.commit()

    request.session["flash"] = "Message queued — the daemon will send it within about a minute."
    return RedirectResponse(f"/admin/chat/{lead_id}", status_code=303)
