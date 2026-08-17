"""
Test: scheduler.py::_auto_watchlist_enabled() — run_scan()'s inline auto-add
path now actually honors "auto_watchlist": {"enabled": false, ...}.

Background (2026-08-17): the inline check only verified the auto_watchlist
config value was a non-empty dict, never its "enabled" key — unlike
auto_watchlist_agent.py::run()'s correct check for the other four discovery
sources (momentum/squeeze/catalyst/supertrend).

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_auto_watchlist_enabled_flag.py -v
"""

from __future__ import annotations

import pytest

import scheduler


class TestAutoWatchlistEnabledFlag:
    def test_explicit_enabled_false_disables(self):
        assert scheduler._auto_watchlist_enabled({"enabled": False, "sources": {}}) is False

    def test_explicit_enabled_true_enables(self):
        assert scheduler._auto_watchlist_enabled({"enabled": True}) is True

    def test_dict_without_enabled_key_defaults_to_enabled(self):
        """Back-compat: existing deployments with an auto_watchlist dict that
        never mentions "enabled" at all must keep working as before."""
        assert scheduler._auto_watchlist_enabled({"sources": {"momentum": {}}}) is True

    def test_bare_true_enables(self):
        assert scheduler._auto_watchlist_enabled(True) is True

    def test_string_true_enables(self):
        assert scheduler._auto_watchlist_enabled("true") is True

    def test_bare_false_disables(self):
        assert scheduler._auto_watchlist_enabled(False) is False

    def test_empty_dict_defaults_to_enabled(self):
        """An empty dict has no explicit "enabled": false, so it defaults to
        enabled — consistent with auto_watchlist_agent.py::run()'s identical
        `aw_cfg.get("enabled", True)` convention. Note this is a deliberate
        correction, not preserved pre-fix behavior: the old truthiness check
        (`isinstance(cfg, dict) and cfg`) happened to treat a bare {} as
        disabled too, but only as a side effect of Python's empty-dict
        falsiness, not because anyone intended {} to mean "disabled"."""
        assert scheduler._auto_watchlist_enabled({}) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
