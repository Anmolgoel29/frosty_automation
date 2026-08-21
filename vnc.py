"""Per-account X displays behind a single VNC port.

Each LinkedIn account's browser renders on its own virtual X display, so the
accounts never draw over each other. Only one ``x11vnc`` runs, and it is
pointed at whichever display is *selected*; the selection lives in a small
control file that `compose/linkedin/start` watches. Switching accounts in the
admin panel rewrites that file, the watcher restarts x11vnc against the new
display, and the noVNC tab reconnects on its own.

One port, any number of accounts — the alternative (a VNC server per account)
would need its ports published before the container starts, which defeats
adding accounts to a running daemon.

Pure stdlib, imported by both stacks (Django daemon and FastAPI admin) and
read by the shell start script — same rationale as ``db_url.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

# Written by the admin panel, polled by compose/linkedin/start. Lives in /tmp
# because it is per-container runtime state, not data worth persisting.
DISPLAY_CONTROL_FILE = Path(
    os.environ.get("OPENOUTREACH_VNC_CONTROL_FILE", "/tmp/openoutreach-vnc-display")
)

# Per-account displays start above the default :99 the start script creates,
# so a stale :99 viewer never silently shows the wrong account.
DISPLAY_BASE = 100


def per_account_displays_enabled() -> bool:
    """True when each account should get its own Xvfb display.

    Off by default so local development still uses the developer's real X
    server; the compose files switch it on.
    """
    return os.environ.get("OPENOUTREACH_PER_ACCOUNT_DISPLAY", "").lower() in (
        "1", "true", "yes",
    )


def display_for(profile_id: int) -> str:
    """The X display owned by one LinkedIn account, e.g. ``":101"``.

    Derived from the primary key rather than allocated, so it is stable across
    restarts and needs no bookkeeping — the admin panel can compute the same
    value without talking to the daemon.
    """
    return f":{DISPLAY_BASE + int(profile_id)}"


def select_display(display: str) -> None:
    """Point the single VNC server at *display*."""
    DISPLAY_CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    DISPLAY_CONTROL_FILE.write_text(display.strip() + "\n", encoding="utf-8")


def selected_display() -> str | None:
    """The display the VNC server should be showing, if one was chosen."""
    try:
        value = DISPLAY_CONTROL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None
