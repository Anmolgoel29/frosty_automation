# webadmin/auth.py
"""Login/logout + the require_login dependency.

Credentials are checked against the existing auth_user table (same rows
DJANGO_SUPERUSER_* / `manage.py createsuperuser` already populate) using
Django's own password hasher — that's a pure function, so we do a minimal
`django.setup()` purely to read settings.PASSWORD_HASHERS, never to run
ORM queries from this process.
"""
from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webadmin.csrf import validate_csrf
from webadmin.db import get_session
from webadmin.models import User
from webadmin.templates_env import templates

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")
import django  # noqa: E402

django.setup()

from django.contrib.auth.hashers import check_password  # noqa: E402

router = APIRouter()


class NotAuthenticated(Exception):
    def __init__(self, next_path: str = "/admin/"):
        self.next_path = next_path


def login_redirect(exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(f"/admin/login?next={quote(exc.next_path)}", status_code=303)


@router.get("/login")
async def login_form(request: Request, next: str = "/admin/"):
    if request.session.get("user_id"):
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin/"),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    validate_csrf(request, csrf_token)
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()

    valid = user is not None and user.is_active and user.is_staff and check_password(password, user.password)
    if not valid:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "Invalid username/password, or account lacks staff access."},
            status_code=401,
        )

    request.session["user_id"] = user.id
    return RedirectResponse(next or "/admin/", status_code=303)


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


async def require_login(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    user_id = request.session.get("user_id")
    if user_id is None:
        raise NotAuthenticated(next_path=str(request.url.path))
    user = await session.get(User, user_id)
    if user is None or not user.is_active or not user.is_staff:
        request.session.clear()
        raise NotAuthenticated(next_path=str(request.url.path))
    return user
