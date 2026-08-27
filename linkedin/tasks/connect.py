# linkedin/tasks/connect.py
"""Connect task — pulls one candidate, connects, self-reschedules."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from django.utils import timezone
from termcolor import colored

from linkedin.conf import CAMPAIGN_CONFIG
from linkedin.db.deals import increment_connect_attempts, set_profile_state
from linkedin.db.leads import disqualify_lead
from linkedin.models import ActionLog
from linkedin.enums import ProfileState
from linkedin.exceptions import (
    ConnectClickFailed, PageStructureError, ProfileInaccessibleError,
    ReachedConnectionLimit, SkipProfile,
)

logger = logging.getLogger(__name__)

MAX_CONNECT_ATTEMPTS = 3


@dataclass
class ConnectStrategy:
    find_candidate: Callable
    pre_connect: Callable | None
    delay: float

    def compute_delay(self, elapsed: float) -> float:
        """Delay until next connect."""
        return self.delay


def strategy_for(campaign):
    """Build the connect strategy for a campaign."""
    from linkedin.pipeline.pools import find_candidate

    return ConnectStrategy(
        find_candidate=find_candidate,
        pre_connect=None,
        delay=CAMPAIGN_CONFIG["connect_delay_seconds"],
    )


def _retry_or_give_up(session, public_id: str, *, why: str) -> None:
    """Track one failed connect attempt; disqualify after MAX_CONNECT_ATTEMPTS.

    Shared by the "no Connect button found" and "button click failed" paths —
    both are attempts on the same lead that didn't produce a sent invite, so
    they share one counter and one give-up threshold.
    """
    attempts = increment_connect_attempts(session, public_id)
    if attempts >= MAX_CONNECT_ATTEMPTS:
        reason = f"Unreachable: {why} after {attempts} attempts"
        disqualify_lead(public_id)
        set_profile_state(session, public_id, ProfileState.FAILED.value, reason=reason)
        logger.warning("Disqualified %s — %s", public_id, reason)
    else:
        set_profile_state(session, public_id, ProfileState.QUALIFIED.value)
        logger.debug("%s: connect attempt %d/%d — %s", public_id, attempts, MAX_CONNECT_ATTEMPTS, why)


def handle_connect(task, session):
    from linkedin.actions.connect import send_connection_request
    from linkedin.actions.status import get_connection_status
    from linkedin.tasks.scheduler import enqueue_connect, seconds_until_tomorrow

    cfg = CAMPAIGN_CONFIG
    campaign = session.campaign
    campaign_id = campaign.pk
    # The LinkedIn account running this task — its own connect loop, its own
    # rate limits, and the owner of whatever lead it reaches out to.
    account = session.linkedin_profile
    strategy = strategy_for(campaign)

    def _reschedule():
        elapsed = (timezone.now() - task.started_at).total_seconds() if task.started_at else 0
        enqueue_connect(campaign_id, account, delay_seconds=strategy.compute_delay(elapsed))

    # --- Rate limit check ---
    if not account.can_execute(ActionLog.ActionType.CONNECT):
        enqueue_connect(campaign_id, account, delay_seconds=seconds_until_tomorrow())
        return

    # --- Get candidate ---
    candidate = strategy.find_candidate(session)
    if candidate is None:
        enqueue_connect(
            campaign_id, account, delay_seconds=cfg["connect_no_candidate_delay_seconds"],
        )
        return

    public_id = candidate["public_identifier"]
    profile = candidate.get("profile") or candidate

    # Ensure we have a Deal before calling set_profile_state
    if strategy.pre_connect:
        strategy.pre_connect(session, public_id)

    from crm.models import Deal

    deal = Deal.objects.filter(
        lead__public_identifier=public_id,
        campaign=session.campaign,
    ).first()
    reason = deal.reason if deal else ""
    stats = f"fit_score={deal.fit_score}" if deal and deal.fit_score is not None else ""
    logger.info(
        "[%s] %s as %s",
        campaign, colored("\u25b6 connect", "cyan", attrs=["bold"]), account.linkedin_username,
    )
    logger.info("[%s] %s (%s) — %s", campaign, public_id, stats, reason or "")

    try:
        status = get_connection_status(session, profile)

        if status in (ProfileState.CONNECTED, ProfileState.PENDING):
            # set_profile_state triggers the scheduler hook, which enqueues
            # follow_up (CONNECTED) or check_pending (PENDING).
            set_profile_state(session, public_id, status.value)
            _reschedule()
            return

        # get_connection_status already navigated to the profile page
        new_state = send_connection_request(session=session, profile=profile)

        if new_state == ProfileState.QUALIFIED:
            _retry_or_give_up(session, public_id, why="no Connect button found")
        else:
            set_profile_state(session, public_id, new_state.value)
            account.record_action(ActionLog.ActionType.CONNECT, session.campaign)

    except ReachedConnectionLimit as e:
        logger.warning("Rate limited: %s", e)
        account.mark_exhausted(ActionLog.ActionType.CONNECT)
        enqueue_connect(campaign_id, account, delay_seconds=seconds_until_tomorrow())
        return
    except ProfileInaccessibleError as e:
        logger.warning("Profile inaccessible — marking FAILED: %s", e)
        set_profile_state(session, public_id, ProfileState.FAILED.value,
                          reason=f"Profile inaccessible: {e}")
    except PageStructureError as e:
        # The page didn't render in a shape we know. That is a browser/session
        # problem, not a verdict on the lead — so it must not touch the
        # connect_attempts counter, which gives up by *disqualifying the Lead
        # campaign-wide*. Leave the deal READY_TO_CONNECT and let the connect
        # loop come back to it; if the cause is permanent for this account we
        # re-pick the same lead at the normal connect pace, which self-heals
        # the moment profile pages render again.
        logger.warning("Could not read %s (%s) — leaving it in the ready pool", public_id, e)
    except SkipProfile as e:
        logger.warning("Skipping %s: %s", public_id, e)
        set_profile_state(session, public_id, ProfileState.FAILED.value)
    except ConnectClickFailed as e:
        logger.warning("Connect click failed for %s: %s", public_id, e)
        _retry_or_give_up(session, public_id, why="connect click failed")

    _reschedule()


