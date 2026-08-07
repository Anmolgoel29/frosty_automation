# webadmin/main.py
"""FastAPI entrypoint — replaces `manage.py runserver` as the admin web server.

Run with: uvicorn webadmin.main:app --host 0.0.0.0 --port 8000
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from webadmin import admins  # noqa: F401 — populates registry.REGISTRY on import
from webadmin.auth import NotAuthenticated, login_redirect
from webadmin.auth import router as auth_router
from webadmin.config import SECRET_KEY, SESSION_COOKIE_NAME
from webadmin.csrf import CSRFCookieMiddleware
from webadmin.dashboard import router as dashboard_router
from webadmin.db import engine
from webadmin.registry import REGISTRY
from webadmin.templates_env import templates
from webadmin.views import build_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie=SESSION_COOKIE_NAME)
app.add_middleware(CSRFCookieMiddleware)

app.mount("/admin/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="admin-static")

templates.env.globals["nav_items"] = lambda: [a for a in REGISTRY.values() if not a.hidden_from_nav]


@app.exception_handler(NotAuthenticated)
async def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return login_redirect(exc)


@app.get("/")
async def root():
    return RedirectResponse("/admin/")


app.include_router(auth_router, prefix="/admin")
app.include_router(dashboard_router, prefix="/admin")
for model_admin in REGISTRY.values():
    app.include_router(build_router(model_admin), prefix="/admin")
