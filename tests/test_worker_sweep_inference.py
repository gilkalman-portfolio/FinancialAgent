"""Fill sweep: fills invisible to executions, NULL-id rows, stale GTC entries.

FUBO (2026-10-15): bracket BUY filled between cycles, machine slept past
midnight, reqExecutions() no longer served it — the row would have aged into
ERROR with 1508 shares held. YOU SELL id=296 (2026-08-05) never got an
ibkr_order_id and sat SUBMITTED forever. QLYS (2026-09-30) GTC entry rested
unfilled for 16 days.
"""
from datetime import datetime, timedelta

import pytest

import src.database as db
import src.ibkr_worker as w


class _FakeConn:
    def __init__(self, open_ids=(), fills=None):
        self._open = [{"order_id": i} for i in open_ids]
        self._fills = fills or {}
        self.cancelled = []

    def get_open_orders(self):
        return self._open

    def get_executions(self):
        return self._fills

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True


def _ago(**kw):
    return (datetime.now() - timedelta(**kw)).isoformat()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 300)
    monkeypatch.setattr(w, "_last_fill_sweep_ts", None)
    monkeypatch.setattr(w, "_send_telegram", lambda *a, **k: True)
    db.init_db()
    return db


def _order(ticker, action, shares, ibkr_id, created, status="SUBMITTED"):
    with db.get_connection() as c:
        c.execute(
            "INSERT INTO order_log (ticker, action, shares, entry_price, stop_price, "
            "target_price, status, ibkr_order_id, created_at, updated_at) "
            "VALUES (?,?,?,10.0,9.0,12.0,?,?,?,?)",
            (ticker, action, shares, status, ibkr_id, created, created))


def _position(ticker, shares, avg_cost, synced):
    with db.get_connection() as c:
        c.execute(
            "INSERT INTO ibkr_positions (ticker, shares, avg_cost, last_synced) "
            "VALUES (?,?,?,?)", (ticker, shares, avg_cost, synced))


def _signal(ticker, created):
    with db.get_connection() as c:
        c.execute("INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price) "
                  "VALUES (?, ?, 'BUY', 8.45)", (ticker, created))


def _row(ibkr_id=None, ticker=None):
    with db.get_connection() as c:
        if ibkr_id is not None:
            r = c.execute("SELECT * FROM order_log WHERE ibkr_order_id=?", (ibkr_id,)).fetchone()
        else:
            r = c.execute("SELECT * FROM order_log WHERE ticker=?", (ticker,)).fetchone()
    return dict(r)


# ── fill inferred from a held position ──────────────────────────────────────

def test_fubo_fill_inferred_from_fresh_position(temp_db):
    _order("FUBO", "BUY", 1508, 84992, _ago(hours=30))
    _signal("FUBO", _ago(hours=30))
    _position("FUBO", 1508, 8.455, _ago(minutes=2))
    w._periodic_fill_sweep(_FakeConn())
    r = _row(84992)
    assert r["status"] == "FILLED"
    assert r["fill_price"] == pytest.approx(8.455)
    with db.get_connection() as c:
        f = c.execute("SELECT fill_price, fill_order_id FROM forward_signals").fetchone()
    assert f["fill_price"] == pytest.approx(8.455) and f["fill_order_id"] == 84992


def test_inferred_within_grace_window_too(temp_db):
    _order("FUBO", "BUY", 1508, 84992, _ago(hours=2))
    _position("FUBO", 1508, 8.455, _ago(minutes=2))
    w._periodic_fill_sweep(_FakeConn())
    assert _row(84992)["status"] == "FILLED"


def test_stale_position_sync_is_not_trusted(temp_db):
    # Client Portal / sleep trap: a stale snapshot can't prove anything.
    _order("FUBO", "BUY", 1508, 84992, _ago(hours=2))
    _position("FUBO", 1508, 8.455, _ago(hours=5))
    w._periodic_fill_sweep(_FakeConn())
    assert _row(84992)["status"] == "SUBMITTED"


def test_position_smaller_than_order_not_inferred(temp_db):
    _order("FUBO", "BUY", 1508, 84992, _ago(hours=2))
    _position("FUBO", 500, 8.455, _ago(minutes=2))
    w._periodic_fill_sweep(_FakeConn())
    assert _row(84992)["status"] == "SUBMITTED"


def test_position_already_explained_by_other_fill_not_inferred(temp_db):
    _order("FUBO", "BUY", 1508, 111, _ago(days=3), status="FILLED")
    _order("FUBO", "BUY", 1508, 84992, _ago(hours=2))
    _position("FUBO", 1508, 8.455, _ago(minutes=2))
    w._periodic_fill_sweep(_FakeConn())
    assert _row(84992)["status"] == "SUBMITTED"


def test_sell_never_inferred_from_position(temp_db):
    _order("ABC", "SELL", 10, 777, _ago(hours=2))
    _position("ABC", 10, 50.0, _ago(minutes=2))
    w._periodic_fill_sweep(_FakeConn())
    assert _row(777)["status"] == "SUBMITTED"


def test_execution_price_still_wins_over_avg_cost(temp_db):
    _order("FUBO", "BUY", 1508, 84992, _ago(hours=2))
    _position("FUBO", 1508, 8.455, _ago(minutes=2))
    w._periodic_fill_sweep(_FakeConn(fills={84992: 8.44}))
    assert _row(84992)["fill_price"] == pytest.approx(8.44)


def test_startup_reconcile_infers_from_position(temp_db):
    _order("FUBO", "BUY", 1508, 84992, _ago(hours=30))
    _position("FUBO", 1508, 8.455, _ago(minutes=2))
    w._reconcile_orders_on_startup(_FakeConn())
    r = _row(84992)
    assert r["status"] == "FILLED" and "inferred" in r["notes"]


# ── NULL ibkr_order_id ──────────────────────────────────────────────────────

def test_null_ibkr_id_errored_after_grace(temp_db):
    _order("YOU", "SELL", 10, None, _ago(days=60))
    w._periodic_fill_sweep(_FakeConn())
    r = _row(ticker="YOU")
    assert r["status"] == "ERROR" and "Never reached broker" in r["notes"]


def test_null_ibkr_id_left_alone_within_grace(temp_db):
    _order("YOU", "SELL", 10, None, _ago(hours=1))
    w._periodic_fill_sweep(_FakeConn())
    assert _row(ticker="YOU")["status"] == "SUBMITTED"


# ── stale resting entries ───────────────────────────────────────────────────

def test_stale_unfilled_entry_cancelled(temp_db):
    _order("QLYS", "BUY", 71, 82649, _ago(days=16))
    conn = _FakeConn(open_ids=[82649])
    w._periodic_fill_sweep(conn)
    assert conn.cancelled == [82649]
    r = _row(82649)
    assert r["status"] == "CANCELLED" and "Stale" in r["notes"]


def test_fresh_resting_entry_kept(temp_db):
    _order("DECK", "BUY", 150, 85045, _ago(hours=3))
    conn = _FakeConn(open_ids=[85045])
    w._periodic_fill_sweep(conn)
    assert conn.cancelled == []
    assert _row(85045)["status"] == "SUBMITTED"


def test_resting_sell_never_cancelled_as_stale(temp_db):
    _order("ABC", "SELL", 10, 999, _ago(days=10))
    conn = _FakeConn(open_ids=[999])
    w._periodic_fill_sweep(conn)
    assert conn.cancelled == []


def test_stale_entry_with_held_position_not_cancelled(temp_db):
    # Parent still listed as open but the position is there (e.g. partial fill):
    # cancelling would strip the bracket's stop/target from real shares.
    _order("QLYS", "BUY", 71, 82649, _ago(days=2))
    _position("QLYS", 71, 178.5, _ago(minutes=2))
    conn = _FakeConn(open_ids=[82649])
    w._periodic_fill_sweep(conn)
    assert conn.cancelled == []


def test_failed_cancel_leaves_row_submitted(temp_db):
    _order("QLYS", "BUY", 71, 82649, _ago(days=16))
    conn = _FakeConn(open_ids=[82649])
    conn.cancel_order = lambda oid: False
    w._periodic_fill_sweep(conn)
    assert _row(82649)["status"] == "SUBMITTED"


# ── time-stop holding period (EXPO/EVER, 2026-10-08) ────────────────────────

def _h(action, shares, ts):
    return {"action": action, "shares": shares, "created_at": ts}


def test_missed_bracket_exit_does_not_leak_old_entry_date():
    # EXPO: 73sh 07-31, exited via bracket (never logged), 182sh fresh 10-07.
    hist = [_h("BUY", 182, "2026-10-07"), _h("BUY", 73, "2026-07-31")]
    assert w._holding_period_start(hist, 182) == "2026-10-07"


def test_pyramided_position_dates_from_first_add():
    hist = [_h("BUY", 50, "2026-07-22"), _h("BUY", 100, "2026-06-26")]
    assert w._holding_period_start(hist, 150) == "2026-06-26"


def test_partial_exit_counts_back_through_sell():
    # 70 bought, T1 sold 28, 42 held — still dated from the 70sh entry.
    hist = [_h("SELL", 28, "2026-08-11"), _h("BUY", 70, "2026-08-10"),
            _h("BUY", 30, "2026-05-01")]
    assert w._holding_period_start(hist, 42) == "2026-08-10"


def test_rebuy_after_logged_close_starts_fresh():
    hist = [_h("BUY", 40, "2026-08-07"), _h("SELL", 40, "2026-08-01"),
            _h("BUY", 40, "2026-07-01")]
    assert w._holding_period_start(hist, 40) == "2026-08-07"


def test_unexplained_position_falls_back_to_oldest_buy():
    hist = [_h("BUY", 10, "2026-09-01"), _h("BUY", 10, "2026-08-01")]
    assert w._holding_period_start(hist, 500) == "2026-08-01"


def test_no_buys_returns_none():
    assert w._holding_period_start([], 10) is None
