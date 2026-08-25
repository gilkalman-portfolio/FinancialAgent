"""
Test: edgar_fcf._FACTS_CACHE is bounded (LRU) instead of growing forever.

Background (2026-08-25): _FACTS_CACHE stored the full raw SEC companyfacts
payload per ticker (verified 1.7-4.9MB as JSON, several times that once
parsed into nested Python dicts/lists) with a 24h "TTL" that only gated
re-fetching -- expired entries were never actually removed from the dict,
so a ticker looked up once and never revisited kept its multi-MB blob in
memory for the entire life of the scheduler process. With ~2,463 distinct
tickers touched per day (main scan, momentum monitor, watchlist scans,
DCF/fundamentals scoring), this was diagnosed as a likely major contributor
to the scheduler.py process reaching 4-5GB of memory within a single day.
Fix: _FACTS_CACHE is now an OrderedDict with LRU touch-on-read and a hard
size cap (_FACTS_CACHE_MAX_SIZE), plus eager eviction of expired entries
on access instead of leaving them to be silently overwritten (or never
touched again). See CLAUDE.md Incident Archive, 2026-08-25.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_edgar_fcf_cache_bound.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import src.edgar_fcf as edgar_fcf


@pytest.fixture(autouse=True)
def _clean_cache():
    """Every test starts and ends with an empty shared module-level cache."""
    edgar_fcf._FACTS_CACHE.clear()
    yield
    edgar_fcf._FACTS_CACHE.clear()


def _mock_facts_response(marker: str):
    resp = MagicMock()
    resp.json.return_value = {"facts": {"us-gaap": {"marker": marker}}}
    resp.raise_for_status.return_value = None
    return resp


class TestCacheSizeBound:
    def test_cache_never_exceeds_max_size(self, monkeypatch):
        monkeypatch.setattr(edgar_fcf, "_FACTS_CACHE_MAX_SIZE", 3)
        with patch.object(edgar_fcf, "_get_cik", return_value="0000000001"), \
             patch("src.edgar_fcf.time.sleep"), \
             patch("src.edgar_fcf.requests.get") as mock_get:
            mock_get.side_effect = lambda *a, **k: _mock_facts_response(a)
            for i in range(10):
                edgar_fcf._fetch_facts(f"TCK{i}")
                assert len(edgar_fcf._FACTS_CACHE) <= 3

        assert len(edgar_fcf._FACTS_CACHE) == 3
        # Only the 3 most-recently-fetched tickers should survive
        assert list(edgar_fcf._FACTS_CACHE.keys()) == ["TCK7", "TCK8", "TCK9"]

    def test_reading_a_cached_ticker_protects_it_from_eviction(self, monkeypatch):
        """A ticker re-read (LRU touch) should survive longer than an
        untouched one, even if the untouched one was fetched more recently."""
        monkeypatch.setattr(edgar_fcf, "_FACTS_CACHE_MAX_SIZE", 2)
        with patch.object(edgar_fcf, "_get_cik", return_value="0000000001"), \
             patch("src.edgar_fcf.time.sleep"), \
             patch("src.edgar_fcf.requests.get") as mock_get:
            mock_get.side_effect = lambda *a, **k: _mock_facts_response(a)

            edgar_fcf._fetch_facts("AAA")   # cache: [AAA]
            edgar_fcf._fetch_facts("BBB")   # cache: [AAA, BBB]
            edgar_fcf._fetch_facts("AAA")   # cache hit, moves AAA to MRU end: [BBB, AAA]
            edgar_fcf._fetch_facts("CCC")   # BBB is now LRU -> evicted: [AAA, CCC]

        assert set(edgar_fcf._FACTS_CACHE.keys()) == {"AAA", "CCC"}
        assert "BBB" not in edgar_fcf._FACTS_CACHE


class TestExpiredEntryEviction:
    def test_expired_entry_is_removed_not_left_dangling(self, monkeypatch):
        """Regression guard: an expired entry must be deleted from the dict
        at read time, not merely bypassed by the TTL check and left to rot
        in memory until (if ever) that same ticker is looked up again."""
        stale_time = datetime.now() - edgar_fcf._FACTS_TTL - timedelta(hours=1)
        edgar_fcf._FACTS_CACHE["OLD"] = (stale_time, {"stale": True})
        assert "OLD" in edgar_fcf._FACTS_CACHE

        with patch.object(edgar_fcf, "_get_cik", return_value="0000000001"), \
             patch("src.edgar_fcf.time.sleep"), \
             patch("src.edgar_fcf.requests.get") as mock_get:
            mock_get.return_value = _mock_facts_response("fresh")
            result = edgar_fcf._fetch_facts("OLD")

        assert result == {"us-gaap": {"marker": "fresh"}}
        assert mock_get.called, "expired entry must trigger a real re-fetch"
        cached_at, facts = edgar_fcf._FACTS_CACHE["OLD"]
        assert cached_at > stale_time

    def test_fresh_entry_is_served_from_cache_without_a_request(self, monkeypatch):
        edgar_fcf._FACTS_CACHE["FRESH"] = (datetime.now(), {"cached": True})
        with patch("src.edgar_fcf.requests.get") as mock_get:
            result = edgar_fcf._fetch_facts("FRESH")
        assert result == {"cached": True}
        assert not mock_get.called
