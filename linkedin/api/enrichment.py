# linkedin/api/enrichment.py
"""Extra reads that back the qualification dossier.

``client.py:get_profile`` covers the profile itself (headline, About,
experience). This module adds the four things the expensive qualification
stage also wants and the profile decoration does not carry: the member's
follower count, their recent posts, the pages of the companies they
currently work at, and those companies' recent posts.

Everything except follower count is a Voyager REST read. Follower count is
a DOM scrape of the profile page instead: LinkedIn retired the old
``networkinfo`` REST endpoint (HTTP 410) and never put the field on the
main profile decoration either — the count now only exists in the
rendered page (or behind an unversioned GraphQL query id not worth
reverse-engineering for one optional number). ``fetch_follower_count``
therefore takes a ``session``, not an ``api`` client, and navigates the
browser instead of issuing a fetch. It is matched by its own text content
(a `<number> followers` pattern), not a link or CSS class — LinkedIn used
to render it as an anchor to `/people-follow/followers`, but it is now
plain, non-interactive text, and the classes around it are build-hashed
and reshuffle every deploy.

**Fail-soft by design.** Every fetcher here returns ``None``/``[]`` rather
than raising when LinkedIn says no. These are enrichment extras: a company
page that is private, a member with posting disabled, or an endpoint or
selector LinkedIn has quietly moved should degrade the dossier, not kill
the qualification of an otherwise good lead. ``AuthenticationError`` is the
one exception — a 401 means the whole session is dead, which the daemon
handles by re-authenticating, so it is always re-raised.

The REST endpoints here are LinkedIn-internal and unversioned; they move
without notice, same as the DOM the follower-count scrape depends on. Each
has a ``__main__`` probe below (``python -m linkedin.api.enrichment
--help``) so they can be checked against a live session when something
starts coming back empty.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import quote

from linkedin.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

_BASE = "https://www.linkedin.com/voyager/api"

# Decoration for the full company page — carries description, staff count,
# follower count and industries in one read.
_COMPANY_DECORATION = "com.linkedin.voyager.deco.organization.web.WebFullCompanyMain-12"


# ── Shared helpers ───────────────────────────────────────────────────


def _get_json(api, url: str, *, what: str) -> Optional[dict]:
    """GET *url* and decode JSON, or return None with a warning.

    Swallows everything except ``AuthenticationError`` — see the module
    docstring on why enrichment failures must not abort qualification.
    """
    try:
        res = api.get(url)
        if res.status == 401:
            raise AuthenticationError(f"LinkedIn API returned 401 fetching {what}.")
        if not res.ok:
            logger.warning("%s → HTTP %s, skipping", what, res.status)
            return None
        return res.json()
    except AuthenticationError:
        raise
    except Exception as e:
        logger.warning("%s failed (%s: %s), skipping", what, type(e).__name__, e)
        return None


def company_universal_name(company_url: str | None) -> Optional[str]:
    """Slug LinkedIn keys the organization API by, from a company page URL.

    ``https://www.linkedin.com/company/google/`` → ``google``. Schools use
    ``/school/<slug>/`` and resolve through the same organization endpoint,
    so both prefixes are accepted.
    """
    if not company_url:
        return None
    for marker in ("/company/", "/school/", "/showcase/"):
        if marker in company_url:
            slug = company_url.split(marker, 1)[1]
            return slug.strip("/").split("/")[0].split("?")[0] or None
    return None


def _walk_post_texts(node: Any, found: list[str]) -> None:
    """Collect post body text from an arbitrarily-shaped feed response.

    Feed payloads nest the author's text under ``commentary.text.text`` but
    the wrapper around it differs per surface (member shares, company feed,
    reshares) and changes over time. Walking for the stable inner shape
    survives those wrapper changes; matching an exact path does not.
    """
    if isinstance(node, dict):
        commentary = node.get("commentary")
        if isinstance(commentary, dict):
            text_node = commentary.get("text")
            if isinstance(text_node, dict):
                text = text_node.get("text")
            else:
                text = text_node if isinstance(text_node, str) else None
            if isinstance(text, str) and text.strip():
                found.append(text.strip())
        for key, value in node.items():
            if key != "commentary":
                _walk_post_texts(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_post_texts(item, found)


def _post_texts(payload: Optional[dict], limit: int) -> list[str]:
    """Up to *limit* post bodies from a feed response, newest first, deduped."""
    if not payload:
        return []
    found: list[str] = []
    _walk_post_texts(payload, found)

    seen: set[str] = set()
    unique: list[str] = []
    for text in found:
        if text in seen:
            continue
        seen.add(text)
        unique.append(text)
        if len(unique) >= limit:
            break
    return unique


# ── Member ───────────────────────────────────────────────────────────


# LinkedIn no longer renders this as a link at all — it's plain text (e.g.
# `<p>2,614 followers</p>`) inside build-hashed, unstable class names. The
# text pattern itself is the only stable thing left to match on. Anchored so
# it doesn't also match the unrelated "Follow <name>" button labels.
_FOLLOWERS_TEXT_RE = re.compile(r"^[\d,]+\+?\s+followers?$", re.IGNORECASE)


def _goto_profile_page(session, profile: dict) -> None:
    """Navigate to *profile*'s page, deliberately without
    ``actions/search.py:visit_profile``'s side effect of discovering and
    enriching every other profile linked from the page.

    ``collect_dossier`` runs under ``pipeline/locks.py:campaign_lock`` —
    that lock's own docstring is explicit that slow browser-bound work must
    stay outside it, precisely so one account's qualification can't stall
    every other account in the campaign. ``visit_profile``'s discovery pass
    is exactly that: a paced, potentially multi-profile Voyager crawl. Using
    it here would run that crawl on every expensive-stage qualification
    while holding the campaign-wide lock.
    """
    from linkedin.browser.nav import goto_page

    session.ensure_browser()
    public_identifier = profile.get("public_identifier")
    if f"/in/{public_identifier}" in session.page.url:
        return
    goto_page(
        session,
        action=lambda: session.page.goto(profile.get("url"), wait_until="domcontentloaded"),
        expected_url_pattern=f"/in/{public_identifier}",
        error_message="Failed to navigate to profile for follower count",
    )


def fetch_follower_count(session, profile: dict) -> Optional[int]:
    """The member's follower count, scraped off the profile page.

    Reads the top-card's "X followers" text — matched by its own text
    content (``_FOLLOWERS_TEXT_RE``), not by CSS class or href. LinkedIn
    used to render this as a link to `/people-follow/followers`; it now
    renders as a plain, non-interactive `<p>`, so href-based matching no
    longer finds anything. The count is duplicated verbatim elsewhere on
    the page (near the Activity section) — ``.first`` picks up the top-card
    occurrence, but either would return the same number. Fail-soft like
    everything else here: missing text, unparsable text, or a navigation
    error all just return None instead of raising.
    """
    public_identifier = profile.get("public_identifier")
    try:
        _goto_profile_page(session, profile)
        session.wait()
        locator = session.page.get_by_text(_FOLLOWERS_TEXT_RE)
        if locator.count() == 0:
            logger.warning("followers text not found for %s, skipping", public_identifier)
            return None
        text = locator.first.inner_text()
    except AuthenticationError:
        raise
    except Exception as e:
        logger.warning(
            "follower count for %s failed (%s: %s), skipping",
            public_identifier, type(e).__name__, e,
        )
        return None

    match = re.search(r"[\d,]+", text)
    if not match:
        logger.warning("could not parse follower count %r for %s", text, public_identifier)
        return None
    return int(match.group(0).replace(",", ""))


def fetch_member_posts(api, profile_urn: str, limit: int = 3) -> list[str]:
    """The member's most recent post bodies (up to *limit*)."""
    if not profile_urn:
        return []
    url = (
        f"{_BASE}/identity/profileUpdatesV2"
        f"?includeLongTermHistory=true&moduleKey=member-shares%3Aphone"
        f"&numComments=0&numLikes=0&q=memberShareFeed"
        f"&profileUrn={quote(profile_urn, safe='')}&count={limit}"
    )
    data = _get_json(api, url, what=f"posts for {profile_urn}")
    return _post_texts(data, limit)


# ── Company ──────────────────────────────────────────────────────────


def fetch_company(api, universal_name: str) -> Optional[dict]:
    """Company page facts: description, industry, staff and follower counts.

    Returns a flat dict (``name``, ``universal_name``, ``description``,
    ``tagline``, ``industry``, ``staff_count``, ``follower_count``) or None.
    """
    url = (
        f"{_BASE}/organization/companies"
        f"?decorationId={_COMPANY_DECORATION}"
        f"&q=universalName&universalName={quote(universal_name)}"
    )
    data = _get_json(api, url, what=f"company page {universal_name}")
    if not data:
        return None

    elements = data.get("elements") or []
    if not elements:
        logger.warning("company page %s returned no elements, skipping", universal_name)
        return None
    company = elements[0]

    industries = company.get("companyIndustries") or []
    industry = ""
    if industries and isinstance(industries[0], dict):
        industry = industries[0].get("localizedName") or ""

    following_info = company.get("followingInfo") or {}
    follower_count = following_info.get("followerCount")

    return {
        "name": company.get("name") or "",
        "universal_name": universal_name,
        "description": company.get("description") or "",
        "tagline": company.get("tagline") or "",
        "industry": industry,
        "staff_count": company.get("staffCount"),
        "follower_count": follower_count if isinstance(follower_count, int) else None,
    }


def fetch_company_posts(api, universal_name: str, limit: int = 3) -> list[str]:
    """The company page's most recent post bodies (up to *limit*)."""
    url = (
        f"{_BASE}/feed/updates"
        f"?companyUniversalName={quote(universal_name)}"
        f"&q=companyFeedByUniversalName&count={limit}"
    )
    data = _get_json(api, url, what=f"company posts for {universal_name}")
    return _post_texts(data, limit)


def _debug_dump_follower_html(session, context_chars: int = 200) -> None:
    """Print HTML snippets around every case-insensitive "follow" occurrence.

    Diagnostic aid for when ``_FOLLOWERS_LINK_SELECTOR`` stops matching —
    LinkedIn's DOM/hrefs move without notice (see module docstring). Dumps
    from the already-navigated page, so call this right after
    ``fetch_follower_count`` returns None.
    """
    html = session.page.content()
    matches = list(re.finditer(r"follow", html, re.IGNORECASE))
    print(f"=== {len(matches)} 'follow' occurrence(s) in page HTML ===")
    seen_snippets: set[str] = set()
    for m in matches:
        start = max(0, m.start() - context_chars)
        end = min(len(html), m.end() + context_chars)
        snippet = html[start:end]
        if snippet in seen_snippets:
            continue
        seen_snippets.add(snippet)
        print("---")
        print(snippet)


if __name__ == "__main__":
    import json

    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Probe the qualification-dossier's Voyager endpoints and the follower-count DOM scrape")
    parser.add_argument("--profile", default=None, help="public identifier to read")
    parser.add_argument("--company", default=None, help="company universalName to read")
    parser.add_argument("--raw", action="store_true", help="Also dump each Voyager endpoint's unparsed JSON response")
    parser.add_argument(
        "--debug-followers", action="store_true",
        help="Dump HTML snippets around every 'follow' occurrence on the profile page "
             "(always, not just on a miss) to diagnose selector drift",
    )
    args = parser.parse_args()

    session = cli_session(args)
    session.ensure_browser()
    probe = PlaywrightLinkedinAPI(session=session)

    if args.profile:
        parsed, _raw = probe.get_profile(public_identifier=args.profile)
        count = fetch_follower_count(session, parsed)
        print(f"followers: {count}")
        if args.debug_followers or count is None:
            _debug_dump_follower_html(session)
        print(json.dumps(fetch_member_posts(probe, parsed["urn"]), indent=2))
    if args.company:
        if args.raw:
            url = (
                f"{_BASE}/organization/companies"
                f"?decorationId={_COMPANY_DECORATION}"
                f"&q=universalName&universalName={quote(args.company)}"
            )
            print("=== RAW company response ===")
            print(json.dumps(_get_json(probe, url, what="company (raw probe)"), indent=2))
        print(json.dumps(fetch_company(probe, args.company), indent=2))
        print(json.dumps(fetch_company_posts(probe, args.company), indent=2))
