# linkedin/ml/dossier.py
"""The lead dossier the expensive qualification stage reads.

Two halves, deliberately separable so the rendering can be tested without a
browser:

* ``collect_dossier(session, lead)`` — the scrape. Profile, follower count,
  the lead's recent posts, and for **every company the lead currently works
  at** that company's page plus its recent posts.
* ``render_dossier(dossier)`` — turns that into explicitly labelled plain
  text (``Company industry: Software Development``) rather than a prose
  summary. Every value carries the name of the field it came from, so the
  model never has to infer what a bare line of text is.

Nothing here truncates. The expensive stage is meant to see the *whole*
About section, the *whole* experience section and full post bodies; the
cheap prefilter (``ml/qualifier.py:qualify_cheap``) is what keeps that cost
off the majority of leads.

Cost note: one qualification here is several LinkedIn reads — 1 profile +
1 profile-page visit (follower count) + 1 posts + 2 per current company —
where it used to be one. The follower count is the odd one out: it is a
DOM scrape of the rendered profile page rather than a Voyager fetch, so
it costs a full page navigation, not just a request — see
``api/enrichment.py:fetch_follower_count`` for why. ``CAMPAIGN_CONFIG
["dossier_*_delay_seconds"]`` spaces the reads out.
"""
from __future__ import annotations

import logging
import random
import time

from linkedin.conf import CAMPAIGN_CONFIG

logger = logging.getLogger(__name__)

_MONTHS = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


# ── Collection ───────────────────────────────────────────────────────


def _pause() -> None:
    """Jittered gap between the dossier's LinkedIn reads."""
    time.sleep(random.uniform(
        CAMPAIGN_CONFIG["dossier_min_delay_seconds"],
        CAMPAIGN_CONFIG["dossier_max_delay_seconds"],
    ))


def _is_current(position: dict) -> bool:
    """True when the position has no end date.

    A missing ``date_range`` entirely counts as current: LinkedIn omits it
    for roles with hidden dates, and there is nothing in the payload that
    says the role ended.
    """
    date_range = position.get("date_range")
    if not date_range:
        return True
    return not date_range.get("end")


def current_companies(profile: dict) -> list[dict]:
    """Positions the lead is currently active in, one entry per company.

    Deduped on the company page slug, so a lead promoted twice inside one
    company yields a single company to scrape, not three.
    """
    from linkedin.api.enrichment import company_universal_name

    seen: set[str] = set()
    companies = []
    for position in profile.get("positions") or []:
        if not _is_current(position):
            continue
        universal_name = company_universal_name(position.get("company_url"))
        if not universal_name or universal_name in seen:
            continue
        seen.add(universal_name)
        companies.append({
            "universal_name": universal_name,
            "fallback_name": position.get("company_name") or "",
        })
    return companies


def collect_dossier(session, lead) -> dict | None:
    """Scrape everything the expensive qualification stage reads.

    Returns None only when the profile itself is unreachable — that is the
    one piece with no fallback. Every enrichment extra degrades to
    empty/None instead (see api/enrichment.py).
    """
    from linkedin.api.enrichment import (
        fetch_company,
        fetch_company_posts,
        fetch_follower_count,
        fetch_member_posts,
    )
    from linkedin.api.client import PlaywrightLinkedinAPI

    profile = lead.get_profile(session)
    if not profile:
        return None

    api = PlaywrightLinkedinAPI(session=session)
    posts_limit = CAMPAIGN_CONFIG["dossier_posts_per_source"]
    public_id = lead.public_identifier

    _pause()
    follower_count = fetch_follower_count(session, profile)

    _pause()
    posts = fetch_member_posts(api, profile.get("urn") or "", posts_limit)

    companies = []
    for entry in current_companies(profile):
        universal_name = entry["universal_name"]
        _pause()
        company = fetch_company(api, universal_name)
        if company is None:
            # Page unreachable — keep the name so the dossier still says
            # where they work, just without the page detail.
            company = {
                "name": entry["fallback_name"],
                "universal_name": universal_name,
                "description": "",
                "tagline": "",
                "industry": "",
                "staff_count": None,
                "follower_count": None,
            }
        _pause()
        company["recent_posts"] = fetch_company_posts(api, universal_name, posts_limit)
        companies.append(company)

    logger.debug(
        "dossier for %s: %d post(s), %d current compan(y/ies), followers=%s",
        public_id, len(posts), len(companies), follower_count,
    )

    return {
        "profile": profile,
        "follower_count": follower_count,
        "recent_posts": posts,
        "companies": companies,
    }


# ── Rendering ────────────────────────────────────────────────────────


def _format_date(date: dict | None) -> str:
    if not date:
        return ""
    year, month = date.get("year"), date.get("month")
    if year and month and 1 <= month <= 12:
        return f"{_MONTHS[month]} {year}"
    return str(year) if year else ""


def _format_date_range(date_range: dict | None) -> str:
    if not date_range:
        return "not stated"
    start = _format_date(date_range.get("start"))
    end = _format_date(date_range.get("end")) or "Present"
    if not start:
        return end
    return f"{start} - {end}"


def _posts_block(label: str, posts: list[str]) -> list[str]:
    if not posts:
        return [f"{label}: none found"]
    lines = [f"{label} ({len(posts)}):"]
    for i, post in enumerate(posts, 1):
        lines.append(f"--- Post {i} ---")
        lines.append(post)
    return lines


def render_dossier(dossier: dict) -> str:
    """Explicitly-labelled plain text for the expensive qualification prompt."""
    profile = dossier["profile"]
    industry = (profile.get("industry") or {}).get("name") or ""
    follower_count = dossier.get("follower_count")

    lines: list[str] = ["=== LEAD ==="]
    lines.append(f"Lead name: {profile.get('full_name') or ''}")
    lines.append(f"LinkedIn URL: {profile.get('url') or ''}")
    lines.append(f"Lead headline: {profile.get('headline') or ''}")
    lines.append(f"Lead location: {profile.get('location_name') or ''}")
    lines.append(f"Lead industry: {industry}")
    lines.append(
        f"Lead follower count: {follower_count if follower_count is not None else 'unknown'}"
    )

    lines.append("")
    lines.append("=== LEAD ABOUT SECTION ===")
    lines.append(profile.get("summary") or "(empty)")

    lines.append("")
    lines.append("=== RECENT POSTS BY LEAD ===")
    lines.extend(_posts_block("Recent posts by lead", dossier.get("recent_posts") or []))

    lines.append("")
    lines.append("=== LEAD EXPERIENCE (full history) ===")
    positions = profile.get("positions") or []
    if not positions:
        lines.append("(no positions listed)")
    for i, position in enumerate(positions, 1):
        current = "yes" if _is_current(position) else "no"
        lines.append(f"--- Experience {i} ---")
        lines.append(f"Job title: {position.get('title') or ''}")
        lines.append(f"Company name: {position.get('company_name') or ''}")
        lines.append(f"Currently active in this role: {current}")
        lines.append(f"Dates: {_format_date_range(position.get('date_range'))}")
        lines.append(f"Job location: {position.get('location') or ''}")
        lines.append(f"Role description: {position.get('description') or '(none)'}")

    lines.append("")
    lines.append("=== EDUCATION ===")
    educations = profile.get("educations") or []
    if not educations:
        lines.append("(no education listed)")
    for i, education in enumerate(educations, 1):
        lines.append(f"--- Education {i} ---")
        lines.append(f"School name: {education.get('school_name') or ''}")
        lines.append(f"Degree: {education.get('degree_name') or ''}")
        lines.append(f"Field of study: {education.get('field_of_study') or ''}")
        lines.append(f"Dates: {_format_date_range(education.get('date_range'))}")

    companies = dossier.get("companies") or []
    lines.append("")
    lines.append("=== COMPANIES THE LEAD CURRENTLY WORKS AT ===")
    if not companies:
        lines.append("(none identified)")
    for i, company in enumerate(companies, 1):
        staff = company.get("staff_count")
        followers = company.get("follower_count")
        lines.append("")
        lines.append(f"--- Current company {i} ---")
        lines.append(f"Company name: {company.get('name') or ''}")
        lines.append(f"Company LinkedIn page: https://www.linkedin.com/company/{company.get('universal_name') or ''}/")
        lines.append(f"Company industry: {company.get('industry') or 'unknown'}")
        lines.append(f"Company size (employees on LinkedIn): {staff if staff is not None else 'unknown'}")
        lines.append(f"Company follower count: {followers if followers is not None else 'unknown'}")
        lines.append(f"Company tagline: {company.get('tagline') or '(none)'}")
        lines.append("Company about section:")
        lines.append(company.get("description") or "(empty)")
        lines.extend(_posts_block(f"Recent posts by {company.get('name') or 'this company'}",
                                  company.get("recent_posts") or []))

    return "\n".join(lines)


def build_dossier_text(session, lead) -> str | None:
    """Collect + render in one call. None when the profile is unreachable."""
    dossier = collect_dossier(session, lead)
    if dossier is None:
        return None
    return render_dossier(dossier)


class _UnpersistedLead:
    """Stand-in for a Lead row, for probing a profile without touching the DB.

    ``collect_dossier`` only needs ``public_identifier`` and
    ``get_profile(session)`` — the two things a real Lead provides. This
    skips Lead's URN-caching side effect (fine for a one-off probe) and
    creates nothing in the database.
    """

    def __init__(self, public_identifier: str):
        self.public_identifier = public_identifier
        # The unparsed Voyager response, stashed here (not returned) since
        # collect_dossier's contract only wants the parsed profile back.
        self.raw_profile: dict | None = None

    def get_profile(self, session) -> dict | None:
        from linkedin.api.client import PlaywrightLinkedinAPI

        api = PlaywrightLinkedinAPI(session=session)
        profile, raw = api.get_profile(public_identifier=self.public_identifier)
        self.raw_profile = raw
        return profile


# python -m linkedin.ml.dossier --profile <public_id>
if __name__ == "__main__":
    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Scrape a LinkedIn profile and show everything the qualification dossier collects")
    parser.add_argument("--profile", default="me", help="Public identifier of the target profile (default: me)")
    parser.add_argument("--json", action="store_true", help="Also dump the parsed dossier dict as JSON")
    parser.add_argument("--raw", action="store_true", help="Also dump the unparsed Voyager profile response as JSON")
    args = parser.parse_args()

    session = cli_session(args)
    session.ensure_browser()

    lead = _UnpersistedLead(args.profile)
    print(f"Scraping {args.profile} …", flush=True)
    dossier = collect_dossier(session, lead)

    if dossier is None:
        print(f"Could not reach profile {args.profile!r} — private, deleted, or restricted.")
        raise SystemExit(1)

    print()
    print(render_dossier(dossier))

    if args.json:
        import json

        print()
        print("=== PARSED DOSSIER (JSON) ===")
        print(json.dumps(dossier, indent=2, default=str))

    if args.raw:
        import json

        print()
        print("=== RAW VOYAGER PROFILE RESPONSE (JSON) ===")
        print(json.dumps(lead.raw_profile, indent=2, default=str))
