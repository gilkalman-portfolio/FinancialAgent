"""
A resting SELL with no long behind it can only open a short.

On 2026-08-06 an IBKR paper-account reset flattened every position but left the
GTC bracket legs resting at the broker. They executed against a zero position and
created five shorts — BOX 154, CARG 133, GEN 176, each exactly the prior long
size, i.e. the size its bracket leg was written for. A reset clears positions; it
does not clear orders.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    p = tmp_path / "t.db"

    def _conn():
        c = sqlite3.connect(str(p))
        c.row_factory = sqlite3.Row
        return c

    import src.ibkr_worker  # noqa: F401
    monkeypatch.setattr("src.database.get_connection", _conn)
    monkeypatch.setattr("src.ibkr_worker.get_connection", _conn)

    c = _conn()
    c.execute("""CREATE TABLE ibkr_positions (ticker TEXT PRIMARY KEY, shares REAL,
                 avg_cost REAL, unrealized_pnl REAL, market_value REAL,
                 last_synced TEXT, exit_tier INTEGER DEFAULT 0)""")
    c.commit()
    c.close()
    yield _conn


def _pos(db, ticker, shares):
    with db() as c:
        c.execute("INSERT INTO ibkr_positions (ticker, shares, avg_cost, unrealized_pnl,"
                  " market_value, last_synced) VALUES (?,?,10,0,?,?)",
                  (ticker, shares, shares * 10, datetime.now().isoformat()))


def _order(ticker, action="SELL", qty=100, oid=1, otype="STP"):
    return {"order_id": oid, "ticker": ticker, "action": action, "qty": qty,
            "order_type": otype, "status": "Submitted",
            "lmt_price": None, "aux_price": 9.0}


class TestCancelsUnbacked:
    def test_sell_with_no_position_is_cancelled(self, _db):
        from src import ibkr_worker
        conn = MagicMock()
        conn.get_open_orders.return_value = [_order("BOX", qty=154, oid=11)]
        conn.cancel_order.return_value = True

        with patch.object(ibkr_worker, "_send_telegram") as tg:
            n = ibkr_worker._cancel_unbacked_sell_orders(conn)

        assert n == 1
        conn.cancel_order.assert_called_once_with(11)
        assert "BOX" in tg.call_args[0][0]

    def test_the_full_reset_scenario(self, _db):
        """All five bracket legs left resting after the account was flattened."""
        from src import ibkr_worker
        legs = [_order("BOX", qty=154, oid=1), _order("CARG", qty=133, oid=2),
                _order("GEN", qty=176, oid=3), _order("GO", qty=523, oid=4),
                _order("FUBO", qty=529, oid=5)]
        conn = MagicMock()
        conn.get_open_orders.return_value = legs
        conn.cancel_order.return_value = True

        with patch.object(ibkr_worker, "_send_telegram"):
            assert ibkr_worker._cancel_unbacked_sell_orders(conn) == 5
        assert conn.cancel_order.call_count == 5

    def test_short_position_also_counts_as_unbacked(self, _db):
        from src import ibkr_worker
        _pos(_db, "KSS", -13247)
        conn = MagicMock()
        conn.get_open_orders.return_value = [_order("KSS", qty=300, oid=9)]
        conn.cancel_order.return_value = True
        with patch.object(ibkr_worker, "_send_telegram"):
            assert ibkr_worker._cancel_unbacked_sell_orders(conn) == 1


class TestLeavesLegitimateOrders:
    def test_backed_sell_is_left_alone(self, _db):
        from src import ibkr_worker
        _pos(_db, "AAA", 200)
        conn = MagicMock()
        conn.get_open_orders.return_value = [_order("AAA", qty=200, oid=3)]
        with patch.object(ibkr_worker, "_send_telegram"):
            assert ibkr_worker._cancel_unbacked_sell_orders(conn) == 0
        conn.cancel_order.assert_not_called()

    def test_oversized_stop_leg_is_warned_not_cancelled(self, _db):
        """Normal after a partial exit — cancelling would strip protection."""
        from src import ibkr_worker
        _pos(_db, "AAA", 60)
        conn = MagicMock()
        conn.get_open_orders.return_value = [_order("AAA", qty=100, oid=4)]
        with patch.object(ibkr_worker, "_send_telegram"):
            assert ibkr_worker._cancel_unbacked_sell_orders(conn) == 0
        conn.cancel_order.assert_not_called()

    def test_buy_orders_are_ignored(self, _db):
        from src import ibkr_worker
        conn = MagicMock()
        conn.get_open_orders.return_value = [_order("AAA", action="BUY", qty=100, oid=5)]
        with patch.object(ibkr_worker, "_send_telegram"):
            assert ibkr_worker._cancel_unbacked_sell_orders(conn) == 0
        conn.cancel_order.assert_not_called()

    def test_no_orders_is_a_no_op(self, _db):
        from src import ibkr_worker
        conn = MagicMock()
        conn.get_open_orders.return_value = []
        assert ibkr_worker._cancel_unbacked_sell_orders(conn) == 0

    def test_broker_failure_does_not_raise(self, _db):
        from src import ibkr_worker
        conn = MagicMock()
        conn.get_open_orders.side_effect = RuntimeError("not connected")
        assert ibkr_worker._cancel_unbacked_sell_orders(conn) == 0

    def test_bracket_stop_leg_survives_while_parent_buy_is_still_pending(self, _db):
        """TMDX/GEN/STE/EVER, 2026-08-07: a bracket's stop+target legs are placed
        alongside the parent BUY, before it fills. ibkr_positions still shows 0
        shares for a ticker whose parent hasn't filled (or filled seconds ago and
        hasn't synced yet) — this must not be read as an orphaned SELL from a
        reset. A live open BUY for the same ticker means the SELL is waiting on
        its own parent, not orphaned."""
        from src import ibkr_worker
        conn = MagicMock()
        conn.get_open_orders.return_value = [
            _order("GEN", action="BUY", qty=448, oid=1, otype="LMT"),
            _order("GEN", action="SELL", qty=448, oid=2, otype="STP"),
            _order("GEN", action="SELL", qty=448, oid=3, otype="LMT"),
        ]
        with patch.object(ibkr_worker, "_send_telegram"):
            assert ibkr_worker._cancel_unbacked_sell_orders(conn) == 0
        conn.cancel_order.assert_not_called()
