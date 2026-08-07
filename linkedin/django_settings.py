# linkedin/django_settings.py
"""
Minimal Django settings for the ORM + migrations. Django no longer serves
HTTP — the admin panel is webadmin/ (FastAPI), a separate process that reads
this same Postgres database via its own SQLAlchemy layer.
"""
import os
import sys
from pathlib import Path

# Playwright's sync API runs inside an async event loop, which triggers
# Django's async-safety check. We only use the ORM synchronously, so this is safe.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

ROOT_DIR = Path(__file__).resolve().parent.parent

BASE_DIR = ROOT_DIR

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "openoutreach-local-dev-key-change-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "crm.apps.CrmConfig",
    "chat.apps.ChatConfig",
    "linkedin",
]

# The `migrate_sqlite_to_postgres` upgrade helper points `default` at a legacy
# SQLite file via this env var so it can bring that file up to the current schema
# in isolation. The historical data migrations query via `Model.objects` (no
# `.using(...)`), so they only run against whatever DB is `default` — making the
# SQLite the default is what lets them replay correctly. Unset for every normal
# run (daemon, admin, tests), which always use Postgres.
_LEGACY_SQLITE = os.environ.get("OPENOUTREACH_LEGACY_SQLITE")
if _LEGACY_SQLITE:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": _LEGACY_SQLITE,
        }
    }
else:
    from db_url import django_db_config

    DATABASES = {"default": django_db_config()}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English")]
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

TESTING = sys.argv[1:2] == ["test"]
