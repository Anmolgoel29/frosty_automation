# linkedin/locks.py
"""Cross-process locks, backed by Postgres advisory locks.

Workers are separate OS processes now, so a ``threading.Lock`` coordinates
nothing. Postgres advisory locks are the right primitive here: they are
held by a *connection*, so when a worker process dies — cleanly, by
SIGKILL, or because the container went away — the lock is released the
moment its connection drops. Nothing to time out and nothing to clean up.

Two locks:

- ``profile_lock`` — held for a worker's entire life. It is what makes
  "one process per profile" a guarantee rather than a hope: an orphaned
  worker from a previous container still holds it, so a new one refuses to
  start and two browsers never drive the same LinkedIn account.
- ``campaign_pool_lock`` — held while refilling a campaign's shared
  prospect pool, so several profiles working one campaign don't each pay
  the LLM to qualify the same lead.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from django.db import connection

logger = logging.getLogger(__name__)

# Advisory locks are a flat 64-bit space shared by the whole database, so
# every user picks a namespace to avoid colliding with anyone else. These are
# the (classid, objid) pairs used with the two-argument pg_*_advisory_lock.
NS_PROFILE = 0x0A11  # "one worker per LinkedInProfile"
NS_CAMPAIGN_POOL = 0x0A12  # "one pool refill per Campaign"


def _try_lock(namespace: int, key: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [namespace, key])
        return bool(cursor.fetchone()[0])


def _unlock(namespace: int, key: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [namespace, key])


@contextmanager
def campaign_pool_lock(campaign_id: int):
    """Yield True if this process may refill the campaign's pool, else False.

    Never blocks: a worker that can't get the lock has better things to do
    (working its own assignments) than queue up behind another profile's
    refill.
    """
    acquired = _try_lock(NS_CAMPAIGN_POOL, campaign_id)
    try:
        yield acquired
    finally:
        if acquired:
            _unlock(NS_CAMPAIGN_POOL, campaign_id)


def acquire_profile_lock(profile_id: int) -> bool:
    """Claim exclusive ownership of a profile for this process's lifetime.

    Deliberately *not* a context manager: the lock is meant to live as long
    as the connection does, and be released by the process dying. Call it
    once at worker startup and exit if it returns False.
    """
    return _try_lock(NS_PROFILE, profile_id)


def release_profile_lock(profile_id: int) -> None:
    """Give up a profile claim early.

    A worker doesn't need this — exiting releases the lock — but it keeps
    tests from leaking a claim into whatever runs next on the same
    connection.
    """
    _unlock(NS_PROFILE, profile_id)


def profile_lock_is_free(profile_id: int) -> bool:
    """Check whether a profile is unclaimed, without taking the lock.

    Used by the supervisor to tell "the process I spawned is gone" from
    "a process I don't know about still owns this profile" — the second
    happens when a previous supervisor was SIGKILLed and left orphans.
    Racy by nature (the answer can change immediately), so treat it as a
    diagnostic, not a gate; ``acquire_profile_lock`` is the real gate.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT NOT EXISTS (
                SELECT 1 FROM pg_locks
                WHERE locktype = 'advisory'
                  AND classid = %s AND objid = %s AND granted
            )
            """,
            [NS_PROFILE, profile_id],
        )
        return bool(cursor.fetchone()[0])
