# linkedin/browser/session.py
from __future__ import annotations

import logging
import os
import random
import time
from functools import cached_property

from linkedin.conf import MIN_DELAY, MAX_DELAY

logger = logging.getLogger(__name__)

# The main LinkedIn auth cookie
_AUTH_COOKIE_NAME = "li_at"

# How often to re-read the stored cookie to check for expiry. It expires on a
# scale of days; ensure_browser() runs many times a minute.
COOKIE_CHECK_INTERVAL = 300


def random_sleep(min_val, max_val):
    delay = random.uniform(min_val, max_val)
    logger.debug(f"Pause: {delay:.2f}s")
    time.sleep(delay)


def _lock_file_pid(lock_path: str) -> int | None:
    """PID recorded in an X lock file, or None if it can't be read/parsed."""
    try:
        with open(lock_path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def _ensure_xvfb(display: str) -> None:
    """Start an Xvfb server on *display* unless one is already running.

    Each account gets its own virtual screen so their browser windows never
    overlap in the VNC viewer. The lock file X creates is the liveness check —
    it is what X itself uses to refuse a second server on the same display.

    That lock file only tells the truth across a *clean* X exit. A container
    that gets SIGKILLed (OOM, `docker stop` timeout) or crashes leaves it
    behind with no Xvfb process to match — and since the container's
    writable layer (and therefore /tmp) survives a restart that doesn't
    recreate the container, the stale lock persists into the next run. This
    function then wrongly concludes the display is live, no new Xvfb gets
    spawned, and every consumer of that display fails downstream: x11vnc's
    "XOpenDisplay failed", then Chromium's "Missing X server or $DISPLAY".
    Checking the PID the lock file names is actually alive tells real and
    stale locks apart.
    """
    import subprocess

    number = display.lstrip(":")
    lock_path = f"/tmp/.X{number}-lock"
    pid = _lock_file_pid(lock_path)
    if pid is not None and _process_alive(pid):
        return

    if pid is not None:
        logger.warning(
            "Stale X lock for %s (pid %d is not running — likely an unclean "
            "container restart) — clearing it and starting a fresh Xvfb",
            display, pid,
        )
        for stale in (lock_path, f"/tmp/.X11-unix/X{number}"):
            try:
                os.remove(stale)
            except OSError:
                pass

    logger.info("Starting Xvfb on %s", display)
    subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1920x1080x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # X needs a moment to create its socket; launching Chromium against a
    # display that isn't listening yet fails outright.
    time.sleep(1.5)


class AccountSession:
    def __init__(self, linkedin_profile):
        self.linkedin_profile = linkedin_profile
        self.django_user = linkedin_profile.user

        # Active campaign — set by the daemon before each lane execution
        self.campaign = None

        # Playwright objects – created on first access or after crash.
        # All of them belong to the worker thread that created them: the
        # Playwright sync API is thread-affine, so only this account's worker
        # may touch them (that is why the supervisor asks a worker to shut
        # itself down instead of calling close() across threads).
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

        # The X display this account's browser draws on, once claimed.
        self._display = None

        # Last time the stored auth cookie was checked for expiry.
        self._last_cookie_check = 0.0

    def ensure_display(self) -> str | None:
        """Claim this account's own X display, starting Xvfb if needed.

        Returns None when per-account displays are off (local development on
        a real desktop), in which case the browser uses the ambient DISPLAY.
        """
        from vnc import display_for, per_account_displays_enabled

        if not per_account_displays_enabled():
            logger.info(
                "Per-account displays off — %s uses DISPLAY=%s",
                self, os.environ.get("DISPLAY", "(unset)"),
            )
            return None
        if self._display is None:
            self._display = display_for(self.linkedin_profile.pk)
            _ensure_xvfb(self._display)
            logger.info(
                "%s renders on %s — watch it via Admin → LinkedIn Profile → "
                "\"Watch this account's browser (VNC)\"",
                self, self._display,
            )
        return self._display

    @cached_property
    def campaigns(self):
        """All campaigns this user belongs to (cached)."""
        from linkedin.models import Campaign
        return list(Campaign.objects.filter(users=self.django_user))

    def ensure_browser(self):
        """Launch or recover browser + login if needed. Call before using .page"""
        from linkedin.browser.login import start_browser_session

        if not self.page or self.page.is_closed():
            logger.debug("Launching/recovering browser for %s", self)
            start_browser_session(session=self)
        else:
            self._maybe_refresh_cookies()

    @cached_property
    def self_profile(self) -> dict:
        """Authenticated user's profile dict, fetched once per session.

        The dict isn't persisted to DB (we dropped ``Lead.profile_data``),
        so the first access per session triggers a Voyager call; the
        ``cached_property`` keeps it warm for the rest of the session.
        """
        from linkedin.setup.self_profile import discover_self_profile

        self.ensure_browser()
        return discover_self_profile(self)

    def wait(self, min_delay=MIN_DELAY, max_delay=MAX_DELAY):
        random_sleep(min_delay, max_delay)
        self.page.wait_for_load_state("domcontentloaded")

    def reauthenticate(self):
        """Force a fresh login: close browser, clear saved cookies, re-launch."""
        from linkedin.browser.login import start_browser_session

        logger.warning("Re-authenticating %s — clearing saved session", self)
        self.close()
        self.linkedin_profile.cookie_data = None
        self.linkedin_profile.save(update_fields=["cookie_data"])
        start_browser_session(session=self)

    def _maybe_refresh_cookies(self):
        """Re-login if the li_at auth cookie in the saved DB state is expired.

        Rate-limited to one DB read per ``COOKIE_CHECK_INTERVAL``: this runs on
        every ``ensure_browser()`` — which is called per profile visit, per
        scrape, per message — and each check re-read the whole cookie JSON
        blob. The cookie expires on a scale of days, so polling it every few
        seconds bought nothing.
        """
        from linkedin.browser.login import start_browser_session

        now = time.monotonic()
        if now - self._last_cookie_check < COOKIE_CHECK_INTERVAL:
            return
        self._last_cookie_check = now

        self.linkedin_profile.refresh_from_db(fields=["cookie_data"])
        cookie_data = self.linkedin_profile.cookie_data
        if not cookie_data:
            return
        for cookie in cookie_data.get("cookies", []):
            if cookie.get("name") == _AUTH_COOKIE_NAME:
                expires = cookie.get("expires", -1)
                if expires > 0 and expires < time.time():
                    logger.warning("Auth cookie expired for %s — re-authenticating", self)
                    self.close()
                    start_browser_session(session=self)
                return

    def close(self):
        if self.context:
            try:
                self.context.close()
                if self.browser:
                    self.browser.close()
                if self.playwright:
                    self.playwright.stop()
                logger.info("Browser closed gracefully (%s)", self)
            except Exception as e:
                logger.debug("Error closing browser: %s", e)
            finally:
                self.page = self.context = self.browser = self.playwright = None

        logger.info("Account session closed → %s", self)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return self.linkedin_profile.linkedin_username
