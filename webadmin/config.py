# webadmin/config.py
"""Env-driven settings for the FastAPI admin.

Deliberately independent of Django's settings module for DB access — this
process talks to Postgres directly via its own async SQLAlchemy engine,
reading the same POSTGRES_* env vars Django's settings already use so both
processes point at the identical database without importing Django at all
for connectivity. `DJANGO_SECRET_KEY` is reused (not a new secret to manage)
for session-cookie and CSRF-token signing.
"""
import os

POSTGRES_DB = os.environ.get("POSTGRES_DB", "openoutreach")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "openoutreach")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "openoutreach")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")

DATABASE_URL = (
    f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "openoutreach-local-dev-key-change-in-production",
)

SESSION_COOKIE_NAME = "webadmin_session"
CSRF_COOKIE_NAME = "webadmin_csrf"

PAGE_SIZE = 50
