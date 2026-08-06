# How OpenOutreach Actually Works

A from-the-source-code walkthrough of the daemon, its data model, and every subsystem it drives. This
complements `CLAUDE.md` (rules + quick reference) and `ARCHITECTURE.md` (terse module index) with a narrative
explanation of the *exact* runtime behavior, including the rough edges. Every claim below was verified against
the code at the time of writing (file:line citations throughout) rather than copied from other docs.

## Table of contents

1. [What this system is](#1-what-this-system-is)
2. [Startup sequence](#2-startup-sequence)
3. [Data model](#3-data-model)
4. [The task queue engine](#4-the-task-queue-engine)
5. [Task handlers in detail](#5-task-handlers-in-detail)
6. [Lead discovery & enrichment](#6-lead-discovery--enrichment)
7. [ML qualification: GP + BALD + LLM](#7-ml-qualification-gp--bald--llm)
8. [Browser automation & the Voyager API layer](#8-browser-automation--the-voyager-api-layer)
9. [The conversational AI (follow-up agent + memory)](#9-the-conversational-ai-follow-up-agent--memory)
10. [LLM plumbing](#10-llm-plumbing)
11. [Configuration reference](#11-configuration-reference)
12. [Django Admin & dashboard](#12-django-admin--dashboard)
13. [Onboarding & management commands](#13-onboarding--management-commands)
14. [Docker & deployment](#14-docker--deployment)
15. [End-to-end walkthrough of a single lead](#15-end-to-end-walkthrough-of-a-single-lead)
16. [Known quirks, dead code, and rough edges](#16-known-quirks-dead-code-and-rough-edges)

---

## 1. What this system is

OpenOutreach is a self-hosted Django app whose real product is a **background daemon**
(`linkedin/daemon.py:run_daemon`). It logs into a real LinkedIn account through a stealth-patched Playwright
browser, discovers people via LinkedIn People Search, scrapes their profiles through LinkedIn's internal
"Voyager" API, decides who's worth contacting with a Gaussian-Process-driven active-learning loop backed by an
LLM, sends connection requests, and — once accepted — runs an LLM sales-conversation agent over LinkedIn
messaging. Everything is observable/operable through Django Admin (`unfold`-themed), which is the *only* web
surface the project exposes (`linkedin/urls.py` has exactly one route beyond static files).

There is no separate API server, no Celery, no Redis. Concurrency model: **one daemon process, one browser
session, one task at a time**, backed by a `Task` table in SQLite acting as a durable work queue.

## 2. Startup sequence

`manage.py:17-24` — if you run `python manage.py` with no args (or the first arg is a flag), it rewrites
`sys.argv` to inject `rundaemon` as the subcommand. So `python manage.py` and `python manage.py rundaemon` are
identical; this is also what the Docker image's `CMD` ultimately runs.

`rundaemon` (`linkedin/management/commands/rundaemon.py`) does five steps, strictly in order:

1. **Logging** — `configure_logging()` (`linkedin/logging.py`): one `StreamHandler(stdout)` with a colored
   `[LVL] message` formatter, and a fixed silence-list
   (`urllib3, httpx, pydantic_ai, openai, playwright, httpcore, fastembed, huggingface_hub, filelock, asyncio`)
   forced to `WARNING` so their DEBUG chatter doesn't drown the daemon's own logs.
2. **DB** — `migrate --no-input`, then `setup_crm()` (`linkedin/management/setup_crm.py`), which is just
   `Site.objects.get_or_create(id=1, ...)` — Django's `sites` framework needs a row for `SITE_ID=1`.
3. **Onboarding check** — if `missing_keys()` (`linkedin/onboarding.py`) is non-empty, calls
   `apply(collect_from_wizard())`. If keys are *still* missing afterward, the command exits with `sys.exit(1)`
   and a clear stderr message — under Docker, the `start` script's restart-on-exit loop keeps retrying this
   until you finish onboarding via the admin panel or `config.json` (see [§13](#13-onboarding--management-commands)
   for why `collect_from_wizard()` is not actually a wizard).
4. **Session bootstrap** — validates `SiteConfig.load().llm_api_key` is set, fetches the first `active=True`
   `LinkedInProfile`, calls `get_or_create_session(profile)`, and picks the first non-freemium campaign
   (`next((c for c in session.campaigns if not c.is_freemium), None) or session.campaigns[0]`). Exits if any of
   these are missing.
5. **Newsletter** — force-disables `subscribe_newsletter` and marks `newsletter_processed=True` unconditionally
   on first run (see [§16](#16-known-quirks-dead-code-and-rough-edges) — this makes the GDPR-aware newsletter
   logic in `setup/gdpr.py` effectively unreachable).

Then: `run_daemon(session)` — this call never returns under normal operation.

## 3. Data model

Three Django apps, `linkedin` / `crm` / `chat`, sharing one SQLite file at `data/db.sqlite3`
(`OPTIONS={"timeout": 30}` — a 30s busy-timeout because the admin webserver and the daemon both write to it
concurrently in Docker).

### `linkedin` app (`linkedin/models.py`)

| Model | Purpose |
|---|---|
| `SiteConfig` | Singleton (`pk` forced to `1` in `save()`). LLM provider/key/model/base-url. `SiteConfig.load()` is `get_or_create(pk=1)`. |
| `Campaign` | `name` (unique), `users` M2M, `product_docs`, `campaign_objective`, `booking_link`, `seed_public_ids`, `model_blob` (the joblib-pickled GP pipeline). `is_freemium`/`action_fraction` are legacy — the daemon treats every campaign the same. |
| `LinkedInProfile` | 1:1 with `User`. Credentials, `cookie_data` (Playwright `storage_state()` JSON), rate limits, `self_lead` FK. Owns `can_execute()`/`record_action()`/`mark_exhausted()` rate-limiting logic. |
| `SearchKeyword` | FK to `Campaign`. `keyword`, `used`, `used_at`. Unique on `(campaign, keyword)`. |
| `ActionLog` | One row per rate-limited action taken (`connect`/`follow_up`). This *is* the rate-limit ledger — `can_execute` counts these. |
| `Task` | The work queue (see [§4](#4-the-task-queue-engine)). |

### `crm` app

- **`Lead`** (`crm/models/lead.py`) — one row per LinkedIn person, keyed by `linkedin_url`/`public_identifier`
  (both unique). Deliberately **does not store the parsed profile** (name, company, headline, etc.) — only
  `urn` (cached LinkedIn entity id) and `embedding` (384-dim float32, raw bytes) persist. Anything else is
  re-scraped live via `get_profile(session)` whenever needed. This is a conscious design choice, not
  laziness — see the docstring at `crm/models/lead.py:40-47`.
- **`Deal`** (`crm/models/deal.py`) — the *per-campaign* pipeline state for a Lead. `UniqueConstraint(lead,
  campaign)`. Carries `state` (a `ProfileState`), `outcome` (an `Outcome`), `reason`, `connect_attempts`,
  `backoff_hours`, and the two lazy fact-list JSON fields `profile_summary`/`chat_summary`.

### `chat` app

- **`ChatMessage`** (`chat/models.py`) — `GenericForeignKey` to any model (in practice always `Lead`),
  `content`, `is_outgoing`, `linkedin_urn` (unique — the dedup key when syncing from Voyager),
  `owner`/`answer_to`/`topic` self/user FKs. Two M2M fields, `recipients` and `to`, are declared but never
  written anywhere in the sync code — vestigial.

### State machine

`linkedin/enums.py:ProfileState` — `QUALIFIED`, `READY_TO_CONNECT`, `PENDING`, `CONNECTED`, `COMPLETED`,
`FAILED`. Before a `Deal` even exists, a `Lead` implicitly passes through two unmodeled states: **url_only**
(row exists, `embedding` is `NULL`) and **enriched** (`embedding` set). `Lead.disqualified=True` is a permanent,
campaign-independent exclusion (used for the operator's own profile and unreachable profiles). `FAILED` Deals
carry an `Outcome` — a rejection by the qualification LLM is specifically `FAILED` + `outcome=wrong_fit`
(campaign-scoped, so the same person can be re-tried in a different campaign).

```
(scrape)        (embed)
url_only ──► enriched ──► QUALIFIED ──► READY_TO_CONNECT ──► PENDING ──► CONNECTED ──► COMPLETED
                              │                │                 │            │
                              └────────────────┴─────────────────┴────────────┴──► FAILED
```

Every transition into `PENDING` or `CONNECTED` is what drives the task queue — see next section.

## 4. The task queue engine

> For a much deeper dive — full code, exact backoff/jitter formulas, the crash-recovery mechanism traced
> step by step, and the test suite that pins it all down — see [TASK_QUEUE.md](TASK_QUEUE.md).

`Task` (`linkedin/models.py:225-269`) has `task_type` (`connect`/`check_pending`/`follow_up`), `status`
(`pending`/`running`/`completed`/`failed`), `scheduled_at`, and a `payload` JSONField. `TaskQuerySet` adds
`pending()` (ordered by `scheduled_at`), `claim_next()` (`scheduled_at <= now`), `seconds_to_next()`.

**`linkedin/tasks/scheduler.py` is the single writer of `Task` rows** — no other module inserts one. It has
three layers:

1. **Low-level enqueue** — `enqueue_connect`/`enqueue_check_pending`/`enqueue_follow_up`, all routed through
   `_insert_task()` (`scheduler.py:46-73`), which **deduplicates**: it skips the insert if a `PENDING` row
   already exists with matching payload keys. This is what makes calling these functions idempotent from
   multiple call sites.
2. **State-transition hook** — `on_deal_state_entered(deal)` (`scheduler.py:140-160`), called by every
   `set_profile_state()` (`linkedin/db/deals.py`) **unconditionally**, even when the new state equals the old
   one (only the log line differs). It inspects `deal.state` and enqueues the implied next task:
   `PENDING → enqueue_check_pending`, `CONNECTED → enqueue_follow_up`. Other states have no implied task.
3. **Reconcile** — `reconcile(session)` (`scheduler.py:210-223`): resets stale `RUNNING` tasks back to
   `PENDING` (they can only be stale if the daemon crashed mid-task), seeds one `connect` task per campaign,
   and re-derives tasks for every `PENDING`/`CONNECTED` `Deal` (skipping `human_takeover` leads — see
   [§16](#16-known-quirks-dead-code-and-rough-edges)). **This is the entire retry mechanism**: a handler that
   crashes leaves a `FAILED` task with no successor; `reconcile()` notices the Deal is still `PENDING`/
   `CONNECTED` with nothing queued and recreates the task from scratch.

### The daemon loop (`linkedin/daemon.py:run_daemon`)

```python
while True:
    pause = seconds_until_active()          # active-hours/rest-day guard
    if pause > 0: sleep; continue

    task = Task.objects.claim_next()
    if task is None:
        reconcile(session)                  # retry mechanism fires here
        wait = Task.objects.seconds_to_next()
        # poll in 60s slices rather than one long sleep, then continue
        continue

    session.campaign = <campaign from task.payload>
    task.mark_running()
    try:
        with failure_diagnostics(session):
            _HANDLERS[task.task_type](task, session, qualifiers)
    except AuthenticationError:
        session.reauthenticate(); task.mark_failed(); continue
    except ModelHTTPError:
        task.mark_failed(); return          # daemon STOPS entirely
    except Exception:
        task.mark_failed(); continue

    task.mark_completed()
    rhythm.maybe_break()
```

Notable behavior:

- **`reconcile()` runs on every idle cycle**, not just at startup — any time the queue drains, CRM state gets
  re-swept.
- When waiting for a future task, the daemon **polls in 60-second slices** (`QUEUE_POLL_INTERVAL`) instead of
  sleeping the full delay in one block. This is specifically so the Admin's "Run now" bulk action (which just
  sets `scheduled_at=now()` on a task) takes effect within about a minute instead of waiting out whatever delay
  was originally computed.
- **`ModelHTTPError` stops the whole daemon** (`return`, not `continue`) — a single bad LLM API call (wrong key,
  quota exhausted, provider outage) halts all automation until the process is restarted. In Docker, `start`'s
  supervision loop restarts `rundaemon` automatically (after a 10s pause); running it bare with `make run`
  requires a manual restart.
- **`AuthenticationError`** (401 from Voyager) triggers `session.reauthenticate()` (full re-login, clearing
  saved cookies) and marks the task failed — `reconcile()` recreates it next cycle.
- **`_HumanRhythmBreak`** (`daemon.py:83-121`) simulates human work rhythm: after each successfully completed
  task, if the current "burst" (45–65 min, `CAMPAIGN_CONFIG["burst_min/max_seconds"]`) has run its course, it
  sleeps a random 10–20 min break before starting a new burst. Idle sleeps (active-hours pause, empty queue)
  reset the burst timer via `rhythm.reset()` so break-timing only tracks actual work.
- **`Heartbeat`** logs `alive — <context>` at most once per 5 minutes so a long sleep never looks like a hang
  in the logs.

## 5. Task handlers in detail

All three live in `linkedin/tasks/` with signature `handle_*(task, session, qualifiers)`.

### `handle_connect` (`linkedin/tasks/connect.py`)

1. Rate check: `session.linkedin_profile.can_execute("connect")` — if exhausted, reschedules the *same*
   `connect` task for `seconds_until_tomorrow()` and returns (no candidate lookup happens).
2. `strategy.find_candidate(session)` → `linkedin/pipeline/pools.py:find_candidate` (see [§6](#6-lead-discovery--enrichment)).
   `None` → reschedule after `connect_no_candidate_delay_seconds` (300s).
3. `get_connection_status(session, profile)` — if the profile turns out to already be `CONNECTED` or `PENDING`
   (e.g. a stale QUALIFIED Deal for someone you're already talking to), `set_profile_state()` is called and the
   loop just self-reschedules another `connect` task at the normal cadence.
4. `send_connection_request()` (DOM click). Three outcomes:
   - **No Connect button found** → treated as a soft failure: `increment_connect_attempts()`.
     `MAX_CONNECT_ATTEMPTS = 3` — below that, state resets to `QUALIFIED` (will be re-picked later); at/above
     it, the lead is **disqualified** (`Lead.disqualified=True`, permanent, cross-campaign) and the Deal is set
     `FAILED` with reason `"Unreachable: no Connect button after {n} attempts"`.
   - **Success** → `PENDING`, `record_action("connect", campaign)` (feeds the rate limiter).
   - **Exceptions**: `ReachedConnectionLimit` → `mark_exhausted("connect")` (in-memory flag, resets at
     midnight) + reschedule for tomorrow. `ProfileInaccessibleError`/`SkipProfile` → Deal `FAILED` with a
     reason string, then the *normal* `connect_delay_seconds` (10s) cadence resumes (this is per-profile, not
     account-wide, so there's no reason to wait until tomorrow).
5. Every non-exhausted path reschedules a fresh `connect` task at `connect_delay_seconds` (10s) — this is what
   keeps the connect loop running continuously for a campaign.

### `handle_check_pending` (`linkedin/tasks/check_pending.py`)

- `get_connection_status()` (hybrid Voyager+DOM, see [§8](#8-browser-automation--the-voyager-api-layer)).
- If **still `PENDING`**: `_bump_backoff()` **doubles** `deal.backoff_hours` (`new = current * 2`, no jitter,
  no ceiling) and saves it *before* calling `set_profile_state()`, so the scheduler hook reads the
  already-doubled value. The *actual* delay used to schedule the next `check_pending` task is jittered
  separately, in `scheduler.py:enqueue_check_pending` — `delay_hours = half + random.uniform(0, half)` where
  `half = backoff_hours / 2`, i.e. uniform over `[backoff/2, backoff]`. So the combined behavior genuinely is
  "exponential backoff with jitter" — the doubling and the jitter just happen in two different functions.
- If the status is anything else (`CONNECTED`, or degraded to `QUALIFIED`/`FAILED` via `SkipProfile`), it's
  passed straight to `set_profile_state()` with no special-casing here — the scheduler hook decides what
  happens next (e.g. `CONNECTED` → a `follow_up` task appears automatically).
- `SkipProfile` → Deal `FAILED`, no re-enqueue.

### `handle_follow_up` (`linkedin/tasks/follow_up.py`)

1. Rate check (`follow_up` daily limit) → reschedule in a fixed 3600s if exhausted.
2. **`deal.lead.human_takeover` check** — if `True`, logs and returns with **no re-enqueue at all**. This Deal
   produces zero automated activity from this point forward (see [§16](#16-known-quirks-dead-code-and-rough-edges)
   for how it stays that way even through `reconcile()`).
3. **`sync_conversation(session, public_id)` runs before any gating logic.** The code comment is explicit about
   why: the gating functions below read local `ChatMessage` rows, so a reply that just arrived on LinkedIn has
   to be pulled in first, or the gate would judge a stale state and just re-enqueue without ever "seeing" the
   reply (this also means the Admin's "Run now" action against a stale follow_up task correctly reacts to a
   just-arrived reply, instead of being a no-op).
4. `_unanswered_count(deal)` — outgoing messages sent since the last incoming reply (or all outgoing messages if
   there's never been a reply). **`>= 3` → Deal `COMPLETED` with `outcome="unresponsive"`**, terminal, no LLM
   call, no re-enqueue.
5. `_too_soon_to_nudge(deal, unanswered)` — if the last message was a reply, always `False` (never too soon).
   Otherwise requires `unanswered * 3` days of silence since the last outgoing message
   (`MIN_DAYS_PER_UNANSWERED = 3`; in practice only the 1-unanswered→3d and 2-unanswered→6d cases matter, since
   3 unanswered already triggers step 4). If too soon, reschedule in 24h **without calling the LLM** — an
   explicit cost-control measure.
6. Otherwise: `materialize_profile_summary_if_missing(deal, session)` then `run_follow_up_agent(session, deal)`
   → a `FollowUpDecision` (see [§9](#9-the-conversational-ai-follow-up-agent--memory)). The handler executes it
   deterministically:
   - `send_message` → `send_raw_message()`. **On failure, the Deal is demoted back to `QUALIFIED`** (not just
     retried as a message) — it re-enters the connect pipeline from scratch rather than staying `CONNECTED`.
     On success, `record_action("follow_up", campaign)` + reschedule in `decision.follow_up_hours * 3600`.
   - `mark_completed` → Deal `COMPLETED` with `outcome=decision.outcome`. Terminal.
   - `wait` → reschedule in `decision.follow_up_hours * 3600`, no message sent.

## 6. Lead discovery & enrichment

`linkedin/pipeline/pools.py` composes three Python generators, each pulling from the one before it on demand —
nothing runs eagerly:

```
search_source(session)                       # yields keyword-search results, or stops
   └─► qualify_source(session, qualifier)     # yields one newly-qualified public_id per LLM call
          └─► ready_source(session, qualifier, threshold)   # yields one READY_TO_CONNECT public_id
                 └─► find_candidate() = next(ready_source(...), None)   # what handle_connect calls
```

- **`search_source`**: `while True: yield run_search(session)` until it returns `None`.
  `linkedin/pipeline/search.py:run_search` — if the campaign has zero unused `SearchKeyword` rows, generates a
  fresh batch of 10 via `generate_search_keywords()` (LLM, `search_keywords.j2`, **temperature 0.9** — the
  highest of any prompt in the codebase, favoring lexical diversity), bulk-creates them
  (`ignore_conflicts=True`). Picks the oldest unused keyword, marks it `used=True`, and calls
  `search_people(session, keyword)` (DOM search + `extract_in_urls()` auto-discovery of `/in/...` links on the
  results page, throttled via `discover_and_enrich` — max `enrich_max_per_page=10` new profiles per page,
  6–10s delay between each Voyager scrape). There is no separate seed-URL discovery path in the live daemon
  loop; new profiles come exclusively from LinkedIn People Search using LLM-generated keywords (seed URLs from
  onboarding just create initial `Lead`+`QUALIFIED Deal` rows directly, bypassing search).
- **`qualify_source`**: fetches already-enriched-but-unqualified candidates; if none exist, pulls one item from
  `search_source` to bring more in. `_needs_search()` gates whether to keep pulling from search before
  qualifying, using an **adaptive threshold**: `max(0, min_positive_pool_prob - 1/sqrt(n_obs))` where
  `min_positive_pool_prob=0.20`. With few labeled observations (`n_obs <= 25`, since `1/sqrt(25)=0.2`), this is
  `0`, so search essentially never triggers early on — an implicit cold-start guard against burning search
  queries before the model has any signal. It also refuses to search while in "explore" mode (see next
  section) or when GP predictions are degenerate. Each `qualify_source` yield corresponds to **exactly one**
  `run_qualification()` call, i.e. one LLM qualification decision.
- **`ready_source`**: tries the existing `READY_TO_CONNECT` pool first; if empty, tries
  `promote_to_ready()` (the GP confidence gate, [§7](#7-ml-qualification-gp--bald--llm)); if that promotes
  nothing, pulls one more qualification from `qualify_source` (which may itself trigger a search) and retries.
  Stops once everything upstream is exhausted.

## 7. ML qualification: GP + BALD + LLM

Every `Lead` that gets scraped is turned into text (`linkedin/ml/profile_text.py:build_profile_text`) by
concatenating, in order and lowercased: `headline`, `summary`, `location_name`, `industry.name`, then for every
position `title/company_name/location/description`, then for every education `school_name/degree/
field_of_study` — a flat, unlabeled bag-of-words string. That text is embedded
(`linkedin/ml/embeddings.py`, FastEmbed's `BAAI/bge-small-en-v1.5`, 384 dimensions) and the raw float32 bytes
are stored directly on `Lead.embedding` — no vector DB, just a `BinaryField`.

### `BayesianQualifier` (`linkedin/ml/qualifier.py`)

Per campaign: `Pipeline([StandardScaler(), GaussianProcessRegressor(kernel=ConstantKernel(1.0) *
RBF(length_scale=sqrt(384)), alpha=0.1, n_restarts_optimizer=3)])`. Training pairs `(X, y)` accumulate
in-memory as Python lists; refitting is **lazy and from scratch** every time — `update()` just appends and
flags `_fitted=False`; the *next* prediction call triggers `_fit_if_needed()`, which requires ≥2 labeled
observations spanning both classes (cold start otherwise returns `None`/no-op), subsamples the majority class
down to at most 2× the minority count for balance, refits the whole pipeline, and immediately joblib-dumps
(`compress=3`) the fitted pipeline into `Campaign.model_blob` — persistence is a side effect of every refit,
not a separate step.

Two different "probability" constructions exist, for two different purposes:

- **Predictive gate** — `P(f > 0.5)` computed **exactly** via the Gaussian survival function:
  `scipy.stats.norm.sf(0.5, loc=gp_mean, scale=gp_std)`.
- **BALD (Bayesian Active Learning by Disagreement)** — draws 100 Monte Carlo samples of the latent function
  `f_samples = f_mean + f_std * N(0,1)` (shape `100 × N`), converts each to a probit-style probability
  `Φ(f_samples - 0.5)`, then computes
  `BALD = H(mean_over_samples(p)) - mean_over_samples(H(p))` (mutual information between the label and the
  model parameters — high when the model is confident on average but individual posterior draws disagree).

**Exploit/explore switch** (`acquisition_scores`) is a single inequality on raw label counts:
`n_negatives > n_positives → exploit` (rank remaining candidates by `P(f>0.5)`, chase what looks positive);
otherwise (including the 0/0 tie) `→ explore` (rank by BALD, chase what's most informative). No smoothing or
hysteresis — this can flip every labeling round near parity.

**`run_qualification()`** (`linkedin/pipeline/qualify.py`): fetches a batch of enriched-but-unqualified
candidates, scores all of them via `acquisition_scores`, and picks the single `argmax`. **The LLM is always
called for that candidate** — the GP's own prediction is logged for visibility but does not shortcut/replace
the LLM call. The LLM call (`qualify_with_llm`, `qualify_lead.j2`, **temperature 0.7**) judges role/title fit,
industry relevance, seniority/decision-making authority, and company fit against the campaign's
`product_docs`/`campaign_objective`, returning a binary accept/reject + reason. The result always calls
`qualifier.update(embedding, label)` (this is what actually moves the GP), and then: label `1` → promote to a
`QUALIFIED` Deal; label `0` → `FAILED` Deal with `outcome=wrong_fit` and the LLM's reason.

**`promote_to_ready()`** (`linkedin/pipeline/ready_pool.py`): for every `QUALIFIED` lead with an embedding,
computes the exact `P(f > 0.5)` (same closed-form as above, no sampling) and promotes to `READY_TO_CONNECT` any
whose probability exceeds `CAMPAIGN_CONFIG["min_ready_to_connect_prob"] = 0.9`. Because this uses the exact
posterior, it's sensitive to the GP's `std` estimate, which itself depends on the never-tuned fixed
`alpha=0.1` noise term and the fixed-length-scale RBF kernel.

## 8. Browser automation & the Voyager API layer

`AccountSession` (`linkedin/browser/session.py`) holds the live Playwright `browser`/`context`/`page` plus
`linkedin_profile`, `django_user`, and the daemon-assigned `campaign`. `campaigns` is a `cached_property`
(every `Campaign` whose `users` M2M includes this session's `django_user`). `self_profile` is also a
`cached_property` — one Voyager scrape of `public_identifier="me"` per session lifetime, not persisted to the
DB beyond the one-time `LinkedInProfile.self_lead` FK link set during onboarding
(`linkedin/setup/self_profile.py:discover_self_profile` — deliberately creates that self-Lead with
`disqualified=True` so auto-discovery never targets the operator's own profile).

`ensure_browser()` either does a full `start_browser_session()` (no page yet) or a cheap
`_maybe_refresh_cookies()` check (re-reads `cookie_data` from DB, and if the `li_at` cookie's `expires` has
passed, closes and fully relaunches). `reauthenticate()` — used by the daemon on a 401 — nulls
`cookie_data` in the DB *before* relaunching, forcing a fresh interactive login rather than a cookie resume.

`linkedin/browser/login.py`: DOM login flow using **fallback locator chains** (several candidate CSS selectors
tried in order, 5s visibility wait each) for email/password/submit fields and LinkedIn's "Agree to comply"
interstitial. Stealth is applied via `playwright-stealth`'s `Stealth().apply_stealth_sync(context)`. Cookies
are saved as a full `context.storage_state()` blob into `LinkedInProfile.cookie_data`. **There is no 2FA/
checkpoint handling** — if LinkedIn shows a security challenge, the expected redirect to `/feed` simply times
out and raises `RuntimeError("Login failed – no redirect to feed")`.

### The Voyager client (`linkedin/api/client.py`)

`PlaywrightLinkedinAPI` doesn't make HTTP requests from Python — it runs `fetch()` **inside the already-logged-in
page's own JS context** via `page.evaluate()`. This means every ambient browser header (user-agent,
`sec-ch-*`, `x-li-track`, etc.) is authentically the real browser's, not a spoofed value — only 4 headers are
set explicitly: `accept`, `csrf-token` (the `JSESSIONID` cookie value, unquoted), `x-li-lang`,
`x-restli-protocol-version`. Retries (`tenacity`, 3 attempts, exponential backoff 2–30s) only fire on generic
`IOError` — a 401 raises `AuthenticationError` and a 403/404 raises `ProfileInaccessibleError`, **neither of
which is retried** (matches LinkedIn's semantics: no point retrying an auth failure or a genuinely
private/deleted profile).

`linkedin/api/voyager.py:parse_linkedin_voyager_response()` walks LinkedIn's normalized
`{data: {...}, included: [...]}` graph shape: builds an `entityUrn → entity` lookup map over `included`, locates
the profile entity (preferring an exact `public_identifier` match, then any entity whose `$recipeTypes`
contains `"FullProfile"`), and resolves positions/educations/connection-degree through 2–3 levels of `*field`
indirection into that map. Connection degree comes from following
`profile["*memberRelationship"]` → the resolved relationship entity → mapping
`DISTANCE_1/2/3/OUT_OF_NETWORK` to numeric degrees.

Messaging (`linkedin/api/messaging/`): `send_message()` builds a `createMessage` mutation with a random
`originToken`/`trackingId`, posted as `text/plain` despite a JSON body (a LinkedIn API quirk).
`fetch_conversations`/`fetch_messages` hit persisted GraphQL queries via **hardcoded `queryId` hashes** — if
LinkedIn ever rotates these server-side, there's no specific error path that detects "stale queryId," it'll
just start failing. Notably, the messaging layer's `check_response()` maps 403/404 to a **retryable**
generic `IOError`, unlike the profile-scraping client's non-retried `ProfileInaccessibleError` — an
inconsistency between the two API surfaces.

### DOM vs. Voyager, by action

| Function | Mechanism |
|---|---|
| `send_connection_request` (`actions/connect.py`) | DOM click (Connect button / "More" dropdown → "Send") |
| `get_connection_status` (`actions/status.py`) | Hybrid — tries Voyager degree first, falls back to DOM inspection |
| `send_raw_message` (`actions/message.py`) | DOM first (messaging thread compose box), falls back to Voyager API on failure |
| `scrape_profile` (`actions/profile.py`) | Voyager (`get_profile`) |
| `search_people` (`actions/search.py`) | DOM (search results page + link extraction) |
| `get_conversation`/`find_conversation_urn` (`actions/conversations.py`) | Voyager, with a DOM-navigation fallback that captures the conversation URN by intercepting the network response |

## 9. The conversational AI (follow-up agent + memory)

`Deal.profile_summary` and `Deal.chat_summary` are lazy, mem0-style JSON fact lists (`{"facts": [...]}`) —
the follow-up agent never sees the raw scraped profile or the entire chat history; it sees a curated,
incrementally-maintained list of facts plus the last 6 raw messages for tone/immediacy.

- **`materialize_profile_summary_if_missing(deal, session)`** (`linkedin/db/summaries.py`) fires once, on the
  first follow-up touch for a `(lead, campaign)` pair: one extra live Voyager re-scrape, `build_profile_text`,
  then `extract_facts()` against that text.
- **`sync_conversation()`** (`linkedin/db/chat.py`) fetches messages via Voyager (or the DOM-navigation
  fallback), upserts `ChatMessage` rows keyed by `linkedin_urn`, and — only if there are genuinely new messages
  — calls `update_chat_summary()`.
- **`update_chat_summary()`** filters to **incoming (lead) messages only** before extraction —
  `_format_messages_for_extraction` returns `""` if every new message is outgoing, which short-circuits the
  whole thing with no LLM call at all. This is deliberate: `chat_summary` is meant to hold facts *about the
  lead*, never a record of the seller's own pitch.
- **`extract_facts(text, seller_name, context)`** — `temperature=0.0`, uses the vendored
  `_FACT_EXTRACTION_PROMPT` plus an **identity-binding block**: `"[Me] is named {seller_name}."` with an
  instruction that any mention of that name inside a `[Lead]` line refers to the seller, not the lead. This
  exists specifically to stop the LLM from inferring "the lead's name is Diego" out of a reply like `"Hola
  Diego, gracias..."` where Diego is the seller being greeted back.
- **`reconcile_facts(existing, new, seller_name)`** — the actual mem0 ADD/UPDATE/DELETE/NONE mechanism:
  1. Existing facts get keyed by their **current list index as a string id** (`{"0": fact0, "1": fact1, ...}`).
  2. mem0's vendored `get_update_memory_messages()` prompt is built from that indexed list plus the new
     candidate facts, with a custom preamble prepended: *"Existing facts that describe {seller_name} as if
     they were the lead are contamination — issue a DELETE for them."* This is the actual mechanism for
     cleaning up facts that were previously mis-attributed before the identity-binding fix existed.
  3. The LLM (temperature 0.0, plain-text output parsed as JSON, with markdown-fence/reasoning-tag stripping
     fallbacks) returns a list of `{id, text, event}` actions.
  4. `_apply_memory_actions` rebuilds the fact store: `ADD` appends at a fresh sequential id; `UPDATE`
     overwrites in place by id (logs+skips if the LLM hallucinated an unknown id); `DELETE` removes by id;
     `NONE` is a no-op. The final list preserves original ordering for untouched facts.
  - **Caveat**: reconciliation — including the contamination cleanup — only runs when there's genuinely new
    incoming content. A dormant Deal (lead has gone silent) never gets its `chat_summary` reconciled again,
    even if it was contaminated before the identity-binding fix landed.

**`run_follow_up_agent(session, deal)`** (`linkedin/agents/follow_up.py`) renders `follow_up_agent.j2` with:
`self_name` (seller's first name), `contact_email` (surfaced as `linkedin_profile.linkedin_username` — the
agent is instructed to offer this instead of ever promising email/calls "outside LinkedIn"), `product_docs`,
`campaign_objective`, `booking_link`, the two fact lists as bullet points, the last 6 `ChatMessage` rows with
humanized ages (`"2h ago"`) tagged `Me`/`Lead`, plus `today`, `days_since_last_outgoing`, and
`unanswered_outgoing`. One `pydantic_ai.Agent` call, `temperature=0.7`, structured output
`FollowUpDecision`:

```python
action: Literal["send_message", "mark_completed", "wait"]
message: str | None          # required if action == send_message
outcome: Literal[...7 values...] | None   # required if action == mark_completed
follow_up_hours: float       # always required
```

The prompt itself (99 lines, the largest of the three templates) instructs a "Mom Test" discovery-before-pitch
approach, spells out all 7 `Outcome` values inline (mirroring the Python enum), gives timing guidance (2–8h if
actively replying, ~24h normal cadence, hard stop after 3 unanswered), and enforces language/formatting rules
(reply in the lead's inferred language, 1–3 sentences, no placeholders, no signature block).

## 10. LLM plumbing

`SiteConfig` (DB singleton) picks one of 7 providers: `openai`/`anthropic`/`google`/`groq`/`mistral`/`cohere`/
`openai_compatible` (the last requires `llm_api_base`). `linkedin/llm.py:get_llm_model()` is the single
factory: validates the config, dispatches to a per-provider builder that wraps the provider's native async SDK
client (`max_retries=8`) in the matching `pydantic_ai.models.*` class. Every call site builds its own
`Agent(get_llm_model(), ...)` per invocation — there's no shared/cached `Agent` or Jinja `Environment` anywhere
in the codebase; every LLM call constructs both fresh.

**`run_agent_sync(coro)`** exists to solve a specific, gnarly interaction between `pydantic_ai`/`anyio` and
Playwright's *synchronous* API, both of which need to touch asyncio's per-thread state:

- `Agent.run_sync()`'s anyio portal leaves the calling thread's "running event loop" slot populated after it
  returns, which then makes any subsequent *synchronous* Playwright call on that same thread raise `"using
  Playwright Sync API inside the asyncio loop"`.
- A bare `asyncio.run()` per call isn't safe either — the openai/anthropic SDKs' `httpx.AsyncClient` schedules
  `self.aclose()` from `__del__` via `get_running_loop().create_task(...)`, which can fire during a *different*
  call's loop if garbage collection is deferred, risking `RuntimeError: Event loop is closed`.

The fix: a module-level singleton background thread (`name="llm-runner"`) runs one `asyncio.new_event_loop()`
forever, for the whole process lifetime. `run_agent_sync` submits the coroutine via
`asyncio.run_coroutine_threadsafe(coro, loop).result()` and blocks the *calling* (daemon/Playwright) thread
until it's done — that thread's own asyncio state is never touched, and every HTTP client's home loop stays
alive for the process's whole life, sidestepping both failure modes. **Every LLM call site in this codebase
must go through `run_agent_sync`, never `Agent.run_sync` directly.**

## 11. Configuration reference

`linkedin/conf.py` (hardcoded constants, no YAML/env layer beyond what's noted):

```python
ENABLE_ACTIVE_HOURS = False        # daemon runs 24/7 by default; flip in source to enable
ACTIVE_START_HOUR = 9; ACTIVE_END_HOUR = 19    # local time, used only if ENABLE_ACTIVE_HOURS=True
ACTIVE_TIMEZONE = system_timezone()            # auto-detected (python tzinfo → TZ env → /etc/timezone → /etc/localtime → "UTC")
REST_DAYS = (5, 6)                 # Sat+Sun

DEFAULT_CONNECT_DAILY_LIMIT = 20
DEFAULT_CONNECT_WEEKLY_LIMIT = 100
DEFAULT_FOLLOW_UP_DAILY_LIMIT = 25

CAMPAIGN_CONFIG = {
    "check_pending_recheck_after_hours": 24,
    "min_action_interval": 120,
    "qualification_n_mc_samples": 100,
    "min_ready_to_connect_prob": 0.9,
    "min_positive_pool_prob": 0.20,
    "embedding_model": "BAAI/bge-small-en-v1.5",
    "connect_delay_seconds": 10,
    "connect_no_candidate_delay_seconds": 300,
    "enrich_min_delay_seconds": 6,
    "enrich_max_delay_seconds": 10,
    "enrich_max_per_page": 10,
    "burst_min_seconds": 2700, "burst_max_seconds": 3900,   # 45-65 min work bursts
    "break_min_seconds": 600,  "break_max_seconds": 1200,   # 10-20 min breaks
}
```

Per-account overrides live on `LinkedInProfile` (`connect_daily_limit`, `connect_weekly_limit`,
`follow_up_daily_limit`), editable in Admin. LLM provider/key/model live on `SiteConfig`, also Admin-editable
(reachable only by direct URL — see [§12](#12-django-admin--dashboard)).

Prompt temperatures, gathered across all LLM call sites:

| Call | Template | Temperature |
|---|---|---|
| Lead qualification | `qualify_lead.j2` | 0.7 |
| Search keyword generation | `search_keywords.j2` | 0.9 |
| Follow-up conversation decision | `follow_up_agent.j2` | 0.7 |
| Fact extraction / mem0 reconciliation | (vendored prompts, `summaries.py`) | 0.0 |

## 12. Django Admin & dashboard

The entire web surface is Django Admin, themed with `unfold` (`INSTALLED_APPS` puts `unfold` before
`django.contrib.admin`; all `ModelAdmin` subclasses in `linkedin/admin.py`/`crm/admin.py` must inherit from
`unfold.admin.ModelAdmin` specifically — a stock `django.contrib.admin.ModelAdmin` still renders inside
Unfold's page chrome but its bulk-action `<select>` lacks the Alpine.js `x-model` binding Unfold's changelist
template expects, so the "Run" button silently never appears).

Notable registrations:

- **`SiteConfigAdmin`** — `has_module_permission` always returns `False`, hiding it from the nav entirely; it's
  a working, editable form, just not linked from anywhere (`/admin/linkedin/siteconfig/1/change/`).
- **`TaskAdmin`** — a `run_now` bulk action, restricted to `PENDING` rows, that sets `scheduled_at=now()`. This
  is the standard way to make a future-dated `follow_up`/`check_pending` task (waiting out its backoff/cooldown)
  execute immediately — e.g. right after a lead replies and you don't want to wait for the natural schedule.
- **`LeadAdmin`** — `mark_human_takeover`/`resume_ai` bulk actions. This is the operator-facing kill switch for
  letting a human take over a specific conversation.
- **`dashboard_callback`** (`linkedin/dashboard.py`) — 4 plain, global (not campaign-scoped) counters rendered
  on the admin index page: messages sent, replies received, connection requests sent, connection requests
  accepted (`Deal` count in `CONNECTED`/`COMPLETED`).

## 13. Onboarding & management commands

- **`onboard`** — interactive (requires a TTY; if `missing_keys()` is empty, it's a no-op) or
  `--non-interactive` with either `--config-file` or individual flags per `OnboardConfig` field.
- **`add_seeds <campaign_id>`** — reads newline-separated profile URLs from stdin, parses them
  (`url_to_public_id`), and creates `Lead` + `QUALIFIED Deal` rows directly, bypassing search/qualification
  entirely.
- **`reset_data`** — wipes `Deal`, `ActionLog`, `Lead` (in that dependency order), resets every
  `SearchKeyword.used` flag, and clears every `Campaign.model_blob` (forces GP retraining from scratch).
  **Keeps** `Campaign` and `LinkedInProfile` rows, so it's meant for "re-run this campaign from scratch against
  the same LinkedIn account/LLM config," not a full factory reset. It does **not** delete `ChatMessage` rows,
  which leaves them pointing (via `GenericForeignKey`) at now-deleted Leads.
- **`OnboardConfig.apply()`** — the single idempotent write path: creates a `Campaign` only if none exists yet
  and a name was given (falling back to `README.md`/`docs/default_campaign.md` for product docs/objective);
  creates a `LinkedInProfile`+`User` only if no active profile exists yet (derives a Django username from the
  email local-part); writes any non-empty LLM fields onto `SiteConfig`; bulk-flips `legal_accepted=True` for
  all active profiles if `legal_acceptance` was set.

## 14. Docker & deployment

`compose/linkedin/Dockerfile` — two-stage build (`uv pip install` for deps, then a runtime stage with
Playwright's Chromium plus a full VNC stack: Xvfb, x11vnc, websockify, noVNC). Runs as a non-root `ubuntu` user
whose UID/GID get remapped to `HOST_UID`/`HOST_GID` at container start (`entrypoint`) so bind-mounted files
keep sane ownership in local dev.

`compose/linkedin/start` supervises **two long-lived processes in one container**, both restart-on-exit:
`runserver 0.0.0.0:8000 --noreload --insecure` (Admin, always up) and `rundaemon` (the automation itself). Both
share the same SQLite file, hence the 30s busy timeout mentioned earlier. Three ports: `8000` (Admin), `6080`
(noVNC — watch the browser live in a normal web browser), `5900` (native VNC client, for watching/debugging the
actual Playwright session).

`Makefile` targets (`make setup`/`make run`/`make up`/etc.) all target `local.yml` (dev compose file, bind-mounts
the whole repo) — the production `docker-compose.yml` has no corresponding `make` target and is meant to be
driven with `docker compose` directly.

## 15. End-to-end walkthrough of a single lead

1. **Discovery** — the connect loop is empty, so `qualify_source` pulls from `search_source`, which uses an
   LLM-generated (or existing) search keyword to run a LinkedIn People Search. `extract_in_urls` finds a new
   profile URL on the results page; `discover_and_enrich` scrapes it via Voyager (throttled) and creates a
   `Lead` row with a cached `embedding`.
2. **Qualification** — that Lead is now "enriched but unqualified." The GP+BALD acquisition function (in
   exploit or explore mode depending on the current label balance) may or may not pick it as the single most
   valuable candidate this round; once picked, the qualification LLM (`qualify_lead.j2`) judges fit against the
   campaign's `product_docs`/`campaign_objective`. Accept → `QUALIFIED` Deal created, GP updated with a
   positive label. Reject → `FAILED` Deal, `outcome=wrong_fit`, GP updated with a negative label.
3. **Ready gate** — once its exact posterior `P(f > 0.5)` clears `0.9`, `promote_to_ready()` flips it to
   `READY_TO_CONNECT`.
4. **Connect** — the next `connect` task's `find_candidate()` picks it up (subject to rate limits), sends the
   connection request, sets `PENDING`, and `on_deal_state_entered` auto-enqueues a `check_pending` task.
5. **Waiting** — `check_pending` re-checks status on a doubling+jittered backoff (starting at 24h) until either
   accepted or it gives up (`SkipProfile` → `FAILED`).
6. **Connected** — acceptance flips the Deal to `CONNECTED`, which auto-enqueues a `follow_up` task.
7. **Conversation** — each `follow_up` cycle syncs the real LinkedIn thread first, checks the unanswered-reply
   gate, then (if not throttled) asks the follow-up LLM agent — armed with `profile_summary`/`chat_summary`
   fact lists plus the last 6 raw messages — to send a message, wait, or close out the Deal with a specific
   `Outcome`. Every incoming reply gets folded into `chat_summary` via the mem0-style reconcile step.
8. **Terminal** — the Deal ends either because the agent explicitly calls `mark_completed` (converted,
   not_interested, no_budget, etc.) or automatically, after 3 consecutive unanswered outgoing messages,
   as `outcome="unresponsive"`.

At every step, if the process crashes mid-task, the next idle-cycle `reconcile()` call notices the Deal's state
doesn't match the Task table and recreates whatever task is implied — nothing needs to remember "where it was."

## 16. Known quirks, dead code, and rough edges

Verified directly against the current source — useful to know before relying on documentation elsewhere that
may describe an idealized rather than actual behavior:

- **`OnboardConfig.from_json()` is called but never defined.** `linkedin/management/commands/onboard.py:57`
  calls `OnboardConfig.from_json(options["config_file"])`; no such method exists anywhere in the repo (only
  mentioned in docstrings). Running `onboard --non-interactive --config-file=...` will raise `AttributeError`.
  The `rundaemon` startup path doesn't hit this — it calls `collect_from_wizard()`, not `from_json`.
- **`collect_from_wizard()` isn't interactive despite its name.** It just probes 3 fixed paths for
  `config.json` (`data/`, cwd, `~/.openoutreach/`) and falls back to dataclass defaults — no `questionary`
  prompts fire. The old interactive-prompt implementation still exists, but only as a byte-for-byte
  commented-out duplicate of the top half of `linkedin/onboarding.py`.
- **`TerminalStateError`** (`linkedin/exceptions.py`) is defined with a docstring implying active use in the
  state machine, but is never raised or caught anywhere in the codebase.
- **`ENABLE_ACTIVE_HOURS = False`** in the current `conf.py` — the daemon runs continuously, 24/7, by default.
  This is a source-level flag with no env-var or Admin override.
- **`apply_gdpr_newsletter_override()`** (`linkedin/setup/gdpr.py`) is only exercised by tests
  (`tests/test_gdpr.py`) — it's never called from `rundaemon.py`, whose own `_ensure_newsletter` step
  unconditionally force-disables the subscription and marks `newsletter_processed=True` on first run,
  regardless of the lead's country.
- **`linkedin/pipeline/freemium_pool.py`** and **`linkedin/ml/hub.py`** are fully intact, non-trivial modules —
  not stubs — that simply aren't wired into the live daemon path (freemium kit download/import is disabled per
  project policy, not deleted from the code).
- **Messaging API vs. profile API error semantics differ.** `api/client.py` treats a profile 403/404 as
  non-retried `ProfileInaccessibleError`; `api/messaging/utils.py:check_response` treats a messaging 403/404 as
  a **retried** generic `IOError`.
- **A failed follow-up send demotes the whole Deal**, not just the message — `handle_follow_up` rolls the
  state back to `QUALIFIED` on send failure, re-entering the connect pipeline rather than retrying the send.
- **`human_takeover` leads go fully dormant.** `handle_follow_up` returns without re-enqueuing when the flag is
  set, and `reconcile()`'s deal-sweep (`_seed_deal_tasks`) explicitly filters `lead__human_takeover=False` — so
  no automated task will ever be recreated for that Deal until an operator flips `resume_ai` in Admin.
- **No 2FA/security-checkpoint handling in the login flow** — a LinkedIn challenge screen during login just
  times out as a generic `RuntimeError`, with no detection or wait-out branch.
- **Voyager GraphQL `queryId`s are hardcoded** in `api/messaging/conversations.py` — if LinkedIn rotates these
  persisted-query hashes, requests fail with no specific "stale queryId" detection.
- **`reset_data` orphans `ChatMessage` rows** — it deletes `Lead` rows but not the `ChatMessage` rows whose
  `GenericForeignKey` points at them.
- **`chat/models.py`'s `recipients` and `to` M2M fields** are declared but never populated by any sync code —
  vestigial.
- **`django_settings.py` hardcodes `TIME_ZONE = "Asia/Kolkata"`**, distinct from `conf.py`'s
  `ACTIVE_TIMEZONE = system_timezone()` (auto-detected) used for the active-hours gate — two different
  timezone concepts in the same project, easy to conflate.
- **`Makefile` only drives `local.yml`** — the production `docker-compose.yml` has no corresponding `make`
  target.
