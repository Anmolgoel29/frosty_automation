"""Single source of truth for the Postgres connection, driven by DATABASE_URL.

Set `DATABASE_URL` to point the whole app — Django (ORM + migrations) and the
FastAPI admin's SQLAlchemy engine — at any Postgres, whether the bundled local
container or a hosted one like Supabase / Neon / RDS:

    DATABASE_URL=postgresql://user:pass@host:5432/dbname
    # Supabase (SSL required — note the sslmode query param):
    DATABASE_URL=postgresql://postgres:pw@db.abcd.supabase.co:5432/postgres?sslmode=require

When `DATABASE_URL` is unset it is assembled from the `POSTGRES_*` vars so local
dev and the bundled `db` compose service keep working with zero config.

Pure stdlib (urllib only) and dependency-free so it can be imported from both
`linkedin/django_settings.py` and `webadmin/config.py` without either pulling in
the other's stack (no Django import from webadmin, no SQLAlchemy import here).
"""
import os
from urllib.parse import parse_qs, unquote, urlparse, urlunparse


def get_database_url() -> str:
    """The raw connection URL — `DATABASE_URL` if set, else built from POSTGRES_*."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    user = os.environ.get("POSTGRES_USER", "openoutreach")
    password = os.environ.get("POSTGRES_PASSWORD", "openoutreach")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "openoutreach")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def django_db_config(url: str | None = None) -> dict:
    """A Django `DATABASES['default']` dict for the given (or ambient) URL."""
    parts = urlparse(url or get_database_url())
    config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": unquote(parts.hostname or ""),
        "PORT": str(parts.port or ""),
        # Always present: the postgres backend reads settings_dict["OPTIONS"]
        # unconditionally, and Django only fills defaults for the DATABASES it
        # sets up itself — a dict injected straight into connections.databases
        # (e.g. the migration helpers' verification alias) must carry it already.
        "OPTIONS": {},
    }
    # psycopg understands `sslmode` natively; pass it straight through.
    sslmode = parse_qs(parts.query).get("sslmode", [None])[0]
    if sslmode:
        config["OPTIONS"]["sslmode"] = sslmode
    return config


def sqlalchemy_async_url(url: str | None = None) -> tuple[str, dict]:
    """A `(url, connect_args)` pair for `create_async_engine`.

    Rewrites the scheme to `postgresql+asyncpg` (whatever it came in as) and
    lifts a `sslmode` query param into asyncpg's `ssl` connect arg — asyncpg
    accepts the same sslmode strings (require / verify-full / ...) there but
    does not understand `sslmode` in the URL query itself.
    """
    parts = urlparse(url or get_database_url())
    async_url = urlunparse(parts._replace(scheme="postgresql+asyncpg", query=""))
    connect_args: dict = {}
    sslmode = parse_qs(parts.query).get("sslmode", [None])[0]
    if sslmode:
        connect_args["ssl"] = sslmode
    return async_url, connect_args
