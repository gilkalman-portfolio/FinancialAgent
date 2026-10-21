"""Trailing stop must use the initial stop's own geometry (2 x DAILY ATR(14)).

Regression for the 2026-10-21 investigation: the trail used 2.5 x ATR of 1h
bars (~0.85 daily ATR) against an initial stop of 2 x daily ATR, so the very
first trailing pass — run in the same worker cycle as the bracket submission —
ratcheted every new position's stop to less than half its intended distance
while the trade was still flat (SBGI 2026-10-20: stop 12.21 -> 12.63, 6 s after
the bracket was placed). Also pins: no trailing of an unfilled bracket, never
above price, and T1's breakeven move never lowers a higher trailed stop.
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    db_path = tmp_path / "t.db"

    def _get_conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    import src.ibkr_worker  # noqa: F401
    import src.order_manager  # noqa: F401
    monkeypatch.setattr("src.database.get_connection", _get_conn)
    monkeypatch.setattr("src.ibkr_worker.get_connection", _get_conn)
    monkeypatch.setattr("src.order_manager.get_connection", _get_conn)
    c = _get_conn()
    c.executescript("""
        CREATE TABLE ibkr_positions (ticker TEXT PRIMARY KEY, shares REAL, avg_cost REAL,
            unrealized_pnl REAL, market_value REAL, last_synced TEXT, exit_tier INTEGER DEFAULT 0);
        CREATE TABLE order_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
            action TEXT NOT NULL, shares INTEGER NOT NULL, entry_price REAL, stop_price REAL,
            target_price REAL, status TEXT NOT NULL, fill_price REAL, ibkr_order_id INTEGER,
            stop_order_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, notes TEXT);
    """)
    c.close()
    import src.ibkr_worker as w
    w._last_trailing_stop_ts = None
    yield _get_conn


def _pos(get_conn, ticker, shares, avg_cost, upnl=0.0, tier=0):
    with get_conn() as c:
        c.execute("INSERT INTO ibkr_positions VALUES (?,?,?,?,?,?,?)",
                  (ticker, shares, avg_cost, upnl, shares * avg_cost + upnl,
                   datetime.now().isoformat(), tier))


def _buy(get_conn, ticker, stop_order_id):
    now = datetime.now().isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO order_log (ticker, action, shares, entry_price, stop_price, target_price,"
                  " status, ibkr_order_id, stop_order_id, created_at, updated_at) "
                  "VALUES (?, 'BUY', 100, 13.11, 12.21, 14.47, 'FILLED', 1, ?, ?, ?)",
                  (ticker, stop_order_id, now, now))


def _hourly(last_close, n=40, hr_range=0.13):
    """Hourly bars whose 1h ATR is small (like SBGI: ~0.13 vs daily ~0.45)."""
    idx = pd.date_range(datetime.now() - timedelta(hours=n), periods=n, freq="h")
    close = np.full(n, last_close)
    return pd.DataFrame({"Open": close, "High": close + hr_range / 2, "Low": close - hr_range / 2,
                         "Close": close, "Volume": np.ones(n)}, index=idx)


def _daily(last_close, n=25, day_range=0.45, end_days_ago=1):
    """Completed daily bars (ending yesterday) with TR == day_range."""
    end = pd.Timestamp(datetime.now().date()) - pd.Timedelta(days=end_days_ago)
    idx = pd.date_range(end=end, periods=n, freq="D")
    close = np.full(n, last_close)
    return pd.DataFrame({"Open": close, "High": close + day_range / 2, "Low": close - day_range / 2,
                         "Close": close, "Volume": np.ones(n)}, index=idx)


def _conn(ticker, stop, hourly, daily, order_id=85945):
    conn = MagicMock()
    conn.get_open_orders.return_value = [{
        "order_id": order_id, "ticker": ticker, "action": "SELL", "order_type": "STP",
        "aux_price": stop, "lmt_price": None, "qty": 100, "status": "Submitted"}]
    conn.historical_bars.side_effect = lambda t, bar_size="1 hour", duration="10 D", **k: (
        daily if bar_size == "1 day" else hourly)
    conn.modify_stop_order.return_value = True
    return conn


def test_multiplier_matches_initial_stop_geometry():
    from src import execution_engine, ibkr_worker
    assert ibkr_worker.TRAILING_ATR_MULTIPLIER == execution_engine._ATR_STOP_MULT
    assert ibkr_worker.TRAILING_ATR_PERIOD == 14


def test_first_pass_after_entry_does_not_tighten_flat_position(_db):
    """SBGI 2026-10-20 replay: entry 13.11, stop 12.21 (= 13.11 - 2*0.45).
    Price still 13.11. Old rule: 13.11 - 2.5*0.13 = 12.79 -> raised. New: no-op."""
    from src import ibkr_worker
    _pos(_db, "SBGI", 971, 13.05)
    _buy(_db, "SBGI", 85945)
    conn = _conn("SBGI", 12.21, _hourly(13.11), _daily(13.11, day_range=0.45))
    ibkr_worker._update_trailing_stops(conn)
    conn.modify_stop_order.assert_not_called()


def test_raises_once_price_has_moved_up(_db):
    from src import ibkr_worker
    _pos(_db, "SBGI", 971, 13.05)
    _buy(_db, "SBGI", 85945)
    conn = _conn("SBGI", 12.21, _hourly(14.00), _daily(13.11, day_range=0.45))
    ibkr_worker._update_trailing_stops(conn)
    conn.modify_stop_order.assert_called_once()
    args, kwargs = conn.modify_stop_order.call_args
    assert args[0] == "SBGI"
    assert args[1] == pytest.approx(14.00 - 2 * 0.45, abs=0.01)  # 13.10
    assert kwargs.get("stop_order_id") == 85945


def test_never_lowers(_db):
    from src import ibkr_worker
    _pos(_db, "AAA", 100, 50.0)
    conn = _conn("AAA", 49.5, _hourly(50.0), _daily(50.0, day_range=1.0))  # cand 48.0 < 49.5
    ibkr_worker._update_trailing_stops(conn)
    conn.modify_stop_order.assert_not_called()


def test_unfilled_bracket_is_not_trailed(_db):
    """Bracket STP child is visible in open orders before the parent fills."""
    from src import ibkr_worker
    conn = _conn("NEW", 9.0, _hourly(12.0), _daily(10.0, day_range=0.2))  # no ibkr_positions row
    ibkr_worker._update_trailing_stops(conn)
    conn.modify_stop_order.assert_not_called()


def test_one_percent_floor_like_initial_stop(_db):
    from src import ibkr_worker
    _pos(_db, "LOWVOL", 100, 100.0)
    conn = _conn("LOWVOL", 90.0, _hourly(110.0), _daily(110.0, day_range=0.1))  # 2*ATR=0.2 < 1%
    ibkr_worker._update_trailing_stops(conn)
    args, _ = conn.modify_stop_order.call_args
    assert args[1] == pytest.approx(110.0 - 1.10, abs=0.01)


def test_insufficient_daily_history_skips(_db):
    from src import ibkr_worker
    _pos(_db, "IPO", 100, 10.0)
    conn = _conn("IPO", 9.0, _hourly(12.0), _daily(12.0, n=5))
    ibkr_worker._update_trailing_stops(conn)
    conn.modify_stop_order.assert_not_called()


def test_daily_atr_drops_todays_in_progress_bar():
    from src import ibkr_worker
    d = _daily(10.0, n=20, day_range=0.5, end_days_ago=1)
    today = pd.DataFrame({"Open": [10.0], "High": [10.01], "Low": [9.99], "Close": [10.0], "Volume": [1.0]},
                         index=[pd.Timestamp(datetime.now().date())])
    assert ibkr_worker._daily_atr(pd.concat([d, today])) == pytest.approx(0.5)


def test_t1_breakeven_never_lowers_a_higher_trailed_stop(_db):
    """IT 2026-10-15: trail had the stop at 196.65; T1 'moved it to breakeven' 192.92."""
    from src import ibkr_worker
    _pos(_db, "IT", 66, 192.92, upnl=66 * 192.92 * 0.08)  # +8% -> T1
    _buy(_db, "IT", 84386)
    conn = _conn("IT", 196.65, _hourly(208.0), _daily(208.0), order_id=84386)
    conn.place_limit_order.return_value = 999
    with patch.object(ibkr_worker, "_send_telegram"):
        ibkr_worker._check_tiered_exits(conn)
    conn.place_limit_order.assert_called_once()        # the 40% partial still goes out
    conn.modify_stop_order.assert_not_called()          # but the stop is not loosened


def test_t1_breakeven_still_raises_a_lower_stop(_db):
    from src import ibkr_worker
    _pos(_db, "TDW", 100, 86.15, upnl=100 * 86.15 * 0.08)
    _buy(_db, "TDW", 777)
    conn = _conn("TDW", 77.57, _hourly(93.0), _daily(93.0), order_id=777)
    conn.place_limit_order.return_value = 998
    with patch.object(ibkr_worker, "_send_telegram"):
        ibkr_worker._check_tiered_exits(conn)
    conn.modify_stop_order.assert_called_once()
    args, kwargs = conn.modify_stop_order.call_args
    assert args[1] == pytest.approx(86.15)
    assert kwargs.get("stop_order_id") == 777
