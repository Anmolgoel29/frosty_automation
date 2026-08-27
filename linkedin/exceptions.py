class AuthenticationError(Exception):
    """Custom exception for 401 Unauthorized errors."""
    pass


class TerminalStateError(Exception):
    """Profile is already done or dead — caller must skip it"""
    pass


class SkipProfile(Exception):
    """Profile must be skipped — the profile itself is the problem (404, gone).

    Terminal: callers mark the Deal FAILED. Never raise this for a scrape
    that merely failed to parse — see PageStructureError.
    """
    pass


class PageStructureError(Exception):
    """The page loaded but its markup didn't match any known selector.

    Expected/recoverable, and says nothing about the lead: LinkedIn A/B-tests
    profile markup per member, serves a degraded shell to sessions it is
    throttling, and reshuffles its build-hashed classes every deploy — so this
    fires for one account while another sails through the same code. Callers
    must retry/back off, never bury the lead, because the invite the DOM
    failed to describe is still live on LinkedIn.
    """
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

