"""
Test: auto_watchlist_agent.py::_check_liquidity now fails CLOSED when
dollar-volume data is unavailable, matching monitoring_queue.py::_liquid()
(the equivalent gate for real IBKR monitoring, which already failed closed).

Background (2026-08-17): `if not dv: return True` let a thinly-traded
candidate with missing volume data sail past the liquidity gate by default.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_liquidity_fail_closed.py -v
"""

from __future__ import annotations

import pytest

from src.auto_watchlist_agent import _check_liquidity


class TestMissingDataFailsClosed:
    def test_no_dollar_volume_and_no_price_fails_closed(self):
        r = {"ticker": "FAKE"}  # no avg_dollar_volume, no price, no avg_volume
        assert _check_liquidity(r, src_cfg={}, already_in=False) is False

    def test_no_price_means_dv_cannot_be_derived(self):
        r = {"ticker": "FAKE", "avg_volume": 1_000_000}  # price missing → dv=0
        assert _check_liquidity(r, src_cfg={}, already_in=False) is False


class TestRealDataStillEvaluatedNormally:
    def test_high_liquidity_passes(self):
        r = {"ticker": "FAKE", "avg_dollar_volume": 10_000_000}
        assert _check_liquidity(r, src_cfg={}, already_in=False) is True

    def test_low_liquidity_new_candidate_fails(self):
        r = {"ticker": "FAKE", "avg_dollar_volume": 1_000_000}  # below LIQUIDITY_ADV_ENTRY (5M)
        assert _check_liquidity(r, src_cfg={}, already_in=False) is False

    def test_low_liquidity_already_in_survives_hysteresis_band(self):
        # Between LIQUIDITY_ADV_EXIT (3M) and LIQUIDITY_ADV_ENTRY (5M): an
        # already-tracked ticker should stay in (hysteresis), a fresh one
        # should not get in.
        r = {"ticker": "FAKE", "avg_dollar_volume": 4_000_000}
        assert _check_liquidity(r, src_cfg={}, already_in=True) is True
        assert _check_liquidity(r, src_cfg={}, already_in=False) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
