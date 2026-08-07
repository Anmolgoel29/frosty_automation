# webadmin/config.py
"""Env-driven settings for the FastAPI admin.

Deliberately independent of Django's settings module for DB access — this
process talks to Postgres directly via its own async SQLAlchemy engine,
reading the same `DATABASE_URL` (see `db_url.py`) Django's settings resolve so
both processes point at the identical database without importing Django at all
for connectivity. `DJANGO_SECRET_KEY` is reused (not a new secret to manage)
for session-cookie and CSRF-token signing.
"""
import os

from db_url import sqlalchemy_async_url

# (url, connect_args) — connect_args carries e.g. asyncpg's ssl arg for Supabase.
DATABASE_URL, DATABASE_CONNECT_ARGS = sqlalchemy_async_url()

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "openoutreach-local-dev-key-change-in-production",
)

SESSION_COOKIE_NAME = "webadmin_session"
CSRF_COOKIE_NAME = "webadmin_csrf"

PAGE_SIZE = 50
