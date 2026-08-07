"""
Tests for the LLM curation integration point in monitoring_queue.build_queue().

Safety properties under test:
  - Disabled (the default): build_queue() behavior is byte-for-byte identical
    to before this feature existed — the curated-tickers lookup isn't even
    attempted.
  - Enabled: only the SCANNER pool is filtered. Manual watchlist entries and
    recent real-time buy signals are never subject to a week-old judgment
    call — they keep their slot regardless of the curated set.
  - Absent/stale curation data (current_curated_tickers() -> None) or any
    lookup failure must leave the scanner pool untouched, not empty it.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from src import monitoring_queue


def _make_hist(adv_dollars: float = 10_000_000) -> pd.DataFrame:
    close = 10.0
    volume = adv_dollars / close
    return pd.DataFrame({"Close": [close] * 20, "Volume": [volume] * 20})


@pytest.fixture(autouse=True)
def _reset_snapshot():
    monitoring_queue._previous_queue = set()
    yield
    monitoring_queue._previous_queue = set()


def _build(scanner=(), manual=(), recent_buy=(), llm_enabled=False, curated=None):
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = _make_hist()

    with patch.object(monitoring_queue, "_scanner_tickers", return_value=list(scanner)), \
         patch.object(monitoring_queue, "_manual_tickers", return_value=list(manual)), \
         patch.object(monitoring_queue, "_recent_buy_tickers", return_value=list(recent_buy)), \
         patch.object(monitoring_queue.yf, "Ticker", return_value=fake_ticker), \
         patch.object(monitoring_queue, "_llm_curation_enabled", return_value=llm_enabled), \
         patch("src.llm_universe_curator.current_curated_tickers", return_value=curated):
        entries = monitoring_queue.build_queue()
    return {e.ticker for e in entries}


class TestDisabledIsUnaffected:
    def test_disabled_keeps_full_scanner_pool(self):
        result = _build(
            scanner=[("AAA", 90, "t"), ("BBB", 80, "t")],
            llm_enabled=False,
            curated={"AAA"},  # would filter BBB out if it were consulted
        )
        assert result == {"AAA", "BBB"}

    def test_curator_module_never_imported_when_disabled(self):
        """If curation is disabled, the lookup must not even be attempted —
        a broken/missing llm_universe_curator module must not be able to
        affect a deployment that never opted in."""
        with patch.object(monitoring_queue, "_scanner_tickers",
                           return_value=[("AAA", 90, "t")]), \
             patch.object(monitoring_queue, "_manual_tickers", return_value=[]), \
             patch.object(monitoring_queue, "_recent_buy_tickers", return_value=[]), \
             patch.object(monitoring_queue, "_llm_curation_enabled", return_value=False), \
             patch("src.llm_universe_curator.current_curated_tickers") as curated_fn, \
             patch.object(monitoring_queue, "_liquid", return_value=True):
            monitoring_queue.build_queue()
        curated_fn.assert_not_called()


class TestEnabledFiltersScannerOnly:
    def test_scanner_pool_filtered_to_curated_set(self):
        result = _build(
            scanner=[("AAA", 90, "t"), ("BBB", 80, "t"), ("CCC", 70, "t")],
            llm_enabled=True,
            curated={"AAA", "CCC"},
        )
        assert result == {"AAA", "CCC"}

    def test_manual_entries_survive_even_if_not_curated(self):
        """A user's explicit watchlist pick is never second-guessed by the
        weekly curation — it isn't a scanner-sourced entry."""
        result = _build(
            scanner=[("AAA", 90, "t")],
            manual=[("USERPICK", "t")],
            llm_enabled=True,
            curated={"AAA"},  # USERPICK deliberately excluded from curated set
        )
        assert "USERPICK" in result
        assert "AAA" in result

    def test_recent_buy_entries_survive_even_if_not_curated(self):
        """A real-time combined_buy signal is never overridden by a week-old
        qualitative judgment call."""
        result = _build(
            scanner=[("AAA", 90, "t")],
            recent_buy=[("JUSTBOUGHT", "t")],
            llm_enabled=True,
            curated={"AAA"},
        )
        assert "JUSTBOUGHT" in result


class TestAbsentOrFailedCurationLeavesPoolIntact:
    def test_none_curated_set_leaves_scanner_pool_untouched(self):
        """current_curated_tickers() returning None (empty/stale table) must
        not be read as 'curate down to nothing'."""
        result = _build(
            scanner=[("AAA", 90, "t"), ("BBB", 80, "t")],
            llm_enabled=True,
            curated=None,
        )
        assert result == {"AAA", "BBB"}

    def test_lookup_exception_leaves_scanner_pool_untouched(self):
        fake_ticker = MagicMock()
        fake_ticker.history.return_value = _make_hist()
        with patch.object(monitoring_queue, "_scanner_tickers",
                           return_value=[("AAA", 90, "t"), ("BBB", 80, "t")]), \
             patch.object(monitoring_queue, "_manual_tickers", return_value=[]), \
             patch.object(monitoring_queue, "_recent_buy_tickers", return_value=[]), \
             patch.object(monitoring_queue.yf, "Ticker", return_value=fake_ticker), \
             patch.object(monitoring_queue, "_llm_curation_enabled", return_value=True), \
             patch("src.llm_universe_curator.current_curated_tickers",
                   side_effect=RuntimeError("db locked")):
            entries = monitoring_queue.build_queue()
        assert {e.ticker for e in entries} == {"AAA", "BBB"}


class TestConfigFlagReader:
    def test_missing_config_file_defaults_false(self, tmp_path):
        with patch.object(monitoring_queue, "_CONFIG_PATH", tmp_path / "does_not_exist.json"):
            assert monitoring_queue._llm_curation_enabled() is False

    def test_malformed_json_defaults_false(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        with patch.object(monitoring_queue, "_CONFIG_PATH", bad):
            assert monitoring_queue._llm_curation_enabled() is False

    def test_missing_key_defaults_false(self, tmp_path):
        cfg = tmp_path / "cfg.json"
        cfg.write_text("{}")
        with patch.object(monitoring_queue, "_CONFIG_PATH", cfg):
            assert monitoring_queue._llm_curation_enabled() is False

    def test_explicit_true_is_respected(self, tmp_path):
        cfg = tmp_path / "cfg.json"
        cfg.write_text('{"llm_universe_curation_enabled": true}')
        with patch.object(monitoring_queue, "_CONFIG_PATH", cfg):
            assert monitoring_queue._llm_curation_enabled() is True
