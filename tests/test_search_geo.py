# tests/test_search_geo.py
"""Geographic targeting for People search (Campaign.search_locations)."""
from urllib.parse import parse_qs, urlparse

import pytest

from linkedin.models import Campaign

INDIA = {"name": "India", "urn": "102713980"}
US = {"name": "United States", "urn": "103644278"}


def _campaign(locations):
    """An unsaved Campaign — geo_urns/geo_labels are pure, no DB needed."""
    return Campaign(name="test", search_locations=locations)


class TestGeoUrns:
    def test_extracts_ids_from_name_urn_pairs(self):
        assert _campaign([INDIA, US]).geo_urns() == ["102713980", "103644278"]

    def test_accepts_a_full_urn(self):
        """Pasting the whole urn out of a Voyager payload must work."""
        entry = {"name": "India", "urn": "urn:li:fsd_geo:102713980"}
        assert _campaign([entry]).geo_urns() == ["102713980"]

    def test_accepts_a_bare_id(self):
        assert _campaign(["102713980"]).geo_urns() == ["102713980"]

    def test_skips_malformed_entries_without_dropping_the_rest(self):
        """One typo shouldn't stop the campaign searching on the good rows."""
        bad = {"name": "Oops", "urn": "not-a-number"}
        assert _campaign([bad, INDIA]).geo_urns() == ["102713980"]

    @pytest.mark.parametrize("empty", [[], None])
    def test_empty_means_no_filter(self, empty):
        assert _campaign(empty).geo_urns() == []

    def test_labels_are_names_not_ids(self):
        assert _campaign([INDIA, US]).geo_labels() == "India, United States"


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
    def _query(locations):
        from linkedin.actions.search import _initiate_search

        campaign = _campaign(locations)
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
        """No locations configured must behave exactly as it did before."""
        q = self._query([])
        assert "geoUrn" not in q
        assert q["origin"] == ["GLOBAL_SEARCH_HEADER"]

    def test_name_lookup_is_never_geo_filtered(self):
        """Finding a specific person must not be narrowed to the campaign's regions."""
        from linkedin.actions.search import _initiate_search

        session = RecordingSession(_campaign([INDIA, US]))
        _initiate_search(session, "Jane Doe")  # no geo_urns passed
        assert "geoUrn" not in parse_qs(urlparse(session.page.url).query)
