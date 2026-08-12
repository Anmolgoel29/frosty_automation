# linkedin/schedule.py
"""Per-profile working hours.

Each LinkedInProfile carries its own window (``active_hours_enabled``,
``active_start_hour``, ``active_end_hour``, ``timezone_name``,
``rest_days``), so a client in Madrid and a client in Singapore can both
look like they work office hours while sharing one container.

Used in two places: a worker sleeps until its profile's window opens, and
the dispatcher skips profiles that are outside theirs — no point handing a
prospect to someone who won't wake up for another nine hours.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

logger = logging.getLogger(__name__)


def _profile_zone(linkedin_profile) -> ZoneInfo:
    try:
        return ZoneInfo(linkedin_profile.timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "Unknown timezone %r on %s — falling back to UTC",
            linkedin_profile.timezone_name, linkedin_profile,
        )
        return ZoneInfo("UTC")


def seconds_until_active(linkedin_profile) -> float:
    """Seconds to wait before this profile's next active window, 0 if open now."""
    if not linkedin_profile.active_hours_enabled:
        return 0.0

    rest_days = set(linkedin_profile.rest_days or [])
    start_hour = linkedin_profile.active_start_hour
    end_hour = linkedin_profile.active_end_hour

    tz = _profile_zone(linkedin_profile)
    now = timezone.localtime(timezone=tz)

    if now.weekday() not in rest_days and start_hour <= now.hour < end_hour:
        return 0.0

    # Find the next active start: try today first, then subsequent days.
    candidate = timezone.make_aware(
        now.replace(hour=start_hour, minute=0, second=0, microsecond=0, tzinfo=None),
        timezone=tz,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() in rest_days:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


def is_active_now(linkedin_profile) -> bool:
    """True when this profile is inside its working window."""
    return seconds_until_active(linkedin_profile) == 0.0
