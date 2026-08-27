# linkedin/browser/nav.py
import logging
import random
import time
from urllib.parse import unquote, urlparse, urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.conf import BROWSER_NAV_TIMEOUT_MS, DUMP_PAGES, FIXTURE_PAGES_DIR, HUMAN_TYPE_MIN_DELAY_MS, HUMAN_TYPE_MAX_DELAY_MS
from linkedin.exceptions import PageStructureError, SkipProfile

logger = logging.getLogger(__name__)


def goto_page(session,
              action,
              expected_url_pattern: str,
              timeout: int = BROWSER_NAV_TIMEOUT_MS,
              error_message: str = "",
              ):
    page = session.page
    action()
    if not page:
        return

    try:
        page.wait_for_url(lambda url: expected_url_pattern in unquote(url), timeout=timeout)
    except PlaywrightTimeoutError:
        pass  # we still continue and check URL below

    session.wait()

    current = unquote(page.url)
    if expected_url_pattern not in current:
        if "/404" in current:
            raise SkipProfile(f"Profile returned 404 → {current}")
        raise RuntimeError(f"{error_message} → expected '{expected_url_pattern}' | got '{current}'")

    logger.debug("Navigated to %s", page.url)


def extract_in_urls(page):
    """Extract all /in/ profile URLs from the current page."""
    from linkedin.url_utils import url_to_public_id

    seen = set()
    urls = []
    for link in page.locator('a[href*="/in/"]').all():
        href = link.get_attribute("href")
        if href and "/in/" in href:
            full_url = urljoin(page.url, href.strip())
            clean = urlparse(full_url)._replace(query="", fragment="").geturl()
            if not url_to_public_id(clean):
                continue
            if clean not in seen:
                seen.add(clean)
                urls.append(clean)
    logger.debug(f"Extracted {len(urls)} unique /in/ profiles")
    return urls


def find_first_visible(page, selectors: list[str]):
    """Try selectors in order, return first locator that matches."""
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator.first
    return None


def resolve_locator(page, candidates, timeout_per_ms: int = 5000):
    """Try locator factories in order, return the first one that becomes visible."""
    for factory in candidates:
        locator = factory(page).first
        try:
            locator.wait_for(state="visible", timeout=timeout_per_ms)
            return locator
        except PlaywrightTimeoutError:
            continue
    raise RuntimeError(f"No locator matched on {page.url}")


TOP_CARD_SELECTORS = [
    # SDUI (the current variant). Two things about this key are load-bearing.
    # It is *element-agnostic*: the same componentkey rode a <section> in the
    # previous variant and rides a <div> now, and every selector here used to
    # be section-prefixed, which is precisely why this variant matched nothing
    # at all. And it is anchored on the "Topcard" *suffix*, not on the
    # "com.linkedin.sdui.profile.card" prefix, which every profile card shares
    # — About, Featured, Services, SuggestedForYou and the rest, 8 of them on
    # a typical page. Matching the prefix and taking .first would scope the
    # connect/pending lookup to whichever card sorts first in the DOM.
    '[componentkey$="Topcard"]',
    'section:has(div.top-card-background-hero-image)',
    'section[data-member-id]',
    'section.artdeco-card:has(> div.pv-top-card)',
    'section:has(> div[class*="pv-top-card"])',
    'section[componentkey*="com.linkedin.sdui.profile.card"]',
]

# How long the top card gets to appear. Profiles are navigated with
# ``wait_until="domcontentloaded"``, which fires before LinkedIn's SPA has
# rendered anything, so a bare count() is a race the slower account loses.
TOP_CARD_TIMEOUT_MS = 15_000

# Markers that explain a miss. Presence of any of these in the page HTML says
# the scrape hit something other than a normal profile render.
_MISS_MARKERS = {
    "auth_wall": ("authwall", "/uas/login", "join now to see"),
    "challenge": ("checkpoint/challenge", "unusual activity", "security verification"),
    "unavailable": ("this page doesn't exist", "profile unavailable", "member not found"),
    "rate_limited": ("you've reached the", "try again later"),
    "legacy_markup_present": ("pv-top-card",),
    "sdui_markup_present": ("componentkey",),
}


def _describe_top_card_miss(session) -> str:
    """Say *why* the top card is missing, in one greppable line.

    A bare "not found" is unactionable — the three things it can mean (LinkedIn
    served a different markup variant to this member, the session is being
    throttled behind an interstitial, or the page simply hadn't rendered) are
    indistinguishable without looking at what the page actually was.
    """
    page = session.page
    try:
        html = page.content()
        title = page.title()
    except Exception as e:  # page died mid-inspection — report that instead
        return f"page unreadable ({type(e).__name__}: {e})"

    lowered = html.lower()
    hits = [
        name for name, needles in _MISS_MARKERS.items()
        if any(needle in lowered for needle in needles)
    ]
    return (
        f"title={title!r} html={len(html)}B "
        f"markers={','.join(hits) if hits else 'none'}"
    )


def _dump_top_card_miss(session) -> None:
    """Save the unmatched page so the next selector drift is a diff, not a guess.

    Deliberately *not* gated behind DUMP_PAGES: that flag is for bulk fixture
    collection, while this fires only on a failure that is otherwise
    undiagnosable after the fact — the browser has moved on by the time anyone
    reads the log.
    """
    from linkedin.url_utils import url_to_public_id

    dest = FIXTURE_PAGES_DIR / "top-card-miss"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        slug = url_to_public_id(session.page.url) or "unknown"
        account = session.linkedin_profile.linkedin_username
        path = dest / f"{account}_{slug}_{int(time.time())}.html"
        path.write_text(session.page.content(), encoding="utf-8")
        logger.warning("Saved unmatched profile page → %s", path)
    except Exception as e:
        logger.warning("Could not save unmatched profile page: %s", e)


def find_top_card(session):
    """Return the profile top-card locator, waiting for it to render.

    Raises PageStructureError (not SkipProfile) on a miss: a selector that
    doesn't match is a fact about LinkedIn's markup or this session's standing,
    never about the lead, so callers must retry rather than close the deal.
    """
    page = session.page

    # Wait for *any* known variant, then pick by the priority order above —
    # a comma-joined locator resolves in DOM order, which would silently
    # prefer whichever container happens to come first in the document.
    try:
        page.locator(", ".join(TOP_CARD_SELECTORS)).first.wait_for(
            state="attached", timeout=TOP_CARD_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        pass  # fall through to the miss report below

    top_card = find_first_visible(page, TOP_CARD_SELECTORS)
    if top_card is None:
        logger.warning(
            "Top card not found on %s — %s", page.url, _describe_top_card_miss(session),
        )
        _dump_top_card_miss(session)
        raise PageStructureError("Top Card section not found")
    return top_card


def human_type(locator, text: str, min_delay: int = HUMAN_TYPE_MIN_DELAY_MS, max_delay: int = HUMAN_TYPE_MAX_DELAY_MS):
    """Type text with randomized per-keystroke delay to mimic human input."""
    locator.type(text, delay=random.randint(min_delay, max_delay))


def dump_page_html(session: "AccountSession", profile: dict, category: str = "connect"):
    if not DUMP_PAGES:
        return
    dest = FIXTURE_PAGES_DIR / category
    dest.mkdir(parents=True, exist_ok=True)
    filepath = dest / f"{profile.get('public_identifier')}.html"
    html_content = session.page.content()
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info("Saved page snapshot → %s", filepath)


def probe_top_card_selectors(page) -> bool:
    """Report which selectors match on *page*. Returns True if a top card resolved.

    The counterpart to the dump `find_top_card` writes on a miss: point this at
    that file and it says which selector should have fired and what the buttons
    scope to. Also checks the *page-wide* connect match, because the profile
    page carries other members' connect buttons in its "More profiles for you"
    rail — if the scoped and unscoped answers differ, scoping is the only thing
    standing between the daemon and an invitation sent to the wrong person.
    """
    from linkedin.actions.connect import SELECTORS as CONNECT_SELECTORS
    from linkedin.actions.status import SELECTORS as STATUS_SELECTORS

    print("Top card selectors (match count):")
    for selector in TOP_CARD_SELECTORS:
        print(f"  {page.locator(selector).count():4}  {selector}")

    top_card = find_first_visible(page, TOP_CARD_SELECTORS)
    if top_card is None:
        print("\nNo top card matched.")
        return False

    print(f"\nResolved top card: <{top_card.evaluate('e => e.tagName.toLowerCase()')}> "
          f"componentkey={top_card.get_attribute('componentkey')!r}")

    print("\nScoped to the top card:")
    for name, selector in (
        ("pending_button", STATUS_SELECTORS["pending_button"]),
        ("invite_to_connect", CONNECT_SELECTORS["invite_to_connect"]),
        ("more_button", CONNECT_SELECTORS["more_button"]),
    ):
        print(f"  {top_card.locator(selector).count():4}  {name}")

    wide = page.locator(CONNECT_SELECTORS["invite_to_connect"])
    count = wide.count()
    label = wide.first.get_attribute("aria-label") if count else None
    print(f"\nPage-wide invite_to_connect: {count}"
          + (f" — first is {label!r}" if label else ""))
    print("  (a match here that is absent above belongs to someone else —"
          " never widen the scope on a miss)")
    return True


if __name__ == "__main__":
    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Probe the top-card selectors against a page")
    parser.add_argument(
        "--file",
        help="A dumped .html (e.g. from tests/fixtures/pages/top-card-miss/) "
             "to check offline — no LinkedIn session needed",
    )
    parser.add_argument("--profile", help="Public identifier to probe live instead")
    args = parser.parse_args()

    if args.file:
        from pathlib import Path
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            probe_page = browser.new_page()
            probe_page.goto(Path(args.file).resolve().as_uri())
            ok = probe_top_card_selectors(probe_page)
            browser.close()
    elif args.profile:
        from linkedin.actions.search import visit_profile

        probe_session = cli_session(args)
        visit_profile(probe_session, {
            "url": f"https://www.linkedin.com/in/{args.profile}/",
            "public_identifier": args.profile,
        })
        probe_session.wait()
        ok = probe_top_card_selectors(probe_session.page)
    else:
        parser.error("need --file or --profile")

    raise SystemExit(0 if ok else 1)
