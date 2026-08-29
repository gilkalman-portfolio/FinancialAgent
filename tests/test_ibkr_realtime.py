"""
Unit tests for src/ibkr_realtime.py's IBKRConnection.modify_stop_order() and
place_bracket_order() — the 2026-08-29 fix that lets a caller target the
exact STP leg by IBKR order id instead of an ambiguous ticker/type/action
scan (see CLAUDE.md Open Backlog: "modify_stop_order() matches the first STP
SELL by ticker — ambiguous if multiple STPs exist for one ticker").

ib_async is only installed in .venv313 (see CLAUDE.md IBKR Real-Time
Architecture) — this main venv can't import it, so IB/Stock/LimitOrder/
StopOrder (all None here via the module's own try/except ImportError stub)
are monkeypatched to stand-ins rather than exercising a real IB Gateway.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.ibkr_realtime as ibkr_realtime


@pytest.fixture
def conn(monkeypatch):
    """A bare IBKRConnection with self.ib replaced by a MagicMock."""
    monkeypatch.setattr(ibkr_realtime, "IB", MagicMock)
    return ibkr_realtime.IBKRConnection()


def _fake_trade(symbol, order_id, order_type, action, aux_price=0.0):
    trade = MagicMock()
    trade.contract.symbol = symbol
    trade.order.orderId = order_id
    trade.order.orderType = order_type
    trade.order.action = action
    trade.order.auxPrice = aux_price
    return trade


class TestModifyStopOrderIdMatch:
    def test_matches_exact_order_id_among_ambiguous_duplicates(self, conn):
        """Two resting STP SELL orders for the same ticker — passing
        stop_order_id must modify the RIGHT one, not just the first found."""
        wrong = _fake_trade("AAPL", order_id=100, order_type="STP", action="SELL", aux_price=150.0)
        right = _fake_trade("AAPL", order_id=200, order_type="STP", action="SELL", aux_price=145.0)
        conn.ib.openTrades.return_value = [wrong, right]

        result = conn.modify_stop_order("AAPL", 152.0, stop_order_id=200)

        assert result is True
        assert wrong.order.auxPrice == 150.0, "the non-matching order must be untouched"
        assert right.order.auxPrice == 152.0
        conn.ib.placeOrder.assert_called_once_with(right.contract, right.order)

    def test_no_id_falls_back_to_ticker_scan_first_match(self, conn):
        """Backward compat: an omitted stop_order_id must behave exactly as
        before this parameter existed — first ticker/type/action match wins."""
        first = _fake_trade("MSFT", order_id=1, order_type="STP", action="SELL", aux_price=300.0)
        second = _fake_trade("MSFT", order_id=2, order_type="STP", action="SELL", aux_price=301.0)
        conn.ib.openTrades.return_value = [first, second]

        result = conn.modify_stop_order("MSFT", 310.0)

        assert result is True
        assert first.order.auxPrice == 310.0
        assert second.order.auxPrice == 301.0, "must not touch the second match"

    def test_id_given_but_not_found_does_not_fall_back_to_scan(self, conn):
        """A stale/mismatched id must fail safe (no modification at all) —
        never silently fall back to modifying a different order by ticker,
        which would defeat the whole point of matching by exact id."""
        other = _fake_trade("TSLA", order_id=999, order_type="STP", action="SELL", aux_price=200.0)
        conn.ib.openTrades.return_value = [other]

        result = conn.modify_stop_order("TSLA", 210.0, stop_order_id=555)

        assert result is False
        assert other.order.auxPrice == 200.0, "must be untouched"
        conn.ib.placeOrder.assert_not_called()

    def test_no_open_trades_returns_false(self, conn):
        conn.ib.openTrades.return_value = []
        assert conn.modify_stop_order("NFLX", 100.0) is False
        assert conn.modify_stop_order("NFLX", 100.0, stop_order_id=1) is False


class TestPlaceBracketOrderReturnsBothIds:
    def test_returns_parent_and_stop_leg_ids(self, conn, monkeypatch):
        """order_manager.submit() reads both keys out of this return value to
        populate order_log.ibkr_order_id / stop_order_id."""
        monkeypatch.setattr(ibkr_realtime, "Stock", lambda *a, **k: MagicMock())
        monkeypatch.setattr(ibkr_realtime, "LimitOrder", lambda *a, **k: MagicMock())
        monkeypatch.setattr(ibkr_realtime, "StopOrder", lambda *a, **k: MagicMock())
        conn.ib.client.getReqId.side_effect = [111, 222, 333]

        result = conn.place_bracket_order(
            ticker="AAPL", action="BUY", shares=10,
            entry_price=150.0, stop_price=145.0, target_price=160.0,
        )

        assert result == {"order_id": 111, "stop_order_id": 222}
        assert conn.ib.placeOrder.call_count == 3
