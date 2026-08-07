# Architecture

Detailed module documentation for OpenOutreach. See `CLAUDE.md` for rules and quick reference.

## Entry Flow

`manage.py` — stock Django management entrypoint. Bare `python manage.py` (no args) defaults to `rundaemon`.

### `rundaemon` management command (`management/commands/rundaemon.py`)

Startup sequence:
1. **Configure logging** — DEBUG level, suppresses noisy third-party loggers (urllib3, httpx, pydantic_ai, openai, playwright, etc.).
2. **Ensure DB** — `migrate --no-input`.
3. **Onboard** — checks `missing_keys()`; if incomplete, applies `collect_from_wizard()` (non-interactive: loads `config.json` from data dir / cwd / `~/.openoutreach/`, else defaults). If required fields are still missing, exits with a clear error — the Docker supervisor retries after you finish onboarding via the admin panel or `config.json`.
4. **Validate** — `chat_llm_api_key` and `task_llm_api_key` both set, active `LinkedInProfile`, at least one campaign.
5. **Session** — `get_or_create_session(profile)`, sets default campaign (first available).
6. **Newsletter** — Auto-subscription is disabled for self-hosted runs; no outbound signup call is made.
7. **Run** — `run_daemon(session)`.

Remote freemium kit import is disabled; the daemon only uses locally configured campaigns.

Docker `start` script (`compose/linkedin/start`) sets up Xvfb + VNC, runs `migrate` and an optional `createsuperuser --noinput` (from `DJANGO_SUPERUSER_*` env), then supervises two long-lived processes: the admin web server (`uvicorn webadmin.main:app --host 0.0.0.0 --port 8000`, always up — see "Admin panel (webadmin/)" below) and `rundaemon` (restarts on exit). Both connect to the same Postgres `db` service, which handles their concurrent writes natively (no busy-timeout workaround, unlike the old SQLite setup).

### Other management commands

- `onboard` — standalone onboarding (interactive or `--non-interactive` with `--config-file` / individual flags).
- `add_seeds` — add seed LinkedIn profile URLs to a campaign.
- `migrate_sqlite_to_postgres` — one-time upgrade helper for installs that still have data in `data/db.sqlite3`. Works even when the legacy DB is behind on migrations. It (1) copies the SQLite file to a throwaway working copy, (2) in a subprocess with that copy as the **default** DB (via the `OPENOUTREACH_LEGACY_SQLITE` env var, see `django_settings.py`) runs `migrate` to bring it up to the current schema — a subprocess so the historical data migrations, which query via `Model.objects` without `.using(...)`, replay against the SQLite instead of Postgres — then `dumpdata`s it with `--natural-foreign --natural-primary`, (3) runs `migrate` against Postgres, (4) `loaddata`s the export in. Natural keys make FKs into contenttypes/permissions resolve against Postgres's own rows (not collide on raw ids) and match the auto-created superuser by username. Finally it prints a per-table row-count comparison (legacy vs Postgres) so you can confirm nothing was dropped, and keeps the exported JSON in `data/` as a backup.

## Onboarding (`onboarding.py`)

`OnboardConfig` — pure dataclass with all onboarding fields. Two constructors:
- `OnboardConfig.from_json(path)` — from JSON file (cloud / non-interactive).
- `collect_from_wizard()` — interactive questionary wizard (needs TTY), only asks for `missing_keys()`.

Single write path: `apply(config)` — idempotent, creates missing Campaign, LinkedInProfile, env vars, and legal acceptance. Four components:

1. **Campaign** — name, product docs, objective, booking link, seed URLs. Creates `Campaign` with M2M user membership.
2. **LinkedInProfile** — email, password, newsletter, rate limits. Django username from email slug.
3. **LLM config** — `CHAT_LLM_PROVIDER`/`CHAT_LLM_API_KEY`/`CHAT_AI_MODEL`/`CHAT_LLM_API_BASE` and `TASK_LLM_PROVIDER`/`TASK_LLM_API_KEY`/`TASK_AI_MODEL`/`TASK_LLM_API_BASE` → write to the two independent role groups on the `SiteConfig` singleton in DB.
4. **Legal notice** — per-account acceptance stored as `LinkedInProfile.legal_accepted`.

## Profile State Machine

`enums.py:ProfileState` (TextChoices) values ARE CRM stage names: QUALIFIED, READY_TO_CONNECT, PENDING, CONNECTED, COMPLETED, FAILED. Pre-Deal states: url_only (Lead row exists but `embedding` is null), enriched (has `embedding`). `Lead.disqualified=True` = permanent account-level exclusion. LLM rejections = FAILED Deals with wrong_fit outcome (campaign-scoped).

`crm/models/deal.py:Outcome` (TextChoices): converted, not_interested, wrong_fit, no_budget, has_solution, bad_timing, unresponsive, unknown. Used by `Deal.outcome`.

## Task Queue

Persistent queue backed by `Task` model. Worker loop in `daemon.py`: `seconds_until_active()` guard pauses outside active hours/rest days → pop oldest due task → set campaign on session → RUNNING → dispatch via `_HANDLERS` dict → COMPLETED/FAILED. Failures captured by `failure_diagnostics()` context manager.

Task creation is centralized in `linkedin/tasks/scheduler.py`. No other module inserts Task rows. The module exposes three layers: (1) low-level `enqueue_connect`/`enqueue_check_pending`/`enqueue_follow_up` with per-call dedup against existing PENDING rows, (2) a state-transition hook `on_deal_state_entered(deal)` fired by `set_profile_state()` that picks the right task for the new state, and (3) `reconcile(session)` which walks CRM state and recreates missing tasks.

The daemon calls `reconcile()` whenever the queue has no ready task — startup and every idle cycle. This is the retry mechanism: a handler that crashes mid-execution leaves a FAILED task with no successor, and the next idle cycle re-creates it from the Deal's state. `AuthenticationError` (401) triggers `session.reauthenticate()` then marks the task FAILED; reconcile picks it up.

When idle, the daemon doesn't sleep for the full delay until the next `scheduled_at` in one block — it polls in `QUEUE_POLL_INTERVAL` (60s) slices, rechecking `Task.objects.seconds_to_next()` between each. This bounds how long a task rescheduled to run sooner (e.g. via the Task admin's "Run now" action — see "Admin panel (webadmin/)" below) takes to actually start — at most ~60s, regardless of how far out it was originally scheduled.

Three task types (handlers in `linkedin/tasks/`, signature: `handle_*(task, session, qualifiers)`):

1. **`handle_connect`** — Uses `ConnectStrategy` dataclass with `find_candidate()` from `pools.py`. Unreachable detection after `MAX_CONNECT_ATTEMPTS` (3).
2. **`handle_check_pending`** — Per-profile. Exponential backoff with jitter. On acceptance → enqueues `follow_up`.
3. **`handle_follow_up`** — Per-profile. Syncs the conversation (`db/chat.py:sync_conversation`) *before* evaluating `_unanswered_count`/`_too_soon_to_nudge` — both read local `ChatMessage` rows, so gating on them pre-sync would judge a reply that already arrived on LinkedIn by stale local state (and just re-enqueue without ever seeing it; this also made the Task admin's "Run now" action a no-op against a fresh reply). `_too_soon_to_nudge` requires `unanswered_count * MIN_DAYS_PER_UNANSWERED` (3) days of silence since the last *outgoing* message — it returns `False` immediately once the most recent message is an incoming reply, so a synced reply always clears the cooldown. After the gates pass, calls `run_follow_up_agent()` (assumes the caller already synced) which returns a `FollowUpDecision` (structured output: `send_message`/`mark_completed`/`wait`). Handler executes the decision deterministically.

## Qualification ML Pipeline

GPR (sklearn, ConstantKernel * RBF) inside Pipeline(StandardScaler, GPR) with BALD active learning:

1. **Balance-driven selection** — n_negatives > n_positives → exploit (highest P); otherwise → explore (highest BALD).
2. **LLM decision** — All decisions via LLM (`qualify_lead.j2`). GP only for candidate selection and confidence gate.
3. **READY_TO_CONNECT gate** — P(f > 0.5) above `min_ready_to_connect_prob` (0.9) promotes QUALIFIED → READY_TO_CONNECT.

384-dim FastEmbed embeddings stored directly on Lead model, per-campaign GP models at ``Campaign.model_blob` (BinaryField, joblib-dumped with `compress=3`)`. Cold start returns None until >=2 labels of both classes.

## Django Apps

Three apps in `INSTALLED_APPS`:

- **`linkedin`** — Main app: Campaign (with users M2M), LinkedInProfile, SearchKeyword, ActionLog, Task models. All automation logic.
- **`crm`** — Lead (with embedding) and Deal models (in `crm/models/lead.py` and `crm/models/deal.py`). Also defines `Outcome` enum.
- **`chat`** — `ChatMessage` model (GenericForeignKey to any object, content, owner, answer_to threading, topic).

## CRM Data Model

- **SiteConfig** (`linkedin/models.py`) — Singleton (pk=1). Two independent, fully separate LLM configurations, each with its own provider/key/model/base: `chat_llm_provider`/`chat_llm_api_key`/`chat_ai_model`/`chat_llm_api_base` (TextChoices: openai/anthropic/google/groq/mistral/cohere/openai_compatible) and the identically-shaped `task_*` group. Accessed via `SiteConfig.load()`; `linkedin/llm.py:get_llm_model(role)` (`role="chat"` or `role="task"`) is the single factory that turns the selected role's fields into a `pydantic_ai.models.Model`.
- **Campaign** (`linkedin/models.py`) — `name` (unique), `users` (M2M to User), `product_docs`, `campaign_objective`, `booking_link`, `is_freemium`, `action_fraction`, `seed_public_ids` (JSONField). `is_freemium` and `action_fraction` are legacy flags; the daemon treats all campaigns uniformly.
- **LinkedInProfile** (`linkedin/models.py`) — 1:1 with User. `self_lead` FK to Lead (nullable, set on first self-profile discovery). Credentials, rate limits (`connect_daily_limit`, `connect_weekly_limit`, `follow_up_daily_limit`). Methods: `can_execute`/`record_action`/`mark_exhausted`. In-memory `_exhausted` dict for daily rate limit caching.
- **SearchKeyword** (`linkedin/models.py`) — FK to Campaign. `keyword`, `used`, `used_at`. Unique on `(campaign, keyword)`.
- **ActionLog** (`linkedin/models.py`) — FK to LinkedInProfile + Campaign. `action_type` (connect/follow_up), `created_at`. Composite index on `(linkedin_profile, action_type, created_at)`.
- **Lead** (`crm/models/lead.py`) — Per LinkedIn URL (`linkedin_url` = unique). `public_identifier` (derived from URL, unique). `urn` = unique CharField (LinkedIn entity URN, cached on first scrape). `embedding` = 384-dim float32 BinaryField (nullable). `disqualified` = permanent exclusion. The parsed profile dict, person name, and company name are **not stored** — they live only in memory for the lifetime of a scrape dict. Callers that need them re-scrape via `lead.get_profile(session)`. `embedding_array` property for numpy access. `embed_from_profile(profile)` computes + persists the embedding from an in-hand dict (skips the scrape). `get_labeled_arrays(campaign)` classmethod returns (X, y) for GP warm start. Labels: non-FAILED state → 1, FAILED+wrong_fit → 0, other FAILED → skipped.
- **Deal** (`crm/models/deal.py`) — Per campaign (campaign-scoped via FK). `state` = CharField (ProfileState choices). `outcome` = CharField (Outcome choices: converted/not_interested/wrong_fit/no_budget/has_solution/bad_timing/unresponsive/unknown). `reason` = qualification reason (free text). `connect_attempts` = retry count. `backoff_hours` = check_pending backoff. `profile_summary` / `chat_summary` = JSONField fact lists (lazy, mem0-style, campaign-scoped). `creation_date`, `update_date`.
- **Task** (`linkedin/models.py`) — `task_type` (connect/check_pending/follow_up), `status` (pending/running/completed/failed), `scheduled_at`, `payload` (JSONField), `error`, `started_at`, `completed_at`. Composite index on `(status, scheduled_at)`.
- **ChatMessage** (`chat/models.py`) — GenericForeignKey to any object. `content`, `owner`, `answer_to` (self FK), `topic` (self FK), `recipients`, `to` (M2M to User).

## Key Modules

- **`daemon.py`** — Worker loop with active-hours guard (`ENABLE_ACTIVE_HOURS` flag, `seconds_until_active()`), `_build_qualifiers()`, `_CloudPromoRotator`. Calls `scheduler.reconcile()` when the queue has no ready task.
- **`diagnostics.py`** — `failure_diagnostics()` context manager, `capture_failure()` saves page HTML/screenshot/traceback to `/tmp/openoutreach-diagnostics/`.
- **`tasks/scheduler.py`** — Single owner of Task row creation. Low-level `enqueue_*`, state-transition hook `on_deal_state_entered`, and `reconcile()`.
- **`tasks/connect.py`** — `handle_connect`, `ConnectStrategy`.
- **`tasks/check_pending.py`** — `handle_check_pending`, exponential backoff.
- **`tasks/follow_up.py`** — `handle_follow_up`, rate limiting.
- **`pipeline/qualify.py`** — `run_qualification()`, `fetch_qualification_candidates()`.
- **`pipeline/search.py`** — `run_search()`, keyword management.
- **`pipeline/search_keywords.py`** — `generate_search_keywords()` via LLM.
- **`pipeline/ready_pool.py`** — GP confidence gate, `promote_to_ready()`.
- **`pipeline/pools.py`** — Composable generators: `search_source` → `qualify_source` → `ready_source`.
- **`ml/qualifier.py`** — `Qualifier` protocol, `BayesianQualifier`, `qualify_with_llm()`.
- **`ml/embeddings.py`** — FastEmbed utilities, `embed_text()`, `embed_texts()`.
- **`ml/profile_text.py`** — `build_profile_text()`.
- **`browser/session.py`** — `AccountSession`: linkedin_profile, page, context, browser, playwright. `campaigns` cached_property (list, via Campaign.users M2M). `ensure_browser()` launches/recovers browser. `self_profile` cached_property (re-discovers via Voyager on first access per session — no DB cache; one extra scrape per daemon restart). Cookie expiry check via `_maybe_refresh_cookies()`. `reauthenticate()` forces fresh login (close browser, clear saved cookies, re-launch).
- **`browser/registry.py`** — `get_or_create_session()`, `get_first_active_profile()`, `resolve_profile()`, `cli_parser()`/`cli_session()` (shared CLI bootstrap for `__main__` scripts).
- **`browser/login.py`** — `start_browser_session()` — browser launch + LinkedIn login.
- **`browser/nav.py`** — Navigation, auto-discovery, `goto_page()`.
- **`db/leads.py`** — Lead CRUD, `get_leads_for_qualification()`, `disqualify_lead()`, `_cache_urn_from_profile()`.
- **`db/deals.py`** — Deal/state ops, `set_profile_state()`, `increment_connect_attempts()`.
- **`db/chat.py`** — `sync_conversation()`, `_sync_from_api()`, folds newly-synced messages into `Deal.chat_summary` via `update_chat_summary`.
- **`db/summaries.py`** — Single mem0-style LLM boundary. `materialize_profile_summary_if_missing(deal, session)` fires on first follow-up touch (one Voyager re-scrape per `(lead, campaign)` lifetime); `update_chat_summary(deal, new_messages, *, seller_name)` folds newly-synced ChatMessages incrementally via `reconcile_facts`, which routes new facts through mem0's UPDATE prompt to apply ADD/UPDATE/DELETE/NONE events (mirrors `mem0/memory/main.py::Memory._add_to_vector_store` lines 594-700, with vector-store ops replaced by an in-memory dict because `Deal.chat_summary` is a flat list). `_format_messages_for_extraction` filters to incoming messages only, so `chat_summary` holds facts about the lead and a one-sided outgoing burst is a noop. `extract_facts(text, *, seller_name, context)` runs `pydantic_ai.Agent(get_llm_model("task"), output_type=FactList)` against the vendored `_FACT_EXTRACTION_PROMPT` plus an unconditional identity-binding block (`_build_identity_binding`) telling the LLM that `[Me]` is `seller_name`, so seller-name greetings in `[Lead]` messages don't get misattributed to the lead. `reconcile_facts(existing, new, *, seller_name)` prepends the same binding to mem0's UPDATE prompt with an explicit "DELETE contamination" instruction — previously-stored facts that describe the seller as the lead *should* clean up on the next sync that produces a conflicting fact, though this is best-effort (the upstream mem0 prompt is example-heavy and the cleanup hint is one prepended sentence; dormant deals stay contaminated). `seller_name_from(session)` is the single derivation point — `first_name` from `session.self_profile` with username fallback. mem0's `DEFAULT_UPDATE_MEMORY_PROMPT` and `get_update_memory_messages` live under `linkedin/vendor/mem0/configs/prompts.py` (mirrors upstream path so future syncs are a clean diff; pinned commit recorded in the file header).
- **`url_utils.py`** — `url_to_public_id()`, `public_id_to_url()` — LinkedIn URL ↔ public identifier conversion. Pure utility, no DB dependency.
- **`conf.py`** — Config constants, `CAMPAIGN_CONFIG`. LLM construction lives in `llm.py`.
- **`llm.py`** — `get_llm_model(role)` factory + `run_agent_sync(coro)` sync boundary. `role` is `"chat"` (higher-end model, used only by `agents/follow_up.py` — the sole composer of text sent to leads) or `"task"` (cheaper/faster model, used by qualification, search-keyword generation, and fact extraction/reconcile). `get_llm_model(role)` reads the `{role}_*` fields off `SiteConfig` and dispatches via per-provider builders (OpenAI / Anthropic / Google / Groq / Mistral / Cohere / openai_compatible) to the right `pydantic_ai.models.Model` — each role can use a different provider entirely. Call sites build `Agent(get_llm_model(role), ...)` and invoke `run_agent_sync(agent.run(prompt))` — never `Agent.run_sync`, whose anyio portal leaves the caller's thread running-loop slot populated and poisons subsequent sync Playwright calls (`"using Playwright Sync API inside the asyncio loop"`). `run_agent_sync` drives the coroutine to completion on a short-lived worker thread with its own event loop; per-thread asyncio slots are independent, so the caller's thread stays clean regardless of what anyio / pytest-anyio / Jupyter / etc. did to it.
- **`exceptions.py`** — `AuthenticationError`, `TerminalStateError`, `SkipProfile`, `ReachedConnectionLimit`.
- **`onboarding.py`** — Interactive setup.
- **`agents/follow_up.py`** — Follow-up agent. Single LLM call with structured output (`FollowUpDecision`). Conversation is read in Python and injected into the prompt. No tool-calling loop.
- **`actions/`** — `connect.py` (`send_connection_request`), `status.py` (`get_connection_status`), `message.py` (`send_raw_message`), `profile.py` (profile extraction), `search.py` (LinkedIn search), `conversations.py` (`get_conversation`).
- **`api/client.py`** — `PlaywrightLinkedinAPI`: browser-context fetch (runs JS `fetch()` inside Playwright page for authentic headers). `timeout_ms` constructor param (default 30s). `get_profile()` with tenacity retry.
- **`api/voyager.py`** — `LinkedInProfile` dataclass (url, urn, full_name, headline, positions, educations, country_code, supported_locales, connection_distance/degree). `parse_linkedin_voyager_response()`.
- **`api/newsletter.py`** — `subscribe_to_newsletter()` via Brevo form, `ensure_newsletter_subscription()`. No config parsing — subscribe_newsletter is a BooleanField.
- **`api/messaging/send.py`** — Send messages via Voyager messaging API.
- **`api/messaging/conversations.py`** — Fetch conversations/messages.
- **`api/messaging/utils.py`** — Shared helpers: `encode_urn()`, `check_response()`.
- **`setup/gdpr.py`** — `apply_gdpr_newsletter_override()`.
- **`setup/self_profile.py`** — `discover_self_profile()` — fetches self profile via Voyager API, sets `linkedin_profile.self_lead`.
- **`setup/seeds.py`** — User-provided seed profiles: parse URLs, create Leads + QUALIFIED Deals.
- **`django_settings.py`** — Django settings for the ORM + migrations only — Django no longer serves HTTP. `INSTALLED_APPS` is down to `django.contrib.auth` (User, used by `Campaign.users`/`LinkedInProfile.user`/`ChatMessage.owner`), `django.contrib.contenttypes` (backs `ChatMessage`'s `GenericForeignKey`), and `crm`/`chat`/`linkedin`. Postgres config via `POSTGRES_DB`/`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_HOST`/`POSTGRES_PORT` env vars (defaults match the official `postgres` image's own vars; `HOST` defaults to `localhost` for non-Docker local dev). No `MIDDLEWARE`/`ROOT_URLCONF`/`TEMPLATES`/`STATIC_*`/`MEDIA_*` — those only existed to serve the old Django Admin.

## Admin panel (`webadmin/`)

FastAPI app, a separate process from Django (`uvicorn webadmin.main:app`), serving the same `/admin/` path Django Admin used to. Replaces `admin.py` + `crm/admin.py` + `linkedin/dashboard.py` + `templates/admin/index.html` + `django-unfold` in one pass — Django keeps owning models/migrations; this only replaces who serves the CRUD/dashboard HTTP layer.

- **Data layer** (`webadmin/models.py`) — async SQLAlchemy (`asyncpg`) models mirroring the exact tables Django's migrations already created (`crm_lead`, `crm_deal`, `linkedin_campaign`, `linkedin_campaign_users`, `linkedin_linkedinprofile`, `linkedin_searchkeyword`, `linkedin_actionlog`, `linkedin_task`, `linkedin_siteconfig`, `chat_chatmessage`, `auth_user`). This is a deliberate tradeoff (user's explicit choice over reusing the Django ORM directly): Django remains the only thing that alters schema, but this column set must be hand-kept in sync with `crm/models/`, `linkedin/models.py`, `chat/models.py` whenever a future migration changes one of these tables. `webadmin/config.py` builds its own Postgres DSN from the same `POSTGRES_*` env vars Django reads — no Django import needed for DB connectivity. `ChatMessage.content_type_id`/`object_id` (Django `GenericForeignKey`) isn't resolved generically — confirmed via grep that `content_object` only ever targets `Lead` in this codebase, so `object_id` is treated as a `crm_lead` id directly.
- **Admin engine** (`webadmin/registry.py`, `webadmin/views.py`) — `ModelAdmin`/`AdminAction` dataclasses (list_display/list_filter/search_fields/date_hierarchy/readonly_fields/raw_id_fields/m2m_fields/choices/actions/can_add/can_delete/hidden_from_nav), one instance per model in `webadmin/admins.py` — the direct analog of the old `ModelAdmin` subclasses in `admin.py`/`crm/admin.py`. `views.py:build_router(admin)` generates the full list/add/edit/delete/action route set generically from a `ModelAdmin` + SQLAlchemy column introspection (`build_field_specs`) — form fields are typed from the column (text/textarea/number/float/checkbox/datetime/json/binary/select/fk/m2m), FK fields render as either a raw-id number input or a `<select>` per `raw_id_fields`, choice fields render as a `<select>` from a plain Python `Enum` hand-mirrored from the Django `TextChoices` in `admins.py`. Every model gets a built-in "Delete selected" bulk action plus whatever custom `AdminAction`s it declares.
- **Ported behavior** (`webadmin/admins.py`) — `SiteConfigAdmin` (`hidden_from_nav=True`, `can_add=False`, `can_delete=False`) is the direct analog of the old `has_module_permission=False` trick — reachable only via the direct `/admin/siteconfig/1/edit` URL, never in the nav. `TaskAdmin.run_now` (jump PENDING tasks to `scheduled_at=now()`, warns on skipped non-pending selections), `LeadAdmin.mark_human_takeover`/`resume_ai` are ported verbatim from the old `run_now`/`mark_human_takeover`/`resume_ai` actions. Deviation: `Task`, `ActionLog`, and `ChatMessage` drop the Add button entirely (`can_add=False`) — in the old admin every field on these was already `readonly_fields`, so the Add form was already unusable; this just removes the dead affordance.
- **Auth/session/CSRF** (`webadmin/auth.py`, `webadmin/csrf.py`) — login checks the existing `auth_user` table (same rows `DJANGO_SUPERUSER_*`/`createsuperuser` populate) via `django.contrib.auth.hashers.check_password` — a pure function, so `webadmin` does a settings-only `django.setup()` at startup for this one purpose, never for ORM queries. Session is a signed cookie (Starlette `SessionMiddleware`, keyed off `DJANGO_SECRET_KEY` — no new secret to manage). CSRF is a hand-rolled double-submit cookie (`CSRFCookieMiddleware` sets `webadmin_csrf`; every POST must echo it in a `csrf_token` field) since Django's `CsrfViewMiddleware` no longer runs anywhere.
- **`webadmin/dashboard.py`** — home route, ports the 4 stat counts (messages sent, replies received, connect requests sent/accepted) that used to live in `linkedin/dashboard.py` + `templates/admin/index.html`.


## Configuration

- **`SiteConfig`** (DB singleton) — two independent, identically-shaped field groups, `chat_*` (higher-end model for the follow-up messaging agent) and `task_*` (cheaper/faster model for qualification, search-keyword generation, fact extraction): `{role}_llm_provider` (required, defaults to `openai`; choices: `openai`/`anthropic`/`google`/`groq`/`mistral`/`cohere`/`openai_compatible`), `{role}_llm_api_key` (required), `{role}_ai_model` (required), `{role}_llm_api_base` (required only when that role's provider is `openai_compatible`). Editable via the FastAPI admin, but hidden from its nav (`webadmin/admins.py:ModelAdmin(hidden_from_nav=True)`) — reach it via the direct `/admin/siteconfig/1/edit` URL.
- **`conf.py` schedule** — `ENABLE_ACTIVE_HOURS` (`True`), `ACTIVE_START_HOUR` (9), `ACTIVE_END_HOUR` (19), `ACTIVE_TIMEZONE` (system-local IANA name, falls back to "UTC"), `REST_DAYS` ((5, 6) = Sat+Sun). Daemon sleeps outside this window.
- **`conf.py:CAMPAIGN_CONFIG`** — `min_ready_to_connect_prob` (0.9), `min_positive_pool_prob` (0.20), `connect_delay_seconds` (10), `connect_no_candidate_delay_seconds` (300), `check_pending_recheck_after_hours` (24), `check_pending_jitter_factor` (0.2), `qualification_n_mc_samples` (100), `enrich_min_delay_seconds` (6), `enrich_max_delay_seconds` (10), `enrich_max_per_page` (10), `burst_min_seconds` (2700), `burst_max_seconds` (3900), `break_min_seconds` (600), `break_max_seconds` (1200), `min_action_interval` (120), `embedding_model` ("BAAI/bge-small-en-v1.5").
- **Prompt templates** (at `linkedin/templates/prompts/`) — `qualify_lead.j2` (temp 0.7), `search_keywords.j2` (temp 0.9), `follow_up_agent.j2`.
- **`requirements/`** — `base.txt`, `local.txt`, `production.txt`, `crm.txt` (currently empty).

## Docker

`BUILD_ENV` arg selects requirements. Dockerfile at `compose/linkedin/Dockerfile` (two-stage: deps build → runtime; installs Playwright chromium + a VNC stack: Xvfb, x11vnc, websockify, noVNC). One container exposes three ports: **8000** (FastAPI admin panel, always up), **6080** (noVNC in-browser browser view), **5900** (native VNC client). `local.yml` publishes all three and auto-creates a superuser via `DJANGO_SUPERUSER_*` env. See `compose/linkedin/start` for the process supervision.

Both `docker-compose.yml` and `local.yml` also define a `db` service (`postgres:16-alpine`, data in the `openoutreach_pgdata` named volume) with a `pg_isready` healthcheck; `app` declares `depends_on: db: condition: service_healthy`, so `start`'s `migrate --no-input` only runs once Postgres is actually accepting connections — no wait-loop needed in the start script itself.

## CI/CD

- `tests.yml` — pytest in Docker on push to `master` and PRs.
- `deploy.yml` — Tests → build + push to `ghcr.io/eracle/openoutreach`. Tags: `latest`, `sha-<commit>`, semver.

## Dependencies

`requirements/` files. `uv pip install` for fast installs.

Core: `playwright`, `playwright-stealth`, `Django`, `psycopg[binary]` (Postgres driver, Django ORM), `fastapi`, `uvicorn`, `sqlalchemy[asyncio]`, `asyncpg` (Postgres driver, admin panel), `python-multipart` (form parsing), `itsdangerous` (session/CSRF signing), `pandas`, `pydantic-ai-slim` (with `openai`/`anthropic`/`google`/`groq`/`mistral`/`cohere`/`bedrock` extras), `jinja2`, `pydantic`, `jsonpath-ng`, `tendo`, `termcolor`, `tenacity`
ML: `scikit-learn`, `numpy`, `fastembed`, `joblib`
