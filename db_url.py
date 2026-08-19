"""Single source of truth for the Postgres connection, driven by DATABASE_URL.

Set `DATABASE_URL` to point the whole app — Django (ORM + migrations) and the
FastAPI admin's SQLAlchemy engine — at any Postgres, whether the bundled local
container or a hosted one like Supabase / Neon / RDS:

    DATABASE_URL=postgresql://user:pass@host:5432/dbname
    # Supabase (SSL required — note the sslmode query param):
    DATABASE_URL=postgresql://postgres:pw@db.abcd.supabase.co:5432/postgres?sslmode=require

When `DATABASE_URL` is unset it is assembled from the `POSTGRES_*` vars so local
dev and the bundled `db` compose service keep working with zero config.

Both consumers (`django_db_config`, `sqlalchemy_async_url`) log a one-line boot
diagnostic — masked URL, resolved host/db/user/sslmode, and which source it came
from — so `make logs` shows exactly which database each process connected to.

Pure stdlib (urllib only) and dependency-free so it can be imported from both
`linkedin/django_settings.py` and `webadmin/config.py` without either pulling in
the other's stack (no Django import from webadmin, no SQLAlchemy import here).
"""
import logging
import os
import sys
from urllib.parse import parse_qs, unquote, urlparse, urlunparse
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _emit(message: str) -> None:
    """Print a boot diagnostic where it will actually be seen.

    This module resolves the DB connection while Django *settings* are being
    imported — before Django (or uvicorn) has configured logging — so a plain
    `logger.info` is swallowed at exactly the moment you want to see it. We log
    through the logger too (for anyone who has configured handlers) but also
    write to stderr so it always shows up in `make logs` / the console. stderr,
    not stdout, so it never corrupts a command's real output (e.g. dumpdata).
    """
    logger.info(message)
    print(message, file=sys.stderr, flush=True)


def _mask(url: str) -> str:
    """The URL with its password redacted — safe to print to logs."""
    parts = urlparse(url)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
        parts = parts._replace(netloc=netloc)
    return urlunparse(parts)


def _source(explicit: str | None) -> str:
    """Human label for where the resolved URL came from, for the boot log."""
    if explicit:
        return "explicit url argument"
    if os.environ.get("DATABASE_URL"):
        return "DATABASE_URL env var"
    return "POSTGRES_* env vars (DATABASE_URL unset)"


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
    }
    # psycopg understands `sslmode` natively; pass it straight through.
    sslmode = parse_qs(parts.query).get("sslmode", [None])[0]
    if sslmode:
        config["OPTIONS"] = {"sslmode": sslmode}
    _emit(
        f"[db_url] Django DATABASES -> host={config['HOST'] or '(none)'} "
        f"port={config['PORT'] or '(none)'} db={config['NAME'] or '(none)'} "
        f"user={config['USER'] or '(none)'} sslmode={sslmode or 'none'} "
        f"(source: {_source(url)})"
    )
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
    _emit(
        f"[db_url] SQLAlchemy(asyncpg) -> {_mask(async_url)} "
        f"ssl={sslmode or 'none'} (source: {_source(url)})"
    )
    return async_url, connect_args
