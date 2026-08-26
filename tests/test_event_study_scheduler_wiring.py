"""
Tests for scheduler.py's News-Catalyst Event-Study wiring:
_event_study_enabled() and _record_watch_signals().

These are the same style as tests/test_auto_watchlist_enabled_flag.py's
extracted-helper approach — the actual monitor threads run forever in a
`while True` loop and aren't unit-testable directly, so the config-parsing
and call-through logic is pulled into small standalone functions that are.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_event_study_scheduler_wiring.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import scheduler


class TestEventStudyEnabledFlag:
    def test_missing_key_defaults_to_enabled(self):
        assert scheduler._event_study_enabled({}) is True

    def test_explicit_enabled_true(self):
        assert scheduler._event_study_enabled({"event_study": {"enabled": True}}) is True

    def test_explicit_enabled_false(self):
        assert scheduler._event_study_enabled({"event_study": {"enabled": False}}) is False

    def test_dict_without_enabled_key_defaults_to_enabled(self):
        assert scheduler._event_study_enabled({"event_study": {"max_workers": 5}}) is True

    def test_bare_true(self):
        assert scheduler._event_study_enabled({"event_study": True}) is True

    def test_bare_false(self):
        assert scheduler._event_study_enabled({"event_study": False}) is False


class TestRecordWatchSignalsWiring:
    def test_calls_record_watch_signals_with_configured_max_workers(self):
        cfg = {"event_study": {"enabled": True, "max_workers": 7}}
        results = [{"ticker": "FOO", "price": 10.0}]
        with patch("src.forward_signals.record_watch_signals",
                   return_value={"checked": 1, "recorded": 1, "deduped": 0, "news_found": 0}) as mock_rec:
            scheduler._record_watch_signals(results, "momentum", cfg)
        mock_rec.assert_called_once_with(results, "momentum", max_workers=7)

    def test_disabled_never_calls_record_watch_signals(self):
        cfg = {"event_study": {"enabled": False}}
        results = [{"ticker": "FOO", "price": 10.0}]
        with patch("src.forward_signals.record_watch_signals") as mock_rec:
            scheduler._record_watch_signals(results, "momentum", cfg)
        assert not mock_rec.called

    def test_empty_results_never_calls_record_watch_signals(self):
        cfg = {"event_study": {"enabled": True}}
        with patch("src.forward_signals.record_watch_signals") as mock_rec:
            scheduler._record_watch_signals([], "momentum", cfg)
        assert not mock_rec.called

    def test_exception_is_swallowed_not_raised(self):
        """A failure recording WATCH signals must never take down the monitor
        thread or block the auto_watchlist_agent call that follows it."""
        cfg = {"event_study": {"enabled": True}}
        results = [{"ticker": "FOO", "price": 10.0}]
        with patch("src.forward_signals.record_watch_signals",
                   side_effect=RuntimeError("db locked")):
            scheduler._record_watch_signals(results, "momentum", cfg)  # must not raise

    def test_missing_event_study_key_still_calls_with_default_workers(self):
        cfg = {}
        results = [{"ticker": "FOO", "price": 10.0}]
        with patch("src.forward_signals.record_watch_signals",
                   return_value={"checked": 1, "recorded": 1, "deduped": 0, "news_found": 0}) as mock_rec:
            scheduler._record_watch_signals(results, "supertrend", cfg)
        mock_rec.assert_called_once_with(results, "supertrend", max_workers=10)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
