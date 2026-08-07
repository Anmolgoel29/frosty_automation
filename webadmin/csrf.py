# webadmin/csrf.py
"""Double-submit-cookie CSRF protection.

Django's CsrfViewMiddleware disappears along with django.contrib.admin, so
this is a real (not optional) replacement: every POST must carry a
csrf_token form field matching the csrf_token cookie. The cookie is an
unguessable random value, not attacker-settable cross-origin, so a match
proves the form was rendered by us for this browser.
"""
import hmac
import secrets

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from webadmin.config import CSRF_COOKIE_NAME


class CSRFCookieMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = request.cookies.get(CSRF_COOKIE_NAME)
        is_new = token is None
        if is_new:
            token = secrets.token_urlsafe(32)
        request.state.csrf_token = token
        response: Response = await call_next(request)
        if is_new:
            response.set_cookie(CSRF_COOKIE_NAME, token, httponly=False, samesite="lax")
        return response


def validate_csrf(request: Request, submitted_token: str | None) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token or not hmac.compare_digest(cookie_token, submitted_token):
        raise HTTPException(status_code=403, detail="CSRF verification failed — please retry.")
