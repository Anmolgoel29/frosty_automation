# Follow-Up Agent

The follow-up agent manages LinkedIn DM conversations with connected leads. It
runs as a self-rescheduling loop: every decision that isn't `mark_completed`
creates a new Task, so the daemon keeps checking in on each conversation until
the deal is closed.

## Pipeline Overview

```
CONNECTED lead
    │
    ▼
scheduler.on_deal_state_entered() ← fired from set_profile_state(CONNECTED)
    │
    ▼
enqueue_follow_up()          ← in linkedin/tasks/scheduler.py
    │
    ▼
daemon picks up Task
    │
    ▼
handle_follow_up()           ← linkedin/tasks/follow_up.py
    ├─ rate limit check      ← LinkedInProfile.can_execute(FOLLOW_UP)
    ├─ sync conversation     ← Voyager API → ChatMessage upsert (no summarisation)
    ├─ cooldown gates        ← _unanswered_count / _too_soon_to_nudge
    ├─ materialize profile summary (lazy, once per lead×campaign)
    └─ run_follow_up_agent() ← linkedin/agents/follow_up.py, reads the FULL thread
         │
         ▼
    FollowUpDecision
    ┌────────────────────────────────────────────────────────┐
    │ send_message   → send DM, record action, re-enqueue   │
    │ wait           → re-enqueue (no message sent)          │
    │ mark_completed → close Deal with structured outcome    │
    └────────────────────────────────────────────────────────┘
```

## FollowUpDecision

Structured LLM output defined in `linkedin/agents/follow_up.py`:

| Field | Type | Required When |
|-------|------|---------------|
| `action` | `"send_message"` / `"mark_completed"` / `"wait"` | always |
| `message` | `str` | `send_message` |
| `outcome` | `Outcome` enum | `mark_completed` |
| `follow_up_hours` | `float` | always (agent decides the pace) |

Outcome values: `converted`, `not_interested`, `wrong_fit`, `no_budget`,
`has_solution`, `bad_timing`, `unresponsive`.

Validated by a Pydantic `model_validator` — the LLM call fails if required
fields are missing for the chosen action.

## Agent Context

The agent sees a prompt rendered from `follow_up_agent.j2` with:

| Section | Source | Built When |
|---------|--------|------------|
| Seller identity (`self_name`) | `session.self_profile` | every call |
| Product docs, campaign objective, booking link | `Campaign` model | every call |
| Profile facts | `Deal.profile_summary` (JSON fact list) | lazy, once per lead×campaign |
| **Full conversation transcript** | **every** `ChatMessage` row for the lead | every call |
| `message_count` | length of that transcript | every call |
| `days_since_last_outgoing` | computed from messages | every call |
| `unanswered_outgoing` count | trailing run of outgoing messages | every call |

### The transcript

`_load_conversation(deal)` reads the whole thread, oldest first, and
`_format_transcript()` renders it as numbered, speaker-tagged, dated turns
inside explicit `--- BEGIN TRANSCRIPT ---` / `--- END TRANSCRIPT ---` fences:

```
[1] YOU (Diego Ruiz) — 2026-08-10 09:14 (23d ago)
    Hola Andrea, vi que lideras ops en Acme.

[2] LEAD — 2026-08-10 11:02 (23d ago)
    Hola Diego! Si, desde hace dos anos.
    En que puedo ayudarte?
```

Multi-line bodies stay indented under their turn so a line break inside one
message can't read as a new turn. The template tells the agent, in as many
words, that these lines are the *only* record of what happened: anything not
literally present did not occur, and the `YOU (...)` lines are the complete set
of things it has ever told, offered, promised, or asked this lead.

`MAX_TRANSCRIPT_MESSAGES` (200) and `MAX_TRANSCRIPT_CHARS` (60 000) exist purely
so a pathological thread can't blow the context window. Both sit far above any
real LinkedIn conversation. When either bites, the newest messages are kept and
the header says how many older ones were dropped — the agent is never handed a
partial history it believes is complete.

### Why there is no chat summariser

The previous design compressed the thread into a `Deal.chat_summary` fact list
via two chained LLM calls per sync (`extract_facts` → mem0 `reconcile_facts`),
and showed the agent only the last 6 raw messages. It was the direct cause of
hallucinated and off-topic outgoing messages:

1. **Extraction dropped the seller's side.** The prompt said "extract facts
   about the LEAD only … never extract facts about what [Me] said, offered, or
   asked." Past message 6, the agent had no record of its own promises, offers,
   questions or links — so it re-asked answered questions, re-pitched, and
   invented a shared history that sounded plausible.
2. **A fact list has no order, no time, no speaker.** Unordered bullets, no way
   to tell recent from stale or agreed-to from merely floated.
3. **Errors were permanent.** Both passes were lossy rewrites over rows that
   were never re-read. One bad extraction poisoned the Deal for life, and mem0's
   `UPDATE`/`DELETE` events could rewrite a correct fact into a wrong one.
4. **Silent gaps.** Outgoing-only bursts short-circuited extraction, and a sync
   before the `Deal` row existed skipped it. Only *newly created* rows were ever
   folded in, so history missed once was missed forever.

Reading the full transcript costs a few thousand extra prompt tokens per call
and removes two LLM calls per sync.

## Summaries Pipeline

`Deal.profile_summary` is the only remaining derived summary — a JSON fact list
(`{"facts": [...]}`) built by `linkedin/db/summaries.py`.

`materialize_profile_summary_if_missing(deal, session)`:

1. No-op if `deal.profile_summary` is already populated
2. Re-scrapes the lead's LinkedIn profile via Voyager API
3. Extracts facts via LLM, conditioned on the campaign objective and product docs
4. Persists on `Deal.profile_summary`

Runs **once** per `(lead, campaign)` lifetime — the first time a follow-up
touches the deal. This one earns its keep: the raw Voyager profile blob is tens
of kilobytes of JSON, and it's static background, not dialogue.

`Deal.chat_summary` still exists as a column but is dead — never read, never
written. It was left in place rather than dropped via a destructive migration.

## Conversation Sync

`sync_conversation()` in `linkedin/db/chat.py`:

1. Resolves the conversation URN via `find_conversation_urn()` (API scan) with
   `find_conversation_urn_via_navigation()` fallback
2. Fetches messages via Voyager Messaging GraphQL API
3. Upserts into `ChatMessage` by `linkedin_urn` (dedup key)

No LLM runs on this path. The rows it writes *are* the conversation memory.

## Message Sending

`send_raw_message()` in `linkedin/actions/message.py` tries three strategies in
order, returning `True` on the first success:

| # | Strategy | Method |
|---|----------|--------|
| 1 | **Popup compose** | Open Message popup on profile page, type, send |
| 2 | **Direct thread** | Navigate to `/messaging/thread/new/?recipient=<urn>`, compose, send |
| 3 | **Voyager API** | REST API call via `api/messaging/send.py` |

Each strategy uses the lead's URN (stored on `Lead.urn`). If all three fail,
`handle_follow_up` reverts the Deal to QUALIFIED for re-connection.

## Scheduling & Deduplication

`enqueue_follow_up(campaign_id, public_id, delay_seconds=10)` in
`linkedin/tasks/scheduler.py`:

- Creates a PENDING `Task` with `scheduled_at = now + delay_seconds`
- **Dedup**: only one FOLLOW_UP task per `(campaign_id, public_id)` exists at a
  time — if one already exists and is pending, it's left untouched

Called from three places:

| Caller | When |
|--------|------|
| `handle_connect()` | profile already CONNECTED (skip connection step) |
| `handle_check_pending()` | connection just accepted (PENDING → CONNECTED) |
| `handle_follow_up()` | self-rescheduling after `send_message` or `wait` |

## Rate Limiting

- Daily limit: `LinkedInProfile.follow_up_daily_limit` (default 30)
- Tracked via `ActionLog` with `action_type=FOLLOW_UP`
- When exhausted: task re-enqueued with **1-hour delay**
- Resets daily; cached in-memory via `LinkedInProfile._exhausted` dict

## Failure Handling

| Failure | Recovery |
|---------|----------|
| Send failed (all 3 strategies) | Deal reverted to QUALIFIED for re-connection |
| No Deal found for public_id | Task skipped with warning |
| Rate limit exhausted | Task re-enqueued in 1 hour |
| LLM returns unparseable output | `RuntimeError` raised, daemon stops |
| 401 / `AuthenticationError` | Daemon re-authenticates, resets task to pending |

## Prompt Strategy (Mom Test)

The system prompt (`follow_up_agent.j2`) follows the Mom Test method:

- **Discovery first**: open with questions about the lead's work and problems — no product mention until real signal emerges
- **Pitching on signal**: transition when the lead describes a concrete problem we solve, expresses frustration with their current approach, or asks what we do
- **Keep learning while pitching**: weave discovery questions into the conversation even after introducing the product
- **Language**: infer from profile facts (name origin, location, languages) and from what the lead actually writes; default to English
- **Grounding**: the transcript is the only record — never reference a claim, link, price, name or date that isn't literally in it
- **Tone**: short, casual, warm — like real LinkedIn DMs (1-3 sentences max)
- **No boilerplate**: no placeholders, no signatures, no corporate speak
- **Timing**: agent decides — active reply → 2-8h; async → 24h; no reply → 24-48h; 3+ unanswered → consider `mark_completed`
- **Booking link**: include naturally when suggesting a call, not as a standalone line

## CLI Debugging

The agent can be run standalone for debugging:

```bash
# By profile
.venv/bin/python -m linkedin.agents.follow_up --profile john-doe

# By task ID
.venv/bin/python -m linkedin.agents.follow_up --task-id 42
```

Prints the decision (action, message, reason, follow-up hours) without
executing it.
