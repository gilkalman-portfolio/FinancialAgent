"""Regression: the fill sweep / startup reconciliation must not self-deadlock.

record_fill() opens its own connection. Calling it while the sweep's own
connection still held an uncommitted `UPDATE order_log` made the process block
on its own write lock ("database is locked", worker stall, forward_signals
fill_price left NULL — seen live 2026-10-06). The fix defers record_fill()
until after the sweep's transaction commits.
"""
from datetime import datetime, timedelta

import pytest

import src.database as db
import src.ibkr_worker as w


class _FakeConn:
    def __init__(self, fills):
        self._fills = fills

    def get_open_orders(self):
        return []

    def get_executions(self):
        return self._fills


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 300)  # fail fast if it deadlocks
    db.init_db()
    created = (datetime.now() - timedelta(hours=1)).isoformat()
    with db.get_connection() as c:
        c.execute(
            "INSERT INTO order_log (ticker, action, shares, entry_price, stop_price, "
            "target_price, status, ibkr_order_id, created_at, updated_at) "
            "VALUES ('AAA','BUY',10,10.0,9.0,12.0,'SUBMITTED',555,?,?)", (created, created))
        c.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price) "
            "VALUES ('AAA', ?, 'BUY', 10.0)", (created,))
    return db


def _state():
    with db.get_connection() as c:
        o = c.execute("SELECT status, fill_price FROM order_log WHERE ibkr_order_id=555").fetchone()
        f = c.execute("SELECT fill_price, fill_order_id FROM forward_signals WHERE ticker='AAA'").fetchone()
    return dict(o), dict(f)


def test_periodic_sweep_records_fill_without_lock(temp_db, monkeypatch):
    monkeypatch.setattr(w, "_last_fill_sweep_ts", None)
    w._periodic_fill_sweep(_FakeConn({555: 10.5}))
    o, f = _state()
    assert o == {"status": "FILLED", "fill_price": 10.5}
    assert f == {"fill_price": 10.5, "fill_order_id": 555}


def test_startup_reconcile_records_fill_without_lock(temp_db):
    w._reconcile_orders_on_startup(_FakeConn({555: 10.5}))
    o, f = _state()
    assert o["status"] == "FILLED"
    assert f["fill_price"] == 10.5
