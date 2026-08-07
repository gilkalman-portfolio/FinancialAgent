"""
Tests for the 2026-08-05 live-readiness fixes.

Covers the five defects found while auditing paper trading before the live switch:
  1. startup reconciliation destroyed fill state instead of recovering it
  2. the periodic fill sweep marked unconfirmed orders ERROR (30 bogus rows)
  3. the time stop measured from the LAST fill, so pyramided positions never aged
  4. run_once() skipped the whole exit layer whenever the entry queue was empty
  5. concurrent SELL paths could oversell a position — a short, in a long-only bot

All IBKR calls are mocked. Real SQLite is used so the order_log assertions are real.
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _test_db(monkeypatch, tmp_path):
    """Point every get_connection() at a file-backed test DB."""
    db_path = tmp_path / "test.db"

    def _get_conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    import src.ibkr_worker  # noqa: F401
    import src.order_manager  # noqa: F401

    monkeypatch.setattr("src.database.get_connection", _get_conn)
    monkeypatch.setattr("src.ibkr_worker.get_connection", _get_conn)
    monkeypatch.setattr("src.order_manager.get_connection", _get_conn)
    monkeypatch.setattr("src.execution_engine.get_connection", _get_conn)

    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE ibkr_positions (
            ticker TEXT PRIMARY KEY, shares REAL, avg_cost REAL,
            unrealized_pnl REAL, market_value REAL, last_synced TEXT,
            exit_tier INTEGER DEFAULT 0
        );
        CREATE TABLE order_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, action TEXT NOT NULL, shares INTEGER NOT NULL,
            entry_price REAL, stop_price REAL, target_price REAL,
            status TEXT NOT NULL, fill_price REAL, ibkr_order_id INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, notes TEXT
        );
        CREATE TABLE watchlist_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, alert_type TEXT, message TEXT,
            sent_at TEXT, score REAL, price REAL
        );
        CREATE TABLE scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL, scan_type TEXT NOT NULL DEFAULT 'manual',
            total_scanned INTEGER DEFAULT 0, notes TEXT
        );
        CREATE TABLE scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER, ticker TEXT NOT NULL, scanned_at TEXT NOT NULL,
            price REAL, explosion_score REAL, recommendation TEXT,
            raw_data TEXT, catalyst TEXT
        );
    """)
    conn.close()
    yield _get_conn


@pytest.fixture
def stub_engine():
    """Execution engine stub that always approves.

    The long-only guard lives in order_manager AFTER the engine returns, so the
    engine is deliberately stubbed out — using the real one would drag yfinance
    regime/ATR lookups into a unit test of share arithmetic.
    """
    engine = MagicMock()
    engine.normalize_score_data.side_effect = lambda d: d
    engine.evaluate_trade.return_value = {
        "sizing": {"shares": 10, "stop_price": 0.0, "target_price": 0.0},
    }
    return engine


def _insert_order(get_conn, ticker, action, shares, status, created_at,
                  ibkr_order_id=None, notes=None):
    with get_conn() as c:
        c.execute(
            "INSERT INTO order_log (ticker, action, shares, entry_price, stop_price, "
            "target_price, status, ibkr_order_id, created_at, updated_at, notes) "
            "VALUES (?,?,?,10.0,9.0,12.0,?,?,?,?,?)",
            (ticker, action, shares, status, ibkr_order_id, created_at, created_at, notes),
        )


def _insert_position(get_conn, ticker, shares, avg_cost, unrealized_pnl):
    with get_conn() as c:
        c.execute(
            "INSERT INTO ibkr_positions (ticker, shares, avg_cost, unrealized_pnl, "
            "market_value, last_synced, exit_tier) VALUES (?,?,?,?,?,?,0)",
            (ticker, shares, avg_cost, unrealized_pnl,
             shares * avg_cost + unrealized_pnl, datetime.now().isoformat()),
        )


# ── 1. Startup reconciliation recovers fills instead of destroying them ─────

class TestStartupReconciliation:
    def test_missing_order_found_in_executions_is_marked_filled(self, _test_db):
        """The worker restarts often; a fill completed while it was down must be
        recovered, not branded ERROR."""
        from src import ibkr_worker

        _insert_order(_test_db, "FUBO", "BUY", 529, "SUBMITTED",
                      datetime.now().isoformat(), ibkr_order_id=777)

        conn = MagicMock()
        conn.get_open_orders.return_value = []          # no longer open — it filled
        conn.get_executions.return_value = {777: 9.44}  # broker confirms the fill

        with patch.object(ibkr_worker, "record_fill") as rec:
            ibkr_worker._reconcile_orders_on_startup(conn)
            rec.assert_called_once_with("FUBO", 9.44, 777)

        with _test_db() as c:
            row = c.execute("SELECT status, fill_price FROM order_log").fetchone()
        assert row["status"] == "FILLED"
        assert row["fill_price"] == pytest.approx(9.44)

    def test_unconfirmed_order_is_left_submitted_not_errored(self, _test_db):
        """Regression: this path used to mark every missing order ERROR, which is
        what corrupted 30 rows — 14 of them real fills for still-held tickers."""
        from src import ibkr_worker

        _insert_order(_test_db, "CBT", "BUY", 55, "SUBMITTED",
                      datetime.now().isoformat(), ibkr_order_id=888)

        conn = MagicMock()
        conn.get_open_orders.return_value = []
        conn.get_executions.return_value = {}  # no evidence either way

        ibkr_worker._reconcile_orders_on_startup(conn)

        with _test_db() as c:
            status = c.execute("SELECT status FROM order_log").fetchone()["status"]
        assert status == "SUBMITTED", "an unexplained order must not be called ERROR"


# ── 2. Fill sweep grace window ──────────────────────────────────────────────

class TestFillSweepGraceWindow:
    def _run_sweep(self, executions, open_orders=None):
        from src import ibkr_worker
        ibkr_worker._last_fill_sweep_ts = None  # force it to run
        conn = MagicMock()
        conn.get_open_orders.return_value = open_orders or []
        conn.get_executions.return_value = executions
        ibkr_worker._periodic_fill_sweep(conn)

    def test_recent_unconfirmed_order_stays_submitted(self, _test_db):
        created = (datetime.now() - timedelta(hours=1)).isoformat()
        _insert_order(_test_db, "GO", "BUY", 534, "SUBMITTED", created, ibkr_order_id=101)

        self._run_sweep(executions={})

        with _test_db() as c:
            status = c.execute("SELECT status FROM order_log").fetchone()["status"]
        assert status == "SUBMITTED", "inside the grace window it is unknown, not failed"

    def test_old_unconfirmed_order_is_errored_after_grace(self, _test_db):
        created = (datetime.now() - timedelta(days=3)).isoformat()
        _insert_order(_test_db, "GO", "BUY", 534, "SUBMITTED", created, ibkr_order_id=102)

        self._run_sweep(executions={})

        with _test_db() as c:
            status = c.execute("SELECT status FROM order_log").fetchone()["status"]
        assert status == "ERROR", "past the grace window with execution data, ERROR is correct"

    def test_execution_match_marks_filled_and_records_fill(self, _test_db):
        from src import ibkr_worker
        created = (datetime.now() - timedelta(hours=2)).isoformat()
        _insert_order(_test_db, "TRS", "BUY", 126, "SUBMITTED", created, ibkr_order_id=103)

        with patch.object(ibkr_worker, "record_fill") as rec:
            self._run_sweep(executions={103: 39.52})
            rec.assert_called_once_with("TRS", 39.52, 103)

        with _test_db() as c:
            row = c.execute("SELECT status, fill_price FROM order_log").fetchone()
        assert row["status"] == "FILLED"
        assert row["fill_price"] == pytest.approx(39.52)


# ── 3. Time stop measures from the FIRST fill ───────────────────────────────

class TestTimeStopEntryDate:
    def test_pyramided_position_ages_from_first_buy(self, _test_db):
        """CALM: first bought 2026-06-26, added 2026-07-22. Measuring from the last
        add gave 9 trading days against a 15-day threshold, so it never fired."""
        from src import ibkr_worker
        ibkr_worker._last_time_stop_ts = None

        _insert_position(_test_db, "CALM", 242, 85.15, -84.0)  # ~-0.4%, a zombie
        first = (datetime.now() - timedelta(days=40)).isoformat()
        recent = (datetime.now() - timedelta(days=3)).isoformat()
        _insert_order(_test_db, "CALM", "BUY", 100, "FILLED", first, ibkr_order_id=1)
        _insert_order(_test_db, "CALM", "BUY", 142, "FILLED", recent, ibkr_order_id=2)

        conn = MagicMock()
        conn.place_limit_order.return_value = 5001

        with patch.object(ibkr_worker, "_send_telegram"):
            ibkr_worker._check_time_stops(conn)

        # Exits now route through order_manager.submit_exit, which calls the
        # broker with keyword args after clamping to the held size.
        conn.place_limit_order.assert_called_once()
        assert conn.place_limit_order.call_args.kwargs["ticker"] == "CALM"
        assert conn.place_limit_order.call_args.kwargs["shares"] == 242
        with _test_db() as c:
            row = c.execute(
                "SELECT notes, shares FROM order_log WHERE action='SELL'"
            ).fetchone()
        assert "TIME STOP" in row["notes"]
        assert row["shares"] == 242

    def test_position_with_only_errored_buy_still_ages(self, _test_db):
        """RRR/TRS/LCII had no FILLED BUY row because the sweep corrupted it, so the
        time stop skipped them permanently."""
        from src import ibkr_worker
        ibkr_worker._last_time_stop_ts = None

        _insert_position(_test_db, "RRR", 152, 65.60, -197.0)  # ~-2%, a zombie
        old = (datetime.now() - timedelta(days=40)).isoformat()
        _insert_order(_test_db, "RRR", "BUY", 152, "ERROR", old, ibkr_order_id=9)

        conn = MagicMock()
        conn.place_limit_order.return_value = 5002

        with patch.object(ibkr_worker, "_send_telegram"):
            ibkr_worker._check_time_stops(conn)

        conn.place_limit_order.assert_called_once()
        assert conn.place_limit_order.call_args.kwargs["ticker"] == "RRR"

    def test_directional_position_is_not_time_stopped(self, _test_db):
        from src import ibkr_worker
        ibkr_worker._last_time_stop_ts = None

        # +10% — has direction, not a zombie
        _insert_position(_test_db, "GO", 1000, 10.0, 1000.0)
        old = (datetime.now() - timedelta(days=40)).isoformat()
        _insert_order(_test_db, "GO", "BUY", 1000, "FILLED", old, ibkr_order_id=3)

        conn = MagicMock()
        with patch.object(ibkr_worker, "_send_telegram"):
            ibkr_worker._check_time_stops(conn)
        conn.place_limit_order.assert_not_called()

    def test_reopened_position_ages_from_new_entry_not_old_history(self, _test_db):
        """PTCT/YELP, 2026-08-07: each bought weeks ago, fully closed, then bought
        fresh again today. Aging from the ticker's all-time earliest FILLED BUY
        treated the brand-new position as a 19-24 trading-day zombie and exited it
        within 30 minutes of entry — a fresh position's ~0% P&L sits squarely
        inside the zombie band. The entry date must reset once shares round-tripped
        back to flat between the old trade and the new one."""
        from src import ibkr_worker
        ibkr_worker._last_time_stop_ts = None

        _insert_position(_test_db, "PTCT", 87, 71.89, -17.0)  # ~-0.3%, inside the zombie band
        old_buy = (datetime.now() - timedelta(days=40)).isoformat()
        old_sell = (datetime.now() - timedelta(days=39)).isoformat()
        fresh_buy = (datetime.now() - timedelta(minutes=30)).isoformat()
        _insert_order(_test_db, "PTCT", "BUY", 59, "FILLED", old_buy, ibkr_order_id=1)
        _insert_order(_test_db, "PTCT", "SELL", 59, "FILLED", old_sell, ibkr_order_id=2)
        _insert_order(_test_db, "PTCT", "BUY", 87, "FILLED", fresh_buy, ibkr_order_id=3)

        conn = MagicMock()
        with patch.object(ibkr_worker, "_send_telegram"):
            ibkr_worker._check_time_stops(conn)
        conn.place_limit_order.assert_not_called()


# ── 4. Long-only invariant ──────────────────────────────────────────────────

class TestLongOnlyInvariant:
    def test_pending_sell_blocks_a_second_full_exit(self, _test_db, stub_engine):
        """KSS sold 309 against 287 held. A second exit must never stack."""
        from src.order_manager import OrderManager
        from src.signal_combiner import CombinedAlert

        _insert_position(_test_db, "KSS", 287, 18.84, 0.0)
        _insert_order(_test_db, "KSS", "SELL", 287, "SUBMITTED",
                      datetime.now().isoformat(), ibkr_order_id=200)

        ibkr = MagicMock()
        mgr = OrderManager(ibkr, stub_engine, paper_mode=True)
        alert = CombinedAlert(
            ticker="KSS", alert_type="combined_sell", entry_price=18.90,
            composite_score=70.0, catalyst_summary=None,
            supertrend_level=18.0, message="sell",
        )

        result = mgr.submit(alert)

        assert result["status"] == "VETOED"
        assert "long-only" in result["reason"].lower()
        ibkr.place_limit_order.assert_not_called()

    def test_partial_pending_sell_reduces_the_new_exit(self, _test_db, stub_engine):
        from src.order_manager import OrderManager
        from src.signal_combiner import CombinedAlert

        _insert_position(_test_db, "GTY", 143, 30.0, 0.0)
        _insert_order(_test_db, "GTY", "SELL", 100, "SUBMITTED",
                      datetime.now().isoformat(), ibkr_order_id=201)

        ibkr = MagicMock()
        ibkr.place_limit_order.return_value = 999
        mgr = OrderManager(ibkr, stub_engine, paper_mode=True)
        alert = CombinedAlert(
            ticker="GTY", alert_type="combined_sell", entry_price=31.0,
            composite_score=70.0, catalyst_summary=None,
            supertrend_level=30.0, message="sell",
        )

        result = mgr.submit(alert)

        assert result["status"] == "SUBMITTED"
        assert result["shares"] == 43, "143 held - 100 working = 43 sellable"

    def test_has_pending_sell_helper(self, _test_db):
        from src import ibkr_worker

        assert ibkr_worker._has_pending_sell("NONE") is False
        _insert_order(_test_db, "ABC", "SELL", 10, "SUBMITTED",
                      datetime.now().isoformat(), ibkr_order_id=300)
        assert ibkr_worker._has_pending_sell("ABC") is True

    def test_filled_sell_does_not_block(self, _test_db):
        from src import ibkr_worker
        _insert_order(_test_db, "XYZ", "SELL", 10, "FILLED",
                      datetime.now().isoformat(), ibkr_order_id=301)
        assert ibkr_worker._has_pending_sell("XYZ") is False


# ── 5. Exit layer runs even with an empty entry queue ───────────────────────

class TestStalePositionsBlockEntries:
    def test_failed_sync_blocks_entries_but_not_exits(self, _test_db):
        """Every position-aware guard reads ibkr_positions. If the sync failed,
        those guards evaluate against fiction — so no new entries. Exits still
        run, because they protect capital already at risk."""
        from src import ibkr_worker

        conn = MagicMock()
        entry = MagicMock()
        entry.ticker = "AAA"
        with patch.object(ibkr_worker, "build_queue", return_value=[entry]), \
             patch.object(ibkr_worker, "PositionTracker") as tracker_cls, \
             patch.object(ibkr_worker, "_check_ticker") as check, \
             patch.object(ibkr_worker, "_periodic_fill_sweep"), \
             patch.object(ibkr_worker, "_update_trailing_stops"), \
             patch.object(ibkr_worker, "_check_tiered_exits") as tiers, \
             patch.object(ibkr_worker, "_check_time_stops") as tstop, \
             patch.object(ibkr_worker, "_check_score_deterioration"):
            tracker = MagicMock()
            tracker.sync_positions.side_effect = RuntimeError("gateway timeout")
            tracker_cls.return_value = tracker

            fired = ibkr_worker.run_once(conn)

        assert fired == 0
        check.assert_not_called(), "no ticker may be evaluated on stale positions"
        tiers.assert_called_once()
        tstop.assert_called_once()

    def test_successful_sync_allows_entries(self, _test_db):
        from src import ibkr_worker

        conn = MagicMock()
        entry = MagicMock()
        entry.ticker = "AAA"
        with patch.object(ibkr_worker, "build_queue", return_value=[entry]), \
             patch.object(ibkr_worker, "PositionTracker") as tracker_cls, \
             patch.object(ibkr_worker, "_check_ticker", return_value=None) as check, \
             patch.object(ibkr_worker, "_periodic_fill_sweep"), \
             patch.object(ibkr_worker, "_update_trailing_stops"), \
             patch.object(ibkr_worker, "_check_tiered_exits"), \
             patch.object(ibkr_worker, "_check_time_stops"), \
             patch.object(ibkr_worker, "_check_score_deterioration"):
            tracker_cls.return_value = MagicMock()
            ibkr_worker.run_once(conn)

        check.assert_called_once()


class TestExitLayerRunsOnEmptyQueue:
    def test_empty_queue_still_runs_exit_functions(self, _test_db):
        """The early return made every stop conditional on having something to buy."""
        from src import ibkr_worker

        conn = MagicMock()
        with patch.object(ibkr_worker, "build_queue", return_value=[]), \
             patch.object(ibkr_worker, "PositionTracker") as tracker_cls, \
             patch.object(ibkr_worker, "_periodic_fill_sweep") as sweep, \
             patch.object(ibkr_worker, "_update_trailing_stops") as trail, \
             patch.object(ibkr_worker, "_check_tiered_exits") as tiers, \
             patch.object(ibkr_worker, "_check_time_stops") as tstop, \
             patch.object(ibkr_worker, "_check_score_deterioration") as deter:
            tracker_cls.return_value = MagicMock()

            fired = ibkr_worker.run_once(conn)

        assert fired == 0
        sweep.assert_called_once()
        trail.assert_called_once()
        tiers.assert_called_once()
        tstop.assert_called_once()
        deter.assert_called_once()
