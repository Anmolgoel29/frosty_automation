# tests/test_pools.py
from unittest.mock import patch

from linkedin.pipeline.pools import (
    find_candidate,
    search_source,
    qualify_source,
    ready_source,
)


class TestSearchSource:
    def test_yields_keywords(self):
        with patch("linkedin.pipeline.pools.run_search", side_effect=["kw1", "kw2", None]):
            results = list(search_source("session"))
        assert results == ["kw1", "kw2"]

    def test_stops_on_none(self):
        with patch("linkedin.pipeline.pools.run_search", return_value=None):
            results = list(search_source("session"))
        assert results == []


class TestQualifySource:
    def test_qualifies_without_search_when_pool_has_candidates(self):
        with (
            patch("linkedin.pipeline.pools.has_qualification_candidates", return_value=True),
            patch("linkedin.pipeline.pools.run_qualification", side_effect=["alice", None]),
            patch("linkedin.pipeline.pools.run_search") as mock_search,
        ):
            results = list(qualify_source("session"))

        assert results == ["alice"]
        mock_search.assert_not_called()

    def test_searches_when_no_candidates(self):
        with (
            patch("linkedin.pipeline.pools.has_qualification_candidates",
                  side_effect=[False, True, True]),
            patch("linkedin.pipeline.pools.run_qualification", side_effect=["alice", None]),
            patch("linkedin.pipeline.pools.run_search", return_value="kw1") as mock_search,
        ):
            results = list(qualify_source("session"))

        assert results == ["alice"]
        mock_search.assert_called_once()

    def test_stops_when_search_exhausted_and_no_candidates(self):
        with (
            patch("linkedin.pipeline.pools.has_qualification_candidates", return_value=False),
            patch("linkedin.pipeline.pools.run_search", return_value=None),
            patch("linkedin.pipeline.pools.run_qualification") as mock_qualify,
        ):
            results = list(qualify_source("session"))

        assert results == []
        mock_qualify.assert_not_called()

    def test_stops_when_search_runs_but_pool_still_empty(self):
        """Search succeeded (didn't exhaust) but produced nothing new for this
        campaign — e.g. every result was a duplicate already in the DB."""
        with (
            patch("linkedin.pipeline.pools.has_qualification_candidates",
                  side_effect=[False, False]),
            patch("linkedin.pipeline.pools.run_search", return_value="kw1") as mock_search,
            patch("linkedin.pipeline.pools.run_qualification") as mock_qualify,
        ):
            results = list(qualify_source("session"))

        assert results == []
        mock_search.assert_called_once()
        mock_qualify.assert_not_called()

    def test_drains_backlog_before_searching_again(self):
        with (
            patch("linkedin.pipeline.pools.has_qualification_candidates",
                  side_effect=[True, True, False, True]),
            patch("linkedin.pipeline.pools.run_qualification", side_effect=["alice", "bob", None]),
            patch("linkedin.pipeline.pools.run_search", return_value="kw1") as mock_search,
        ):
            results = list(qualify_source("session"))

        assert results == ["alice", "bob"]
        mock_search.assert_called_once()


class TestGetCandidate:
    def test_backfills_then_returns(self, fake_session):
        candidate = {"public_identifier": "alice"}

        with (
            patch("linkedin.pipeline.pools.find_ready_candidate", side_effect=[None, candidate]),
            patch("linkedin.pipeline.pools.allocate_ready_deals", return_value=0),
            patch("linkedin.pipeline.pools.promote_to_ready", side_effect=[0, 1]),
            patch("linkedin.pipeline.pools.qualify_source", return_value=iter(["alice"])),
        ):
            assert find_candidate(fake_session) == candidate

    def test_exhausted_returns_none(self, fake_session):
        with (
            patch("linkedin.pipeline.pools.find_ready_candidate", return_value=None),
            patch("linkedin.pipeline.pools.allocate_ready_deals", return_value=0),
            patch("linkedin.pipeline.pools.promote_to_ready", return_value=0),
            patch("linkedin.pipeline.pools.qualify_source", return_value=iter([])),
        ):
            assert find_candidate(fake_session) is None

    def test_allocates_before_promoting(self, fake_session):
        """When this account's ready pool is empty, allocation is tried before
        promotion/qualification — leads already QUALIFIED may just need
        dealing out, not fresh promotion."""
        candidate = {"public_identifier": "alice"}

        with (
            patch("linkedin.pipeline.pools.find_ready_candidate", side_effect=[None, candidate]),
            patch("linkedin.pipeline.pools.allocate_ready_deals", return_value=1) as mock_alloc,
            patch("linkedin.pipeline.pools.promote_to_ready") as mock_promote,
        ):
            assert find_candidate(fake_session) == candidate

        mock_alloc.assert_called_once()
        mock_promote.assert_not_called()


class TestReadySource:
    def test_yields_from_ready_pool(self, fake_session):
        with patch("linkedin.pipeline.pools.find_ready_candidate", side_effect=[{"public_identifier": "a"}, None]), \
             patch("linkedin.pipeline.pools.allocate_ready_deals", return_value=0), \
             patch("linkedin.pipeline.pools.promote_to_ready", return_value=0), \
             patch("linkedin.pipeline.pools.qualify_source", return_value=iter([])):
            results = list(ready_source(fake_session))

        assert results == [{"public_identifier": "a"}]
