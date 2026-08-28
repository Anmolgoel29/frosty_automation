# tests/test_search_geo.py
"""Geographic targeting for People search (Campaign.search_geo_urns)."""
from urllib.parse import parse_qs, urlparse

import pytest

from linkedin.models import Campaign

INDIA = "102713980"
US = "103644278"


def _campaign(geo_urns):
    """An unsaved Campaign — geo_urns() is pure, no DB needed."""
    return Campaign(name="test", search_geo_urns=geo_urns)


class TestGeoUrns:
    def test_extracts_a_plain_list_of_ids(self):
        assert _campaign([INDIA, US]).geo_urns() == [INDIA, US]

    def test_accepts_a_full_urn(self):
        """Pasting the whole urn out of a Voyager payload must work."""
        assert _campaign(["urn:li:fsd_geo:102713980"]).geo_urns() == [INDIA]

    def test_accepts_a_bare_int(self):
        """The admin's JSON textarea round-trips numbers, not just strings."""
        assert _campaign([102713980]).geo_urns() == [INDIA]

    def test_skips_malformed_entries_without_dropping_the_rest(self):
        """One typo shouldn't stop the campaign searching on the good ones."""
        assert _campaign(["not-a-number", INDIA]).geo_urns() == [INDIA]

    @pytest.mark.parametrize("empty", [[], None])
    def test_empty_means_no_filter(self, empty):
        """Not configuring targeting is a legitimate choice, and never raises."""
        assert _campaign(empty).geo_urns() == []

    def test_configured_but_all_malformed_raises(self):
        """The dangerous case: targeting is set, but nothing usable parsed.

        Returning [] here would run a *worldwide* search — the exact opposite
        of what the campaign asks for — with only warnings as evidence.
        """
        from linkedin.exceptions import InvalidSearchLocations

        with pytest.raises(InvalidSearchLocations, match="no valid geo id"):
            _campaign(["not-a-number", ""]).geo_urns()

    def test_the_error_names_the_campaign_and_the_bad_value(self):
        """The message has to be enough to fix it in the admin without digging."""
        from linkedin.exceptions import InvalidSearchLocations

        campaign = Campaign(name="Q3 India Push", search_geo_urns=["oops"])
        with pytest.raises(InvalidSearchLocations) as exc:
            campaign.geo_urns()
        assert "Q3 India Push" in str(exc.value)
        assert "oops" in str(exc.value)


class RecordingPage:
    """Captures the URL navigated to, and satisfies goto_page's checks."""

    def __init__(self):
        self.url = ""

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_url(self, matcher, timeout=None):
        pass


class RecordingSession:
    def __init__(self, campaign):
        self.campaign = campaign
        self.page = RecordingPage()

    def wait(self):
        pass

    def ensure_browser(self):
        pass


class TestSearchUrl:
    """Drives the real _initiate_search — the URL is what LinkedIn sees."""

    @staticmethod
    def _query(geo_urns_config):
        from linkedin.actions.search import _initiate_search

        campaign = _campaign(geo_urns_config)
        session = RecordingSession(campaign)
        _initiate_search(session, "Managing Director", geo_urns=campaign.geo_urns())
        return parse_qs(urlparse(session.page.url).query)

    def test_geo_urn_is_a_json_array_of_bare_ids(self):
        assert self._query([INDIA, US])["geoUrn"] == ['["102713980","103644278"]']

    def test_origin_switches_to_faceted_when_filtered(self):
        """LinkedIn's own UI sends FACETED_SEARCH once any filter is on."""
        assert self._query([INDIA])["origin"] == ["FACETED_SEARCH"]

    def test_keyword_still_survives_the_geo_facet(self):
        assert self._query([INDIA])["keywords"] == ["Managing Director"]

    def test_unfiltered_search_is_unchanged(self):
        """No urns configured must behave exactly as an unfiltered search did before."""
        q = self._query([])
        assert "geoUrn" not in q
        assert q["origin"] == ["GLOBAL_SEARCH_HEADER"]

    def test_geo_urns_has_no_default(self):
        """A defaulted param is how a new call site silently searches worldwide.

        Requiring it turns "forgot the geo filter" into a TypeError at the
        call site instead of a search that looks fine in the logs.
        """
        import inspect

        from linkedin.actions.search import _initiate_search

        param = inspect.signature(_initiate_search).parameters["geo_urns"]
        assert param.default is inspect.Parameter.empty

    def test_every_call_site_passes_geo_urns(self):
        """Both real callers must geo-target; only an empty campaign searches worldwide."""
        import ast
        from pathlib import Path

        src = Path("linkedin/actions/search.py").read_text()
        calls = [
            node for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_initiate_search"
        ]
        assert calls, "no _initiate_search call sites found — test is stale"
        for call in calls:
            by_keyword = any(kw.arg == "geo_urns" for kw in call.keywords)
            by_position = len(call.args) >= 3
            assert by_keyword or by_position, f"line {call.lineno} omits geo_urns"
