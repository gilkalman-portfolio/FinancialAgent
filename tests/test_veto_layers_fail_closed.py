"""
Test: 3 additional execution_engine.py veto layers now fail CLOSED on a DB/
data-access error, matching the fix already applied to check_daily_loss_limit.

Background (2026-08-17): Layer -1 (SELL requires an open position — the
belt-and-braces guard against an unintended short), Layer -1.5 (BUY veto if
already long — no pyramiding), and Layer -1.2 (BUY veto at max positions)
all caught their own exceptions with only a logger.warning and let the trade
proceed. A transient DB read failure on any of them silently removed a real
safety check for that one evaluation. All three now veto explicitly instead.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_veto_layers_fail_closed.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.execution_engine as engine


def _score_data(price=50.0):
    return {"price": price, "score": 70, "rsi": 55, "macd_signal": "bullish"}


class TestLayerMinus1SellFailsClosed:
    def test_position_check_error_vetoes_sell(self):
        mock_tracker = MagicMock()
        mock_tracker.get_current_exposure.side_effect = RuntimeError("DB locked")
        engine.set_position_tracker(mock_tracker)
        try:
            reasons = []
            decision = engine.evaluate_trade(
                "FAKE", _score_data(), signal_type="SELL", reasons_out=reasons
            )
        finally:
            engine.set_position_tracker(None)

        assert decision is None
        assert reasons and reasons[0].startswith("L-1:")


class TestLayerMinus1p5NoPyramidingFailsClosed:
    def test_existing_long_check_error_vetoes_buy(self):
        with patch.object(engine, "get_connection", side_effect=RuntimeError("DB locked")):
            reasons = []
            decision = engine.evaluate_trade(
                "FAKE", _score_data(), signal_type="BUY", reasons_out=reasons
            )

        assert decision is None
        assert reasons and reasons[0].startswith("L-1.5:")


class TestLayerMinus1p2MaxPositionsFailsClosed:
    def test_max_positions_check_error_vetoes_buy(self):
        # get_connection succeeds for the (nonexistent) L-1.5 check (no
        # position tracker / no ibkr_positions row needed — a real temp DB
        # would return no row, i.e. no existing long), then fails for L-1.2's
        # own query. Simplest reliable way to isolate just L-1.2: patch
        # get_connection to fail on the SECOND call.
        real_conn_ctx = MagicMock()
        real_conn_ctx.__enter__.return_value.execute.return_value.fetchone.return_value = None
        calls = {"n": 0}

        def _get_connection():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("DB locked")
            return real_conn_ctx

        with patch.object(engine, "get_connection", side_effect=_get_connection):
            reasons = []
            decision = engine.evaluate_trade(
                "FAKE", _score_data(), signal_type="BUY", reasons_out=reasons
            )

        assert decision is None
        assert reasons and reasons[0].startswith("L-1.2:")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
