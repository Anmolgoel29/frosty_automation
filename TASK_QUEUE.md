# The Task Queue Engine, In Depth

A deep dive into how OpenOutreach schedules, executes, and recovers work. This expands on
[EXPLANATION.md §4](EXPLANATION.md#4-the-task-queue-engine) with full code, exact formulas, and worked
failure scenarios. Every claim below cites `file.py:line` and, where one exists, the test that pins the
behavior down.

The three files that make up the whole engine:

- `linkedin/models.py:210-269` — the `Task` model + `TaskQuerySet`.
- `linkedin/tasks/scheduler.py` — the only code allowed to write `Task` rows.
- `linkedin/daemon.py` — the worker loop that reads and executes them.

## Table of contents

1. [Design philosophy](#1-design-philosophy)
2. [The `Task` model](#2-the-task-model)
3. [Task lifecycle and terminal states](#3-task-lifecycle-and-terminal-states)
4. [The three task types](#4-the-three-task-types)
5. [`scheduler.py`, layer by layer](#5-schedulerpy-layer-by-layer)
6. [`reconcile()`: the retry mechanism](#6-reconcile-the-retry-mechanism)
7. [The daemon worker loop](#7-the-daemon-worker-loop)
8. [The active-hours guard](#8-the-active-hours-guard)
9. [Failure handling matrix](#9-failure-handling-matrix)
10. [Diagnostics capture](#10-diagnostics-capture)
11. [Human-rhythm pacing](#11-human-rhythm-pacing)
12. [Single-threading assumptions](#12-single-threading-assumptions)
13. [Admin "Run now" and the poll loop](#13-admin-run-now-and-the-poll-loop)
14. [Worked scenario: surviving a crash](#14-worked-scenario-surviving-a-crash)
15. [Test coverage map](#15-test-coverage-map)
16. [Invariants that make this correct](#16-invariants-that-make-this-correct)

---

## 1. Design philosophy

There is no Celery, no Redis, no message broker. The queue **is** a SQLite table. This works because of
three deliberate constraints:

- **Single worker, single task at a time.** One daemon process claims one `Task` row, runs its handler to
  completion, then claims the next. `daemon.py:202-203` states this explicitly in a comment: *"Single-threaded:
  one task at a time, no concurrent enqueuing, so sleeping until the next `scheduled_at` is safe."*
- **The queue is a reflection of CRM state, not the source of truth.** The `Deal.state` column is the source
  of truth for "what should happen to this lead next." The `Task` table is just a scheduled reminder to act on
  that state. If the reminder is lost (crash, bug, manual deletion), `reconcile()` regenerates it from `Deal`
  state — nothing about the pipeline's correctness depends on a `Task` row surviving.
- **Enqueue is idempotent, not append-only.** Every enqueue function checks for an existing `PENDING` row with
  the same identifying payload before inserting. This means the same "please act on this deal" signal can be
  fired from multiple places (a handler's own reschedule, `on_deal_state_entered`, `reconcile()`) without ever
  producing duplicate work.

## 2. The `Task` model

`linkedin/models.py:210-269`:

```python
class TaskQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=Task.Status.PENDING).order_by("scheduled_at")

    def claim_next(self) -> "Task | None":
        return self.pending().filter(scheduled_at__lte=timezone.now()).first()

    def seconds_to_next(self) -> float | None:
        next_task = self.pending().only("scheduled_at").first()
        if next_task is None:
            return None
        return max((next_task.scheduled_at - timezone.now()).total_seconds(), 0)


class Task(models.Model):
    class TaskType(models.TextChoices):
        CONNECT = "connect"
        CHECK_PENDING = "check_pending"
        FOLLOW_UP = "follow_up"

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"

    task_type = models.CharField(max_length=20, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    scheduled_at = models.DateTimeField()
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = TaskQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["status", "scheduled_at"])]
```

Three important properties fall directly out of this:

- **`claim_next()` is a plain "first ready row" query, not a `SELECT ... FOR UPDATE`.** There's no row-locking
  or `SKIP LOCKED` — safe only because exactly one process ever calls it ([§12](#12-single-threading-assumptions)).
- **`payload` is untyped JSON.** Each task type has its own implicit schema (see [§4](#4-the-three-task-types)),
  enforced only by convention (handlers do `payload["public_id"]` and let it raise `KeyError` if malformed —
  there's no payload validation layer).
- **The composite index is `(status, scheduled_at)`** — exactly the two columns `pending()` and `claim_next()`
  filter/order by, so both queries hit the index directly even as the table grows.

`mark_running()` / `mark_completed()` / `mark_failed()` (`models.py:256-268`) are simple: set `status` (+
`started_at`/`completed_at` where relevant) and `save(update_fields=[...])` — partial saves, so they don't
clobber concurrent changes to `payload` or other fields (not that any exist under the single-worker model, but
it's a cheap correctness habit).

## 3. Task lifecycle and terminal states

```
                 ┌─────────────┐
   insert ─────► │   PENDING   │ ◄────────────────────┐
                 └──────┬──────┘                       │
                        │ claim_next() + mark_running() │ new row inserted by
                        ▼                               │ scheduler / reconcile
                 ┌─────────────┐                        │ (NOT a transition of
                 │   RUNNING   │                        │  the old row!)
                 └──────┬──────┘                        │
             ┌──────────┴──────────┐                    │
             ▼                     ▼                    │
      ┌─────────────┐       ┌─────────────┐             │
      │  COMPLETED  │       │   FAILED    │─────────────┘
      └─────────────┘       └─────────────┘
        (terminal,             (terminal, UNLESS the daemon crashed
         no further             mid-execution — see the RUNNING→PENDING
         action)                recovery path below)
```

**`COMPLETED` and `FAILED` rows are never touched again.** There is no code path that flips a `FAILED` task
back to `PENDING` and retries *the same row*. Instead, retry happens by **inserting a brand-new row** — and
because `_insert_task`'s dedup check only looks at `status=PENDING` rows
(`scheduler.py:58-66`), a `FAILED` or `COMPLETED` row for the same `(task_type, public_id, campaign_id)` never
blocks that new insert. This is confirmed directly by
`tests/test_reconcile.py:test_does_not_create_for_completed_tasks` — a `COMPLETED` `check_pending` row for
`alice` sitting in the table does not stop `reconcile()` from creating a fresh `PENDING` one.

The **one** row-level status transition that isn't a fresh insert is `RUNNING → PENDING`, and it only exists to
undo a crash: `_recover_stale_running_tasks()` (`scheduler.py:166-174`) resets any `RUNNING` row back to
`PENDING` on the theory that a `RUNNING` row can only still exist if the daemon died before calling
`mark_completed()`/`mark_failed()` — under the single-worker model, nothing else could be "currently running"
it.

## 4. The three task types

| `task_type` | Payload shape | Handler | Enqueued by |
|---|---|---|---|
| `connect` | `{"campaign_id": int}` | `handle_connect` (`tasks/connect.py`) | `enqueue_connect()` — self-reschedule, or `reconcile()`'s `_seed_connect_tasks` (one per campaign) |
| `check_pending` | `{"campaign_id": int, "public_id": str, "backoff_hours": float}` | `handle_check_pending` (`tasks/check_pending.py`) | `enqueue_check_pending()` — via `on_deal_state_entered` whenever a Deal enters `PENDING` |
| `follow_up` | `{"campaign_id": int, "public_id": str}` | `handle_follow_up` (`tasks/follow_up.py`) | `enqueue_follow_up()` — via `on_deal_state_entered` whenever a Deal enters `CONNECTED` |

Note `connect` is **campaign-scoped only** — there's exactly one live `connect` task per campaign at a time,
representing "the connect loop for this campaign is running." `check_pending` and `follow_up` are
**per-profile** — one task per `(campaign, public_id)` in flight.

## 5. `scheduler.py`, layer by layer

### Layer 1 — low-level enqueue + dedup

```python
def _insert_task(task_type, payload, delay_seconds, dedup_keys=None) -> bool:
    filter_kwargs = {"task_type": task_type, "status": Task.Status.PENDING}
    for key in (dedup_keys if dedup_keys is not None else payload):
        filter_kwargs[f"payload__{key}"] = payload[key]

    if Task.objects.filter(**filter_kwargs).exists():
        return False

    Task.objects.create(
        task_type=task_type,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
        payload=payload,
    )
    return True
```

`payload__{key}` uses Django's JSONField key-lookup (`payload__campaign_id=5` becomes a query against the
JSON column). `dedup_keys` lets a caller dedup on a *subset* of the payload — this matters for
`enqueue_check_pending`, whose payload includes `backoff_hours` (which changes every call) but should still
dedup against an existing row that only differs in that one field:

```python
def enqueue_connect(campaign_id, delay_seconds=10):
    _insert_task(Task.TaskType.CONNECT, {"campaign_id": campaign_id}, delay_seconds)
    # dedup_keys=None → dedups on the whole payload, i.e. just campaign_id here

def enqueue_check_pending(campaign_id, public_id, backoff_hours):
    half = backoff_hours / 2
    delay_hours = half + random.uniform(0, half)          # ← the jitter
    _insert_task(
        Task.TaskType.CHECK_PENDING,
        {"campaign_id": campaign_id, "public_id": public_id, "backoff_hours": backoff_hours},
        delay_seconds=delay_hours * 3600,
        dedup_keys=["campaign_id", "public_id"],           # ← ignores backoff_hours for dedup
    )
    return delay_hours

def enqueue_follow_up(campaign_id, public_id, delay_seconds=10):
    _insert_task(
        Task.TaskType.FOLLOW_UP,
        {"campaign_id": campaign_id, "public_id": public_id},
        delay_seconds,
        dedup_keys=["campaign_id", "public_id"],
    )
```

**The backoff/jitter split**: `check_pending.py:_bump_backoff` *doubles* `Deal.backoff_hours` with no
randomness (`new = current * 2`). That doubled value is what gets passed into `enqueue_check_pending`, which
then picks the *actual* delay as `uniform(backoff/2, backoff)` — an equal-jitter strategy layered on top of
plain exponential doubling. So "24h → 48h → 96h → ..." is the *ceiling* each round scales toward, and the real
wait is a random point in the lower half of that range each time. Confirmed exactly by
`tests/tasks/test_tasks.py:test_stays_pending_and_doubles_backoff` (72h → 144h on the `Deal.backoff_hours`
column, unconditionally) — the jitter itself isn't asserted there since it's randomized, but the doubling math
is pinned.

### Layer 2 — the state-transition hook

```python
def on_deal_state_entered(deal) -> None:
    state = ProfileState(deal.state)
    campaign_id = deal.campaign_id
    public_id = deal.lead.public_identifier
    if not public_id:
        return
    if state == ProfileState.PENDING:
        backoff = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
        enqueue_check_pending(campaign_id, public_id, backoff_hours=backoff)
    elif state == ProfileState.CONNECTED:
        enqueue_follow_up(campaign_id, public_id)
    # QUALIFIED, READY_TO_CONNECT, COMPLETED, FAILED: no implied task.
```

This is called from **every** `set_profile_state()` call (`linkedin/db/deals.py`), **unconditionally — even if
the new state equals the current state**. That's not a bug: because enqueue is dedup'd against existing
`PENDING` rows, calling it redundantly is a no-op in the common case. It only actually inserts a new row when
one doesn't already exist — which is exactly the situation after a crash wiped out the previous one.

This hook is what makes the pipeline self-propelling: nothing in `handle_connect`, `handle_check_pending`, or
`handle_follow_up` explicitly says "and now enqueue a check_pending task." They just call `set_profile_state()`
with the new state, and the hook decides what (if anything) needs to happen next.

## 6. `reconcile()`: the retry mechanism

```python
def reconcile(session) -> None:
    _recover_stale_running_tasks()
    _seed_connect_tasks(session)
    _seed_deal_tasks(session)
    pending_count = Task.objects.pending().count()
    logger.info("Task queue reconciled: %d pending tasks", pending_count)
```

Three sub-steps:

1. **`_recover_stale_running_tasks()`** — `Task.objects.filter(status=RUNNING).update(status=PENDING)`. Blind
   bulk update; no per-row inspection needed because, under the single-worker model, any `RUNNING` row found
   here can only mean the daemon died mid-task.
2. **`_seed_connect_tasks(session)`** — `for campaign in session.campaigns: enqueue_connect(campaign.pk,
   delay_seconds=0)`. Guarantees every campaign always has a live connect loop, even if its task was somehow
   lost.
3. **`_seed_deal_tasks(session)`** — for every `Deal` in `PENDING` or `CONNECTED` state (excluding
   `lead__human_takeover=True`), calls `on_deal_state_entered(deal)`. Because that function is dedup-safe, this
   is just "make sure every active deal has the task its state implies" — deals that already have a pending
   task are untouched; deals that don't get one created.

```python
def _seed_deal_tasks(session) -> None:
    from crm.models import Deal
    active_states = (ProfileState.PENDING, ProfileState.CONNECTED)
    for campaign in session.campaigns:
        deals = Deal.objects.filter(
            state__in=active_states, campaign=campaign, lead__human_takeover=False,
        ).select_related("lead")
        for deal in deals:
            if deal.state == ProfileState.CONNECTED and deal.lead.human_takeover:
                continue   # belt-and-suspenders; the queryset filter already excludes these
            on_deal_state_entered(deal)
```

(The inner `if` is redundant with the queryset's `lead__human_takeover=False` filter — both exist, presumably
defense-in-depth against a future refactor of the filter.)

**Why this is "the retry mechanism":** a task handler that raises mid-execution gets caught by
`daemon.py`'s outer `try/except`, which calls `task.mark_failed()`. That `FAILED` row is now inert — nothing
will ever revive it. But the `Deal` it was working on is still sitting in `PENDING` or `CONNECTED` (whatever
state it was in before the crash), because the handler never got to call `set_profile_state()` for whatever
transition it was about to make. The *next* time the daemon's queue drains (which happens on literally every
idle cycle, not just at startup), `reconcile()` walks all active deals, notices this one has no `PENDING` task,
and creates a fresh one via `on_deal_state_entered`. From the pipeline's perspective, work resumes as if the
crash never happened — at the cost of re-doing whatever partial work the crashed handler had done (there's no
resumability *within* a handler, only *across* handler invocations).

## 7. The daemon worker loop

`linkedin/daemon.py:run_daemon`, annotated:

```python
while True:
    pause = seconds_until_active()             # §8
    if pause > 0:
        sleep_with_heartbeat(pause, heartbeat, ...)
        rhythm.reset()
        continue

    task = Task.objects.claim_next()           # first PENDING row with scheduled_at <= now
    if task is None:
        reconcile(session)                      # §6 — fires on EVERY empty poll, not just startup
        wait = Task.objects.seconds_to_next()
        if wait is None:
            sleep_with_heartbeat(3600, heartbeat, "queue empty")
            rhythm.reset()
            continue
        if wait > 0:
            # poll in <=60s slices instead of one long sleep — §13
            while wait is not None and wait > 0:
                sleep_with_heartbeat(min(wait, QUEUE_POLL_INTERVAL), heartbeat, ...)
                wait = Task.objects.seconds_to_next()
            rhythm.reset()
        continue

    campaign = Campaign.objects.filter(pk=task.payload.get("campaign_id")).first()
    if not campaign:
        task.mark_failed()
        continue

    session.campaign = campaign                # every handler reads session.campaign
    task.mark_running()

    handler = _HANDLERS.get(task.task_type)
    if handler is None:
        task.mark_failed()
        continue

    try:
        with failure_diagnostics(session):      # §10
            handler(task, session, qualifiers)
    except AuthenticationError:                 # §9
        session.reauthenticate()
        task.mark_failed()
        continue
    except ModelHTTPError:
        task.mark_failed()
        return                                  # daemon PROCESS EXITS
    except Exception:
        task.mark_failed()
        continue

    task.mark_completed()
    rhythm.maybe_break()                        # §11
```

Two things worth calling out that are easy to miss on a first read:

- **Every single iteration re-derives `campaign` from the *task's own payload*, not from whatever
  `session.campaign` was left set to by the previous iteration.** Since `Task.payload["campaign_id"]` is set at
  enqueue time and campaigns are otherwise independent, this is what lets one daemon process correctly
  interleave work across multiple campaigns sharing the same LinkedIn account/browser session — each task
  carries its own campaign with it.
- **A missing campaign (`Campaign.objects.filter(...).first()` returns `None`) just fails the task and moves
  on** — no exception, no diagnostics capture, no reconcile-triggering log. This would only happen if a
  campaign was deleted out from under a still-pending task.

## 8. The active-hours guard

`seconds_until_active()` (`daemon.py:154-173`):

```python
def seconds_until_active() -> float:
    if not ENABLE_ACTIVE_HOURS:
        return 0.0
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)

    if now.weekday() not in REST_DAYS and ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR:
        return 0.0

    candidate = timezone.make_aware(
        now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0, tzinfo=None), timezone=tz,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() in REST_DAYS:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()
```

**As of the current `linkedin/conf.py`, `ENABLE_ACTIVE_HOURS = False`**, so this function always returns `0.0`
and the daemon runs continuously — the active-hours machinery below only matters if that flag is flipped in
source.

Semantics pinned by `tests/test_schedule.py` (all against `ACTIVE_START_HOUR=9`, `ACTIVE_END_HOUR=17`,
`REST_DAYS=(5,6)`):

- **Start is inclusive, end is exclusive** — exactly `09:00` is active (`test_at_exact_start`); exactly `17:00`
  is *not* (`test_at_exact_end`, rolls to next day 9am).
- **Weekend-skipping walks forward day-by-day** through `REST_DAYS` from the candidate, not just "add 2 days if
  Friday" — verified for both a Friday-evening trigger (63h to Monday 9am) and a Saturday-noon trigger (45h to
  Monday 9am).
- **`ACTIVE_TIMEZONE` genuinely localizes** — `08:00 Europe/Berlin` with a 9am start returns exactly 1h, not
  computed against server-local or UTC time.
- **`REST_DAYS = ()` (no rest days configured)** correctly falls through to "active now" checks alone, with no
  infinite loop in the day-walking logic.
- **`ENABLE_ACTIVE_HOURS = False` short-circuits everything** — even 11pm on a rest day returns `0.0`
  (`test_disabled_always_active`).

## 9. Failure handling matrix

| Exception raised by handler | Task | Session | Daemon |
|---|---|---|---|
| `AuthenticationError` (401 from Voyager) | `mark_failed()` | `session.reauthenticate()` — closes browser, clears `cookie_data`, forces fresh login | Continues; `reconcile()` recreates the task next idle cycle |
| `ModelHTTPError` (from `pydantic_ai` — bad LLM key, quota, provider outage) | `mark_failed()` | untouched | **`return` — the daemon process exits entirely.** Only Docker's restart-on-exit supervision (`compose/linkedin/start`) brings it back; running `make run` bare requires a manual restart. |
| Any other `Exception` | `mark_failed()` | untouched | Continues; `reconcile()` recreates the task next idle cycle |
| *(none — handler returns normally)* | `mark_completed()` | — | Continues; `rhythm.maybe_break()` may insert a human-like pause |

`session.reauthenticate()` itself is wrapped in its own inner `try/except Exception: logger.exception(...)` —
even if re-login fails outright, the task still gets marked `FAILED` and the loop continues rather than
propagating a second exception out of the handler's `except AuthenticationError` block.

## 10. Diagnostics capture

`linkedin/diagnostics.py` wraps every handler invocation:

```python
@contextmanager
def failure_diagnostics(session):
    try:
        yield
    except Exception as exc:
        try:
            capture_failure(session, exc)
        except Exception as cap_exc:
            logger.debug("Diagnostic capture itself failed: %s", cap_exc)
        raise
```

It is **purely observational** — it always re-raises, so it never changes what the daemon loop's `except`
clauses see; it just gets a chance to write a post-mortem first. `capture_failure()` creates
`/tmp/openoutreach-diagnostics/<timestamp>_<ExceptionClassName>/` containing:

- `error.txt` — full `traceback.format_exception(...)`.
- `page.html` — `page.content()`, or a placeholder comment if `session.page` is `None`/closed.
- `screenshot.png` — `page.screenshot()`.

HTML capture and screenshot capture are each individually wrapped in their own `try/except Exception:
logger.debug(...)` — a failure to grab a screenshot (e.g. the page navigated away mid-capture) doesn't prevent
the HTML dump, and vice versa, and neither prevents `error.txt` from being written first.

## 11. Human-rhythm pacing

Two small stateful helpers in `daemon.py` make the daemon's activity pattern look less like a script running in
a tight loop:

- **`Heartbeat`** — logs `alive — <context>` at most once per `HEARTBEAT_INTERVAL` (300s). The first call never
  logs (`_last` is initialized to "now"), so a quiet period is measured from daemon start, not from the Unix
  epoch.
- **`_HumanRhythmBreak`** — tracks a "burst" of continuous work. After each **successfully completed** task,
  `maybe_break()` checks whether the current burst (a random duration in `[2700, 3900]`s = 45–65 min) has
  elapsed; if so, it sleeps a random break (`[600, 1200]`s = 10–20 min, itself using `sleep_with_heartbeat` so
  the daemon keeps logging `alive` during the break) and starts a new burst timer. `reset()` — called after
  *idle* sleeps (active-hours pause, empty-queue sleep, waiting for a future task) — restarts the burst clock
  without charging a break, since idle time already looks like "not working," and burst timing is meant to
  track continuous *activity*, not wall-clock time.

Neither of these affects correctness — they exist purely to make the account's activity pattern look
human-paced rather than machine-paced.

## 12. Single-threading assumptions

The entire engine's simplicity rests on "exactly one process, one `AccountSession`, one task at a time."
Concretely, this shows up in a few places that would need real locking under any other concurrency model:

- `Task.objects.claim_next()` is a plain `SELECT ... LIMIT 1`, not `SELECT ... FOR UPDATE SKIP LOCKED` — two
  daemons running against the same DB could claim the same task.
- `linkedin/browser/registry.py`'s `_sessions: dict[int, AccountSession]` module-level cache has no locking.
- `daemon.py`'s own comment justifies sleeping for the full inter-task delay (rather than re-checking
  continuously) specifically because *"no concurrent enqueuing"* is assumed.

This is a correct and simple design for the project's actual deployment shape (one container, one daemon
process supervising one browser, per `compose/linkedin/start`) — it would need `SKIP LOCKED`-style claiming and
per-session locking before it could safely support multiple daemon workers against the same database.

## 13. Admin "Run now" and the poll loop

`linkedin/admin.py:TaskAdmin.run_now` (bulk action, `PENDING`-only):

```python
@admin.action(description="Run now (jump the queue)")
def run_now(self, request, queryset):
    pending = queryset.filter(status=Task.Status.PENDING)
    skipped = queryset.exclude(status=Task.Status.PENDING).count()
    updated = pending.update(scheduled_at=timezone.now())
    ...
```

It does not execute anything itself — it only rewrites `scheduled_at` to "now" on already-`PENDING` rows.
Getting that task to actually run within a reasonable time depends entirely on the daemon loop's polling
behavior described in [§7](#7-the-daemon-worker-loop): when the daemon is waiting for a future task, it sleeps
in `QUEUE_POLL_INTERVAL` (60s) slices and re-checks `Task.objects.seconds_to_next()` after each slice, rather
than committing to one long `time.sleep()` for the full originally-computed delay. So a `follow_up` task that
was scheduled 20 hours out (waiting on its cooldown) and gets "Run now"'d becomes eligible for `claim_next()`
within, at most, about a minute — which is exactly the wording of the success message shown in Admin.

This is also why `handle_follow_up` re-syncs the conversation *before* its gating logic
([EXPLANATION.md §5](EXPLANATION.md#5-task-handlers-in-detail)) — without that ordering, "Run now" against a
task that's waiting out `_too_soon_to_nudge` would just re-evaluate the same stale local messages and
re-enqueue itself right back into the cooldown, making the button a no-op in exactly the situation (a lead just
replied) an operator would use it for.

## 14. Worked scenario: surviving a crash

This traces `tests/test_reconcile.py:test_recreates_task_after_handler_crash`, which is the concrete proof of
the whole retry mechanism:

1. A `Deal` for lead `alice` is `PENDING` (connection request sent, awaiting acceptance).
2. A `check_pending` task for `alice` exists but is `FAILED` — simulating a handler that crashed partway
   through (e.g. `get_connection_status` raised an unexpected exception that propagated out of
   `handle_check_pending`, got caught by the daemon's generic `except Exception`, and `mark_failed()` was
   called on the row).
3. At this point: **no `PENDING` `check_pending` task exists for `alice`.** The `FAILED` row is inert — nothing
   will read `scheduled_at` off it again, and nothing will "retry" it in place.
4. The daemon's queue eventually drains (or this is the very next idle cycle), so `run_daemon`'s loop calls
   `reconcile(session)`.
5. `reconcile()`'s `_seed_deal_tasks` finds `alice`'s `Deal` still sitting in `PENDING` state (the crashed
   handler never got to call `set_profile_state()`), calls `on_deal_state_entered(deal)`, which calls
   `enqueue_check_pending(...)`. Since no `PENDING` row exists for `(campaign_id, public_id="alice")`, the dedup
   check passes and a fresh row is inserted.
6. The next `claim_next()` picks up this new row and the pipeline continues as if the crash never happened.

The only thing lost was the *partial progress* of the crashed run and one backoff round's worth of a fresh
`check_pending_recheck_after_hours` (24h) delay (since the new task uses `deal.backoff_hours` — whatever it was
last set to — not a value informed by the crashed attempt). Nothing about the Deal's business state was lost,
because that lives in the `Deal` row, not in the `Task` row.

## 15. Test coverage map

| File | What it pins down |
|---|---|
| `tests/test_reconcile.py` | Stale-`RUNNING` recovery, one-`connect`-per-campaign seeding, `check_pending`/`follow_up` re-derivation from `Deal` state, backoff value propagation into recreated tasks, idempotency across repeated `reconcile()` calls, and the crash-recovery scenario in [§14](#14-worked-scenario-surviving-a-crash). |
| `tests/test_schedule.py` | Every boundary condition of `seconds_until_active()` — see [§8](#8-the-active-hours-guard). |
| `tests/tasks/test_tasks.py` | Each handler's branch coverage: connect success/rate-limit/skip/no-candidate/self-reschedule; check_pending's exact backoff-doubling math and no-op-on-missing-Deal; follow_up's send-success/send-failure-demotes-to-QUALIFIED/mark_completed/wait/rate-limit paths, and — notably — `test_fresh_reply_synced_mid_handler_bypasses_too_soon_gate`, which is the executable proof behind the "sync before gating" ordering discussed in [§13](#13-admin-run-now-and-the-poll-loop). |

No test file directly drives `daemon.py:run_daemon`'s top-level loop end-to-end (it's exercised indirectly
through the handler and reconcile tests) — the loop's own logic (polling cadence, exception routing,
heartbeat/rhythm timing) is covered by direct code reading rather than an integration test, as of this writing.

## 16. Invariants that make this correct

Pulling the whole design together, the system tolerates crashes, restarts, and manual admin intervention
because of a small set of invariants that hold everywhere:

1. **`Deal.state` is the only durable source of truth for "what's supposed to happen to this lead."** `Task`
   rows are disposable hints derived from it.
2. **Enqueueing is idempotent** (dedup against `PENDING` rows on the relevant payload keys), so calling it
   redundantly — from a handler's self-reschedule, from `on_deal_state_entered`, or from `reconcile()` — never
   creates duplicate work.
3. **`reconcile()` runs on every idle cycle, not just at startup**, so the gap between "a task died" and "a
   replacement exists" is bounded by how quickly the queue next drains — typically seconds to minutes, not
   until the next process restart.
4. **`FAILED`/`COMPLETED` rows are append-only history, never mutated back into the active queue.** Retries are
   always fresh rows, which keeps the "does an active task already exist" check simple (just look at `PENDING`)
   at the cost of leaving failed attempts as permanent audit trail in the table.
5. **Every task carries its own `campaign_id`**, so a single daemon/session can correctly interleave multiple
   campaigns without any handler needing to guess which campaign it's operating in from ambient state.
