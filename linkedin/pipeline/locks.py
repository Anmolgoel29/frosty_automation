# linkedin/pipeline/locks.py
"""Per-campaign serialisation for the parts of the pipeline that aren't
thread-safe.

Accounts run in parallel worker threads, but a campaign has exactly one
shared pool of unlabelled leads. Two accounts qualifying at the same lead at
the same time would both pick it — duplicate LLM spend (cheap *and*
expensive stage), and the second ``Deal`` insert violates
``unique_deal_per_campaign``.

So qualification and fit-score promotion are serialised per campaign.
Search — the slow, browser-bound part the accounts are actually here to
parallelise — stays outside the lock, and keywords are claimed atomically in
the database instead.
"""
from __future__ import annotations

import threading

_LOCKS: dict[int, threading.RLock] = {}
_REGISTRY = threading.Lock()


def campaign_lock(campaign) -> threading.RLock:
    """The lock guarding one campaign's GP model and unlabelled pool."""
    with _REGISTRY:
        return _LOCKS.setdefault(campaign.pk, threading.RLock())
