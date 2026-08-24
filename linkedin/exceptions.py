class AuthenticationError(Exception):
    """Custom exception for 401 Unauthorized errors."""
    pass


class TerminalStateError(Exception):
    """Profile is already done or dead — caller must skip it"""
    pass


class SkipProfile(Exception):
    """Profile must be skipped."""
    pass


class ProfileInaccessibleError(Exception):
    """Profile is private, deleted, or restricted (HTTP 403/404)."""
    pass


class ReachedConnectionLimit(Exception):
    """ Weekly connection limit reached. """
    pass


class ConnectClickFailed(Exception):
    """A connect-flow button click failed (timeout, stale element, UI change).

    Expected/recoverable — the connect button was found but interacting with
    it didn't go through. Distinct from ProfileState.QUALIFIED (no button
    found at all), but routed through the same connect_attempts retry/give-up
    logic in tasks/connect.py.
    """
    pass

