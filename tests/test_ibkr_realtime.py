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


class _Event:
    """Minimal stand-in for ib_async's Event: supports += / -= / emit."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def __isub__(self, fn):
        self.handlers.remove(fn)
        return self

    def emit(self, *args):
        for fn in list(self.handlers):
            fn(*args)


class FakeIB:
    """Fake IB whose broker 'answers' a modify on the next ib.sleep().

    broker_mode:
      accept  - echo openOrder with the new auxPrice (normal ack)
      reject  - echo openOrder with the OLD auxPrice, then Error 201 (what the
                live log shows for MUR/CRC on 2026-09-09..11)
      silent  - never answer (lost ack / slow Gateway)
      wrong   - echo a different auxPrice than requested
    """

    def __init__(self, broker_mode="accept"):
        self.broker_mode = broker_mode
        self.trades = []
        self.openOrderEvent = _Event()
        self.errorEvent = _Event()
        self.placeOrder = MagicMock(side_effect=self._place)
        self.sleep_calls = 0
        self._pending = []

    def openTrades(self):
        return list(self.trades)

    def _place(self, contract, order):
        trade = next(t for t in self.trades if t.order is order)
        self._pending.append((trade, trade._broker_price, order.auxPrice))

    def sleep(self, secs=0.02):
        self.sleep_calls += 1
        pending, self._pending = self._pending, []
        for trade, old, new in pending:
            if self.broker_mode == "accept":
                trade._broker_price = new
                trade.order.auxPrice = new
                self.openOrderEvent.emit(trade)
            elif self.broker_mode == "reject":
                trade.order.auxPrice = old
                self.openOrderEvent.emit(trade)
                self.errorEvent.emit(trade.order.orderId, 201,
                                     "Order rejected - reason:Cannot modify suspended order", None)
            elif self.broker_mode == "wrong":
                trade.order.auxPrice = round(new - 1.0, 2)
                self.openOrderEvent.emit(trade)
        return True


@pytest.fixture
def conn(monkeypatch):
    """A bare IBKRConnection with self.ib replaced by a FakeIB."""
    monkeypatch.setattr(ibkr_realtime, "IB", MagicMock)
    c = ibkr_realtime.IBKRConnection()
    c.ib = FakeIB()
    return c


def _fake_trade(symbol, order_id, order_type, action, aux_price=0.0, done=False):
    trade = MagicMock()
    trade.contract.symbol = symbol
    trade.order.orderId = order_id
    trade.order.orderType = order_type
    trade.order.action = action
    trade.order.auxPrice = aux_price
    trade._broker_price = aux_price
    trade.isDone.return_value = done
    trade.orderStatus.status = "Cancelled" if done else "PreSubmitted"
    return trade


class TestModifyStopOrderIdMatch:
    def test_matches_exact_order_id_among_ambiguous_duplicates(self, conn):
        """Two resting STP SELL orders for the same ticker — passing
        stop_order_id must modify the RIGHT one, not just the first found."""
        wrong = _fake_trade("AAPL", order_id=100, order_type="STP", action="SELL", aux_price=150.0)
        right = _fake_trade("AAPL", order_id=200, order_type="STP", action="SELL", aux_price=145.0)
        conn.ib.trades = [wrong, right]

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
        conn.ib.trades = [first, second]

        result = conn.modify_stop_order("MSFT", 310.0)

        assert result is True
        assert first.order.auxPrice == 310.0
        assert second.order.auxPrice == 301.0, "must not touch the second match"

    def test_id_given_but_not_found_does_not_fall_back_to_scan(self, conn):
        """A stale/mismatched id must fail safe (no modification at all) —
        never silently fall back to modifying a different order by ticker,
        which would defeat the whole point of matching by exact id."""
        other = _fake_trade("TSLA", order_id=999, order_type="STP", action="SELL", aux_price=200.0)
        conn.ib.trades = [other]

        result = conn.modify_stop_order("TSLA", 210.0, stop_order_id=555)

        assert result is False
        assert other.order.auxPrice == 200.0, "must be untouched"
        conn.ib.placeOrder.assert_not_called()

    def test_no_open_trades_returns_false(self, conn):
        conn.ib.trades = []
        assert conn.modify_stop_order("NFLX", 100.0) is False
        assert conn.modify_stop_order("NFLX", 100.0, stop_order_id=1) is False


class TestPlaceBracketOrderReturnsBothIds:
    def test_returns_parent_and_stop_leg_ids(self, conn, monkeypatch):
        """order_manager.submit() reads both keys out of this return value to
        populate order_log.ibkr_order_id / stop_order_id."""
        monkeypatch.setattr(ibkr_realtime, "Stock", lambda *a, **k: MagicMock())
        monkeypatch.setattr(ibkr_realtime, "LimitOrder", lambda *a, **k: MagicMock())
        monkeypatch.setattr(ibkr_realtime, "StopOrder", lambda *a, **k: MagicMock())
        conn.ib = MagicMock()
        conn.ib.client.getReqId.side_effect = [111, 222, 333]

        result = conn.place_bracket_order(
            ticker="AAPL", action="BUY", shares=10,
            entry_price=150.0, stop_price=145.0, target_price=160.0,
        )

        assert result == {"order_id": 111, "stop_order_id": 222}
        assert conn.ib.placeOrder.call_count == 3


class TestModifyStopOrderConfirmation:
    """modify_stop_order() must only report success once the broker echoed the
    new trigger price — see the 2026-10-21 fix (183 Error-201 rejections on
    MUR/CRC were logged as successful raises)."""

    def test_accepted_modify_returns_true_after_echo(self, conn):
        t = _fake_trade("IT", 10, "STP", "SELL", aux_price=212.40)
        conn.ib.trades = [t]
        assert conn.modify_stop_order("IT", 212.90, stop_order_id=10, confirm_timeout=1.0) is True
        assert t.order.auxPrice == 212.90
        assert conn.ib.sleep_calls >= 1, "must actually wait for the ack"

    def test_rejected_modify_returns_false_and_warns(self, conn, caplog):
        conn.ib.broker_mode = "reject"
        t = _fake_trade("MUR", 81569, "STP", "SELL", aux_price=36.57)
        conn.ib.trades = [t]
        with caplog.at_level("WARNING"):
            ok = conn.modify_stop_order("MUR", 37.16, stop_order_id=81569, confirm_timeout=1.0)
        assert ok is False
        assert t.order.auxPrice == 36.57, "local copy must reflect the broker's price"
        assert "NOT confirmed" in caplog.text and "201" in caplog.text

    def test_rejection_returns_fast_not_at_timeout(self, conn):
        import time as _t
        conn.ib.broker_mode = "reject"
        conn.ib.trades = [_fake_trade("MUR", 1, "STP", "SELL", aux_price=36.57)]
        start = _t.monotonic()
        assert conn.modify_stop_order("MUR", 37.16, stop_order_id=1, confirm_timeout=3.0) is False
        assert _t.monotonic() - start < 1.0

    def test_silent_broker_times_out_bounded_and_restores_local_price(self, conn, caplog):
        import time as _t
        conn.ib.broker_mode = "silent"
        t = _fake_trade("CRC", 81510, "STP", "SELL", aux_price=53.89)
        conn.ib.trades = [t]
        start = _t.monotonic()
        with caplog.at_level("WARNING"):
            ok = conn.modify_stop_order("CRC", 55.48, stop_order_id=81510, confirm_timeout=0.3)
        elapsed = _t.monotonic() - start
        assert ok is False
        assert 0.25 <= elapsed < 1.5, "must wait, but never longer than confirm_timeout"
        assert t.order.auxPrice == 53.89
        assert "no broker acknowledgement" in caplog.text

    def test_echo_with_different_price_is_not_success(self, conn):
        conn.ib.broker_mode = "wrong"
        conn.ib.trades = [_fake_trade("SBGI", 5, "STP", "SELL", aux_price=12.80)]
        assert conn.modify_stop_order("SBGI", 12.82, stop_order_id=5, confirm_timeout=0.3) is False

    def test_benign_warning_code_does_not_abort_wait(self, conn):
        t = _fake_trade("RIG", 7, "STP", "SELL", aux_price=5.59)
        conn.ib.trades = [t]
        orig_place = conn.ib._place

        def place_with_399(contract, order):
            orig_place(contract, order)
            conn.ib.errorEvent.emit(7, 399, "order will not be placed until RTH", None)
        conn.ib.placeOrder.side_effect = place_with_399
        assert conn.modify_stop_order("RIG", 5.68, stop_order_id=7, confirm_timeout=1.0) is True

    def test_other_orders_errors_are_ignored(self, conn):
        t = _fake_trade("YOU", 8, "STP", "SELL", aux_price=42.76)
        conn.ib.trades = [t]
        orig_place = conn.ib._place

        def place_with_foreign_error(contract, order):
            orig_place(contract, order)
            conn.ib.errorEvent.emit(9999, 201, "an unrelated order", None)
        conn.ib.placeOrder.side_effect = place_with_foreign_error
        assert conn.modify_stop_order("YOU", 44.27, stop_order_id=8, confirm_timeout=1.0) is True

    def test_done_trade_is_not_modified(self, conn):
        t = _fake_trade("X", 3, "STP", "SELL", aux_price=10.0, done=True)
        conn.ib.trades = [t]
        assert conn.modify_stop_order("X", 11.0, stop_order_id=3, confirm_timeout=0.3) is False
        conn.ib.placeOrder.assert_not_called()

    def test_event_handlers_are_unsubscribed(self, conn):
        conn.ib.trades = [_fake_trade("IT", 10, "STP", "SELL", aux_price=1.0)]
        conn.modify_stop_order("IT", 2.0, stop_order_id=10, confirm_timeout=0.3)
        conn.ib.broker_mode = "silent"
        conn.modify_stop_order("IT", 3.0, stop_order_id=10, confirm_timeout=0.1)
        assert conn.ib.openOrderEvent.handlers == []
        assert conn.ib.errorEvent.handlers == []

    def test_ambiguous_scan_without_id_warns(self, conn, caplog):
        a = _fake_trade("MSFT", 1, "STP", "SELL", aux_price=300.0)
        b = _fake_trade("MSFT", 2, "STP", "SELL", aux_price=301.0)
        conn.ib.trades = [a, b]
        with caplog.at_level("WARNING"):
            assert conn.modify_stop_order("MSFT", 305.0, confirm_timeout=1.0) is True
        assert "2 resting STP SELL orders" in caplog.text
