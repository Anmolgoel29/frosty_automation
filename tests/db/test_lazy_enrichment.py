# tests/db/test_lazy_enrichment.py
"""Tests for Lead lazy accessors (get_profile, get_urn)."""
from __future__ import annotations

from unittest.mock import patch

import pytest


FAKE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer at Acme",
    "positions": [{"company_name": "Acme Corp"}],
    "urn": "urn:li:fsd_profile:ABC123",
}


class TestGetProfile:
    def test_live_scrape_every_call(self, fake_session):
        """`get_profile` is a thin live scrape — no DB caching."""
        from crm.models import Lead

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
        )

        with patch("linkedin.api.client.PlaywrightLinkedinAPI") as MockAPI:
            MockAPI.return_value.get_profile.return_value = (FAKE_PROFILE, {})
            result = lead.get_profile(fake_session)
            lead.get_profile(fake_session)

        # Both calls scraped — no memoization at the Lead level.
        assert MockAPI.return_value.get_profile.call_count == 2
        assert result["first_name"] == "Alice"

    def test_populates_urn_from_scrape(self, fake_session):
        """First successful scrape promotes `urn` onto the Lead row."""
        from crm.models import Lead

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
        )
        assert lead.urn is None

        with patch("linkedin.api.client.PlaywrightLinkedinAPI") as MockAPI:
            MockAPI.return_value.get_profile.return_value = (FAKE_PROFILE, {})
            lead.get_profile(fake_session)

        lead.refresh_from_db()
        assert lead.urn == "urn:li:fsd_profile:ABC123"

    def test_returns_none_on_empty_scrape(self, fake_session):
        """An empty Voyager response surfaces as None (no exception)."""
        from crm.models import Lead

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
        )

        with patch("linkedin.api.client.PlaywrightLinkedinAPI") as MockAPI:
            MockAPI.return_value.get_profile.return_value = (None, {})
            assert lead.get_profile(fake_session) is None

    def test_crashes_on_api_failure(self, fake_session):
        """Lets API errors propagate (get_profile has its own retry)."""
        from crm.models import Lead

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
        )

        with patch("linkedin.api.client.PlaywrightLinkedinAPI") as MockAPI:
            MockAPI.return_value.get_profile.side_effect = IOError("timeout")
            with pytest.raises(IOError):
                lead.get_profile(fake_session)


class TestGetUrn:
    def test_reads_cached_column_without_scraping(self, fake_session):
        """If `urn` is already cached on the row, no scrape happens."""
        from crm.models import Lead

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
            urn="urn:li:fsd_profile:ABC123",
        )

        with patch("linkedin.api.client.PlaywrightLinkedinAPI") as MockAPI:
            assert lead.get_urn(fake_session) == "urn:li:fsd_profile:ABC123"
            MockAPI.assert_not_called()

    def test_scrapes_when_column_is_null(self, fake_session):
        """Missing `urn` triggers a live scrape and caches the result."""
        from crm.models import Lead

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
        )

        with patch("linkedin.api.client.PlaywrightLinkedinAPI") as MockAPI:
            MockAPI.return_value.get_profile.return_value = (FAKE_PROFILE, {})
            assert lead.get_urn(fake_session) == "urn:li:fsd_profile:ABC123"

        lead.refresh_from_db()
        assert lead.urn == "urn:li:fsd_profile:ABC123"
