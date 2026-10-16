"""record_fill must land on the signal that produced the order, not any row.

2026-10-16: a FUBO time-stop SELL fill (no signal row exists for software
exits) was written onto an unrelated FUBO BUY signal from 2026-08-12.
"""
from datetime import datetime, timedelta

import pytest

import src.database as db
from src.forward_signals import record_fill


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db


def _sig(ticker, ts, stype="BUY"):
    with db.get_connection() as c:
        return c.execute("INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price) "
                         "VALUES (?,?,?,10)", (ticker, ts.isoformat(), stype)).lastrowid


def _ord(ticker, action, oid, ts):
    with db.get_connection() as c:
        c.execute("INSERT INTO order_log (ticker, action, shares, entry_price, stop_price, "
                  "target_price, status, ibkr_order_id, created_at, updated_at) "
                  "VALUES (?,?,10,10,9,12,'FILLED',?,?,?)",
                  (ticker, action, oid, ts.isoformat(), ts.isoformat()))


def _fill(sid):
    with db.get_connection() as c:
        return c.execute("SELECT fill_price FROM forward_signals WHERE id=?", (sid,)).fetchone()[0]


def test_software_exit_sell_does_not_hit_old_buy_signal(temp_db):
    old = _sig("FUBO", datetime(2026, 8, 12, 16, 49))
    _ord("FUBO", "SELL", 85444, datetime.now())
    assert record_fill("FUBO", 8.31, 85444) is False
    assert _fill(old) is None


def test_buy_fill_lands_on_its_own_signal(temp_db):
    now = datetime.now()
    sid = _sig("AAA", now - timedelta(seconds=5))
    _ord("AAA", "BUY", 1, now)
    assert record_fill("AAA", 10.5, 1) is True
    assert _fill(sid) == 10.5


def test_signal_sell_fill_lands_on_sell_signal_not_newer_buy(temp_db):
    now = datetime.now()
    sell = _sig("AAA", now - timedelta(seconds=5), "SELL")
    buy = _sig("AAA", now - timedelta(seconds=2), "BUY")
    _ord("AAA", "SELL", 2, now)
    assert record_fill("AAA", 11.0, 2) is True
    assert _fill(sell) == 11.0 and _fill(buy) is None


def test_old_unfilled_signal_outside_window_ignored(temp_db):
    now = datetime.now()
    stale = _sig("AAA", now - timedelta(hours=3))
    _ord("AAA", "BUY", 3, now)
    assert record_fill("AAA", 10.0, 3) is False
    assert _fill(stale) is None
