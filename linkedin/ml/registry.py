# linkedin/ml/registry.py
"""Per-campaign qualifiers, one instance per worker process.

Workers are separate processes, so a campaign worked by three profiles has
three qualifier objects in three interpreters. They can't share memory, and
the thing they'd fight over — ``Campaign.model_blob`` — is a single row.

What keeps them consistent is that **the labels, not the model, are the
source of truth**. Every qualification decision is written to the DB as a
Deal (state + outcome), and ``Lead.get_labeled_arrays(campaign)`` rebuilds
the full training set from those rows. So each process warm-starts from the
DB and calls ``refresh_from_labels()`` before it refills a pool — picking up
every label its peers have produced since. Two processes may briefly hold
slightly different models; they re-converge on the next refresh, and the
GP is a candidate-selection heuristic, not a correctness boundary.

Pool refills are serialized across processes by a Postgres advisory lock
(``locks.campaign_pool_lock``) so the LLM is never paid twice for the same
lead.
"""
from __future__ import annotations

import logging
import threading

from linkedin.conf import CAMPAIGN_CONFIG
from linkedin.ml.qualifier import BayesianQualifier

logger = logging.getLogger(__name__)

# Guards the cache below. A worker process is single-threaded for actual
# work, but it also runs a heartbeat thread, so keep the map safe anyway.
_registry_lock = threading.Lock()
_qualifiers: dict[int, BayesianQualifier] = {}


def get_qualifier(campaign) -> BayesianQualifier:
    """This process's qualifier for the campaign, warm-started on first use."""
    with _registry_lock:
        qualifier = _qualifiers.get(campaign.pk)
        if qualifier is not None:
            return qualifier
        qualifier = _build_qualifier(campaign)
        _qualifiers[campaign.pk] = qualifier
        return qualifier


def forget(campaign_id: int) -> None:
    """Drop cached state for a campaign that is gone or no longer worked."""
    with _registry_lock:
        _qualifiers.pop(campaign_id, None)


def _build_qualifier(campaign) -> BayesianQualifier:
    """Construct a qualifier and load the campaign's labels into it."""
    from linkedin.models import Campaign

    # A private Campaign instance: the qualifier writes model_blob through
    # it, and it must not be one the worker loop is also mutating.
    owned_campaign = Campaign.objects.get(pk=campaign.pk)

    qualifier = BayesianQualifier(
        seed=42,
        n_mc_samples=CAMPAIGN_CONFIG["qualification_n_mc_samples"],
        campaign=owned_campaign,
    )
    qualifier.refresh_from_labels(force=True)
    return qualifier
