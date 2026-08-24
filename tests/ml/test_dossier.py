# tests/ml/test_dossier.py
"""Tests for the qualification dossier — company selection and rendering.

``collect_dossier`` itself is a thin sequence of network calls; what is
worth pinning is which companies it decides to scrape and that the rendered
text actually carries every field labelled, since that is the whole
contract with the expensive qualification prompt.
"""
from __future__ import annotations

from linkedin.api.enrichment import company_universal_name
from linkedin.ml.dossier import current_companies, render_dossier


def _position(title, company, url, *, end=None, description="", location=""):
    date_range = {"start": {"year": 2020, "month": 1}, "end": end}
    return {
        "title": title,
        "company_name": company,
        "company_url": url,
        "date_range": date_range,
        "description": description,
        "location": location,
    }


class TestCompanyUniversalName:
    def test_company_url(self):
        assert company_universal_name("https://www.linkedin.com/company/google/") == "google"

    def test_school_url(self):
        assert company_universal_name("https://www.linkedin.com/school/mit/") == "mit"

    def test_trailing_query_string(self):
        assert company_universal_name("https://www.linkedin.com/company/acme/?trk=x") == "acme"

    def test_none_and_unrecognised(self):
        assert company_universal_name(None) is None
        assert company_universal_name("https://example.com/foo/") is None


class TestCurrentCompanies:
    def test_only_roles_without_an_end_date(self):
        profile = {"positions": [
            _position("Founder", "Acme", "https://www.linkedin.com/company/acme/"),
            _position("Intern", "OldCo", "https://www.linkedin.com/company/oldco/",
                      end={"year": 2019, "month": 6}),
        ]}
        assert [c["universal_name"] for c in current_companies(profile)] == ["acme"]

    def test_multiple_current_companies_all_returned(self):
        """A lead active at several companies gets every one scraped."""
        profile = {"positions": [
            _position("Founder", "Acme", "https://www.linkedin.com/company/acme/"),
            _position("Advisor", "Beta", "https://www.linkedin.com/company/beta/"),
        ]}
        assert [c["universal_name"] for c in current_companies(profile)] == ["acme", "beta"]

    def test_same_company_twice_is_deduped(self):
        """Two current roles at one employer (a promotion) is one scrape."""
        profile = {"positions": [
            _position("VP Eng", "Acme", "https://www.linkedin.com/company/acme/"),
            _position("Director", "Acme", "https://www.linkedin.com/company/acme/"),
        ]}
        assert [c["universal_name"] for c in current_companies(profile)] == ["acme"]

    def test_missing_date_range_counts_as_current(self):
        """LinkedIn omits dates for some current roles; nothing says it ended."""
        profile = {"positions": [
            {"title": "Founder", "company_name": "Acme",
             "company_url": "https://www.linkedin.com/company/acme/", "date_range": None},
        ]}
        assert [c["universal_name"] for c in current_companies(profile)] == ["acme"]

    def test_position_without_company_url_is_skipped(self):
        profile = {"positions": [_position("Founder", "Acme", None)]}
        assert current_companies(profile) == []


def _dossier():
    return {
        "profile": {
            "full_name": "Anmol Gupta",
            "url": "https://www.linkedin.com/in/anmol/",
            "headline": "Founder at SolveEase",
            "location_name": "Bengaluru, India",
            "industry": {"name": "Software Development"},
            "summary": "I build outreach automation.",
            "positions": [
                _position("Founder", "SolveEase", "https://www.linkedin.com/company/solveease/",
                          description="Running the company.", location="Bengaluru"),
                _position("Engineer", "OldCo", "https://www.linkedin.com/company/oldco/",
                          end={"year": 2021, "month": 3}, description="Wrote code."),
            ],
            "educations": [
                {"school_name": "IIT", "degree_name": "BTech",
                 "field_of_study": "CS", "date_range": None},
            ],
        },
        "follower_count": 4213,
        "recent_posts": ["Shipped a new feature today.", "Hiring engineers."],
        "companies": [{
            "name": "SolveEase",
            "universal_name": "solveease",
            "description": "We automate B2B outreach.",
            "tagline": "Outreach on autopilot",
            "industry": "Software Development",
            "staff_count": 12,
            "follower_count": 843,
            "recent_posts": ["We raised a seed round."],
        }],
    }


class TestRenderDossier:
    def test_every_field_is_labelled(self):
        text = render_dossier(_dossier())

        assert "Lead name: Anmol Gupta" in text
        assert "Lead headline: Founder at SolveEase" in text
        assert "Lead location: Bengaluru, India" in text
        assert "Lead industry: Software Development" in text
        assert "Lead follower count: 4213" in text
        assert "Company name: SolveEase" in text
        assert "Company industry: Software Development" in text
        assert "Company size (employees on LinkedIn): 12" in text
        assert "Company follower count: 843" in text

    def test_full_text_is_not_truncated(self):
        """The expensive stage is meant to see whole sections, not summaries."""
        long_about = "word " * 2000
        dossier = _dossier()
        dossier["profile"]["summary"] = long_about

        text = render_dossier(dossier)

        assert long_about in text
        assert "We automate B2B outreach." in text
        assert "Running the company." in text

    def test_includes_lead_and_company_posts(self):
        text = render_dossier(_dossier())

        assert "Shipped a new feature today." in text
        assert "Hiring engineers." in text
        assert "We raised a seed round." in text

    def test_experience_marks_which_roles_are_current(self):
        text = render_dossier(_dossier())
        current_block, past_block = text.split("--- Experience 2 ---")

        assert "Currently active in this role: yes" in current_block
        assert "Currently active in this role: no" in past_block
        # Past roles are still included — full history, not just current.
        assert "OldCo" in past_block

    def test_missing_values_are_marked_not_omitted(self):
        """A blank field must read as missing, never as a negative signal."""
        dossier = _dossier()
        dossier["follower_count"] = None
        dossier["recent_posts"] = []
        dossier["companies"][0]["staff_count"] = None
        dossier["companies"][0]["description"] = ""

        text = render_dossier(dossier)

        assert "Lead follower count: unknown" in text
        assert "Recent posts by lead: none found" in text
        assert "Company size (employees on LinkedIn): unknown" in text
        assert "(empty)" in text

    def test_renders_with_no_current_companies(self):
        dossier = _dossier()
        dossier["companies"] = []

        text = render_dossier(dossier)

        assert "=== COMPANIES THE LEAD CURRENTLY WORKS AT ===" in text
        assert "(none identified)" in text

    def test_date_range_formatting(self):
        text = render_dossier(_dossier())

        assert "Dates: January 2020 - Present" in text
        assert "Dates: January 2020 - March 2021" in text
