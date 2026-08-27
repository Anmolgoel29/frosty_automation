# linkedin/conf.py
from __future__ import annotations

from pathlib import Path

from linkedin.tz_detect import system_timezone


# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent.parent

PROMPTS_DIR = Path(__file__).parent / "templates" / "prompts"

DIAGNOSTICS_DIR = Path("/tmp/openoutreach-diagnostics")

FIXTURE_DIR = ROOT_DIR / "tests" / "fixtures"
FIXTURE_PROFILES_DIR = FIXTURE_DIR / "profiles"
FIXTURE_PAGES_DIR = FIXTURE_DIR / "pages"
DUMP_PAGES = False

MIN_DELAY = 5
MAX_DELAY = 8

# ----------------------------------------------------------------------
# Browser config
# ----------------------------------------------------------------------
BROWSER_SLOW_MO = 200
BROWSER_DEFAULT_TIMEOUT_MS = 30_000
BROWSER_LOGIN_TIMEOUT_MS = 40_000
BROWSER_NAV_TIMEOUT_MS = 10_000
HUMAN_TYPE_MIN_DELAY_MS = 50
HUMAN_TYPE_MAX_DELAY_MS = 200

# ----------------------------------------------------------------------
# Onboarding defaults (shown to user during interactive setup)
# ----------------------------------------------------------------------
DEFAULT_CONNECT_DAILY_LIMIT = 20
DEFAULT_CONNECT_WEEKLY_LIMIT = 100
DEFAULT_FOLLOW_UP_DAILY_LIMIT = 25
DEFAULT_CAMPAIGN_NAME = "LinkedIn Outreach"

# ----------------------------------------------------------------------
# Active-hours schedule (daemon pauses outside this window)
# Set to False to run 24/7.
# ----------------------------------------------------------------------
ENABLE_ACTIVE_HOURS = False
ACTIVE_START_HOUR = 9   # inclusive, local time
ACTIVE_END_HOUR = 19    # exclusive, local time
ACTIVE_TIMEZONE = system_timezone()
REST_DAYS = (5, 6)      # 0=Mon … 6=Sun; default Sat+Sun off

# ----------------------------------------------------------------------
# Campaign config (timing + ML defaults — hardcoded, no YAML)
# ----------------------------------------------------------------------
CAMPAIGN_CONFIG = {
    "check_pending_recheck_after_hours": 24,
    # Ceiling for the check_pending backoff doubling (24 → 48 → 96 → …).
    # Uncapped, a run of inconclusive checks compounds past any useful
    # horizon — a systematic scrape break pushed real deals to 384h before
    # this existed, so they stayed asleep for over two weeks after the break
    # itself was fixed. LinkedIn invitations expire around the 2-3 week mark
    # anyway, so there is nothing to learn from polling slower than this.
    "check_pending_max_backoff_hours": 336,  # 14 days
    "min_action_interval": 120,
    # QUALIFIED -> READY_TO_CONNECT promotion gate and connect-order ranking
    # (pipeline/ready_pool.py) — the expensive qualification stage's own
    # 1-5 self-rating, re-evaluated against the QUALIFIED backlog on every
    # call, so raising or lowering this mid-campaign takes effect immediately.
    "min_fit_score": 4,
    # Qualification dossier (ml/dossier.py). One expensive-stage
    # qualification is now several LinkedIn reads — profile, follower count,
    # the lead's posts, then a page + posts per company they currently work
    # at — so they get a jittered gap rather than firing back-to-back.
    "dossier_posts_per_source": 3,
    "dossier_min_delay_seconds": 2,
    "dossier_max_delay_seconds": 4,
    "connect_delay_seconds": 10,
    "connect_no_candidate_delay_seconds": 300,
    "enrich_min_delay_seconds": 6,
    "enrich_max_delay_seconds": 10,
    "enrich_max_per_page": 10,
    "burst_min_seconds": 1800,   # 30 min
    "burst_max_seconds": 2100,   # 35 min
    "break_min_seconds": 600,    # 10 min
    "break_max_seconds": 1200,   # 20 min
    # Multi-account pacing: minimum jittered gap between two accounts *starting*
    # a task. Tasks still overlap (that is the point of running in parallel),
    # but the accounts don't fire their first request in the same instant,
    # which is the part that correlates them from a shared IP.
    "account_stagger_min_seconds": 20,
    "account_stagger_max_seconds": 60,
    # How often the supervisor re-reads the account roster and reconciles.
    "supervisor_interval_seconds": 60,
}


