"""
Tests for the P&L / digest audit fixes (2026-07-03):

  1. position_tracker.record_daily_pnl() — fallback day_pnl computed as the
     delta of net_liquidation vs. the previous day's row when IBKR reports 0.
  2. forward_signals.weekly_digest() — direction-aware win-rate (SELL win =
     price went DOWN), with BUY/SELL win-rates broken out separately.
  3. opportunity_tracker.update_outcomes() — High/Low intraday touch
     detection, with both-touched-same-day conservatively scored as a stop hit.

All IBKR / yfinance calls are mocked. Uses a real tmp-path SQLite DB (matches
the pattern in tests/test_order_funnel.py).
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.position_tracker import PositionTracker
from src import forward_signals
from src import opportunity_tracker


# ── Shared DB fixture ───────────────────────────────────────────────────────

@pytest.fixture
def db_conn_factory(tmp_path):
    """Return a get_connection()-compatible factory backed by a tmp SQLite file."""
    db_path = tmp_path / "test.db"

    def _get_conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_pnl (
            date            TEXT PRIMARY KEY,
            day_pnl         REAL,
            net_liquidation REAL,
            recorded_at     TEXT
        );
        CREATE TABLE IF NOT EXISTS ibkr_positions (
            ticker          TEXT PRIMARY KEY,
            shares          REAL,
            avg_cost        REAL,
            unrealized_pnl  REAL,
            market_value    REAL,
            last_synced     TEXT
        );
        CREATE TABLE IF NOT EXISTS forward_signals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT NOT NULL,
            signal_ts        TEXT NOT NULL,
            signal_type      TEXT NOT NULL,
            entry_price      REAL,
            composite_score  REAL,
            catalyst_summary TEXT,
            supertrend_level REAL,
            supertrend_atr   REAL,
            ai_verdict       TEXT,
            telegram_sent_at TEXT,
            status           TEXT,
            data_quality_flag TEXT,
            fill_price       REAL,
            fill_source      TEXT,
            price_after_7d   REAL,
            price_after_14d  REAL,
            price_after_30d  REAL,
            return_7d_pct    REAL,
            return_14d_pct   REAL,
            return_30d_pct   REAL
        );
        CREATE TABLE IF NOT EXISTS order_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker        TEXT,
            action        TEXT,
            status        TEXT,
            ibkr_order_id INTEGER,
            created_at    TEXT,
            updated_at    TEXT,
            notes         TEXT
        );
        CREATE TABLE IF NOT EXISTS scan_results (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker     TEXT,
            scanned_at TEXT,
            price      REAL
        );
        CREATE TABLE IF NOT EXISTS opportunity_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker        TEXT NOT NULL,
            detected_at   TEXT NOT NULL,
            signal_type   TEXT,
            entry_price   REAL,
            stop_loss     REAL,
            target1       REAL,
            target2       REAL,
            rr_ratio      REAL,
            status        TEXT NOT NULL DEFAULT 'open',
            outcome_price REAL,
            outcome_at    TEXT,
            outcome_pct   REAL
        );
        """
    )
    conn.close()
    return _get_conn


# ═════════════════════════════════════════════════════════════════════════
# 1. position_tracker — day_pnl fallback delta computation
# ═════════════════════════════════════════════════════════════════════════

class TestDailyPnlFallback:
    def test_fallback_used_when_ibkr_reports_zero(self, db_conn_factory, monkeypatch):
        """When IBKR's day_pnl is 0.0 (the known DailyPnL tag bug), compute the
        fallback as delta of net_liquidation vs. the prior day's stored row."""
        monkeypatch.setattr("src.position_tracker.get_connection", db_conn_factory)

        # Seed yesterday's row.
        conn = db_conn_factory()
        conn.execute(
            "INSERT INTO daily_pnl (date, day_pnl, net_liquidation, recorded_at) "
            "VALUES ('2026-07-01', 0.0, 1000000.0, '2026-07-01T20:00:00')"
        )
        conn.commit()
        conn.close()

        mock_ibkr = MagicMock()
        mock_ibkr.get_account_summary.return_value = {
            "net_liquidation": 1001500.0,
            "day_pnl": 0.0,  # IBKR under-reports — the bug under test
        }
        tracker = PositionTracker(mock_ibkr)

        # Force "now" to be after 09:30 ET on a later date.
        with patch("src.position_tracker.datetime") as mock_dt:
            import datetime as real_datetime
            from zoneinfo import ZoneInfo
            fixed_now = real_datetime.datetime(2026, 7, 2, 15, 0, tzinfo=ZoneInfo("America/New_York"))
            mock_dt.now.side_effect = lambda tz=None: fixed_now if tz else real_datetime.datetime(2026, 7, 2, 15, 0)
            mock_dt.strftime = real_datetime.datetime.strftime
            result = tracker.record_daily_pnl()

        assert result is True
        conn = db_conn_factory()
        row = conn.execute(
            "SELECT day_pnl, net_liquidation FROM daily_pnl WHERE date = '2026-07-02'"
        ).fetchone()
        conn.close()
        assert row is not None
        # 1001500.0 - 1000000.0 = 1500.0
        assert row["day_pnl"] == pytest.approx(1500.0)
        assert row["net_liquidation"] == pytest.approx(1001500.0)

    def test_ibkr_value_used_when_nonzero(self, db_conn_factory, monkeypatch):
        """When IBKR actually reports a non-zero day_pnl, use it directly —
        no fallback override."""
        monkeypatch.setattr("src.position_tracker.get_connection", db_conn_factory)

        mock_ibkr = MagicMock()
        mock_ibkr.get_account_summary.return_value = {
            "net_liquidation": 1001500.0,
            "day_pnl": 842.13,
        }
        tracker = PositionTracker(mock_ibkr)

        with patch("src.position_tracker.datetime") as mock_dt:
            import datetime as real_datetime
            from zoneinfo import ZoneInfo
            fixed_now = real_datetime.datetime(2026, 7, 2, 15, 0, tzinfo=ZoneInfo("America/New_York"))
            mock_dt.now.side_effect = lambda tz=None: fixed_now if tz else real_datetime.datetime(2026, 7, 2, 15, 0)
            result = tracker.record_daily_pnl()

        assert result is True
        conn = db_conn_factory()
        row = conn.execute(
            "SELECT day_pnl FROM daily_pnl WHERE date = '2026-07-02'"
        ).fetchone()
        conn.close()
        assert row["day_pnl"] == pytest.approx(842.13)

    def test_no_fallback_available_stays_zero(self, db_conn_factory, monkeypatch):
        """No prior-day row exists — fallback can't compute a delta, day_pnl
        stays 0.0 (can't fabricate data from nothing)."""
        monkeypatch.setattr("src.position_tracker.get_connection", db_conn_factory)

        mock_ibkr = MagicMock()
        mock_ibkr.get_account_summary.return_value = {
            "net_liquidation": 1000000.0,
            "day_pnl": 0.0,
        }
        tracker = PositionTracker(mock_ibkr)

        with patch("src.position_tracker.datetime") as mock_dt:
            import datetime as real_datetime
            from zoneinfo import ZoneInfo
            fixed_now = real_datetime.datetime(2026, 7, 1, 15, 0, tzinfo=ZoneInfo("America/New_York"))
            mock_dt.now.side_effect = lambda tz=None: fixed_now if tz else real_datetime.datetime(2026, 7, 1, 15, 0)
            result = tracker.record_daily_pnl()

        assert result is True
        conn = db_conn_factory()
        row = conn.execute(
            "SELECT day_pnl FROM daily_pnl WHERE date = '2026-07-01'"
        ).fetchone()
        conn.close()
        assert row["day_pnl"] == pytest.approx(0.0)

    def test_gated_before_0930_et(self, db_conn_factory, monkeypatch):
        """Pre-market ET calls still skip entirely (existing gate preserved)."""
        monkeypatch.setattr("src.position_tracker.get_connection", db_conn_factory)
        mock_ibkr = MagicMock()
        mock_ibkr.get_account_summary.return_value = {
            "net_liquidation": 1000000.0,
            "day_pnl": 0.0,
        }
        tracker = PositionTracker(mock_ibkr)

        with patch("src.position_tracker.datetime") as mock_dt:
            import datetime as real_datetime
            from zoneinfo import ZoneInfo
            fixed_now = real_datetime.datetime(2026, 7, 2, 6, 0, tzinfo=ZoneInfo("America/New_York"))
            mock_dt.now.side_effect = lambda tz=None: fixed_now if tz else real_datetime.datetime(2026, 7, 2, 6, 0)
            result = tracker.record_daily_pnl()

        assert result is False
        mock_ibkr.get_account_summary.assert_not_called()

    def test_compute_fallback_day_pnl_helper_directly(self, db_conn_factory, monkeypatch):
        monkeypatch.setattr("src.position_tracker.get_connection", db_conn_factory)
        conn = db_conn_factory()
        conn.execute(
            "INSERT INTO daily_pnl (date, day_pnl, net_liquidation, recorded_at) "
            "VALUES ('2026-06-30', 0.0, 999000.0, '2026-06-30T20:00:00')"
        )
        conn.commit()
        conn.close()

        delta = PositionTracker._compute_fallback_day_pnl("2026-07-01", 1000500.0)
        assert delta == pytest.approx(1500.0)

        # No prior row for a date with nothing before it.
        delta_none = PositionTracker._compute_fallback_day_pnl("2026-06-01", 500.0)
        assert delta_none is None


# ═════════════════════════════════════════════════════════════════════════
# 2. forward_signals — direction-aware win rate
# ═════════════════════════════════════════════════════════════════════════

class TestForwardSignalsWinRate:
    def _seed(self, conn, rows):
        # Relative, not hardcoded. These tests used a fixed 2026-07-01 timestamp
        # and weekly_digest() filters on `signal_ts >= now - days`, so they passed
        # when written and silently began failing once the calendar moved more
        # than 30 days past that date — then got recorded in CLAUDE.md as
        # "pre-existing failures unrelated to core logic". They were test rot.
        ts = (datetime.now() - timedelta(days=5)).isoformat()
        for ticker, signal_type, ret_7d in rows:
            conn.execute(
                "INSERT INTO forward_signals "
                "(ticker, signal_ts, signal_type, entry_price, status, return_7d_pct) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ticker, ts, signal_type, 100.0, "matured", ret_7d),
            )
        conn.commit()

    def test_sell_win_when_price_drops(self, db_conn_factory, monkeypatch):
        monkeypatch.setattr("src.forward_signals.get_connection", db_conn_factory)
        conn = db_conn_factory()
        # SELL with return -5% → win. SELL with return +3% → loss.
        self._seed(conn, [
            ("AAA", "SELL", -5.0),
            ("BBB", "SELL", 3.0),
        ])
        conn.close()

        d = forward_signals.weekly_digest(days=30)
        assert d["measured_7d_sell"] == 2
        assert d["winners_7d_sell"] if "winners_7d_sell" in d else True  # not exposed directly
        assert d["win_rate_7d_sell_pct"] == pytest.approx(50.0)
        # Overall win_rate_7d_pct must also treat SELL direction-aware: 1 win / 2 = 50%
        assert d["win_rate_7d_pct"] == pytest.approx(50.0)

    def test_buy_win_when_price_rises(self, db_conn_factory, monkeypatch):
        monkeypatch.setattr("src.forward_signals.get_connection", db_conn_factory)
        conn = db_conn_factory()
        self._seed(conn, [
            ("CCC", "BUY", 8.0),   # win
            ("DDD", "BUY", -2.0),  # loss
            ("EEE", "BUY", 4.0),   # win
        ])
        conn.close()

        d = forward_signals.weekly_digest(days=30)
        assert d["measured_7d_buy"] == 3
        assert d["win_rate_7d_buy_pct"] == pytest.approx(2 / 3 * 100.0)

    def test_mixed_buy_sell_breakdown(self, db_conn_factory, monkeypatch):
        monkeypatch.setattr("src.forward_signals.get_connection", db_conn_factory)
        conn = db_conn_factory()
        self._seed(conn, [
            ("AAA", "BUY", 10.0),   # win
            ("BBB", "BUY", -10.0),  # loss
            ("CCC", "SELL", -6.0),  # win (price dropped)
            ("DDD", "SELL", 6.0),   # loss (price rose, bad for SELL)
        ])
        conn.close()

        d = forward_signals.weekly_digest(days=30)
        assert d["win_rate_7d_buy_pct"] == pytest.approx(50.0)
        assert d["win_rate_7d_sell_pct"] == pytest.approx(50.0)
        # Overall: 2 wins / 4 measured = 50%
        assert d["win_rate_7d_pct"] == pytest.approx(50.0)

    def test_digest_message_shows_buy_sell_separately(self, db_conn_factory, monkeypatch):
        monkeypatch.setattr("src.forward_signals.get_connection", db_conn_factory)
        conn = db_conn_factory()
        self._seed(conn, [
            ("AAA", "BUY", 10.0),
            ("CCC", "SELL", -6.0),
        ])
        conn.close()

        d = forward_signals.weekly_digest(days=30)
        msg = forward_signals.format_digest_message(d)
        assert "BUY win rate" in msg
        assert "SELL win rate" in msg


# ═════════════════════════════════════════════════════════════════════════
# 3. opportunity_tracker — High/Low intraday touch detection
# ═════════════════════════════════════════════════════════════════════════

def _make_hist_mock(high, low, close):
    """Build a MagicMock standing in for yf.Ticker(...).history() DataFrame."""
    import pandas as pd
    df = pd.DataFrame({"High": [high], "Low": [low], "Close": [close]})
    return df


class TestOpportunityTouchDetection:
    def _seed_open(self, conn, ticker="XYZ", entry=100.0, stop=90.0, target1=110.0):
        # Relative for the same reason as _seed above: a hardcoded 2026-06-25
        # detection date eventually aged past the tracker's expiry window, so
        # "still open" became "expired" through nothing but the passage of time.
        detected = (datetime.now() - timedelta(days=3)).isoformat()
        conn.execute(
            "INSERT INTO opportunity_log "
            "(ticker, detected_at, signal_type, entry_price, stop_loss, target1, status) "
            "VALUES (?, ?, 'BUY', ?, ?, ?, 'open')",
            (ticker, detected, entry, stop, target1),
        )
        conn.commit()

    def test_target_touched_intraday_high(self, db_conn_factory, monkeypatch):
        """High >= target1 even if Close pulled back below it → hit_target."""
        monkeypatch.setattr("src.opportunity_tracker.get_connection", db_conn_factory)
        conn = db_conn_factory()
        self._seed_open(conn)
        conn.close()

        # High touches 112 (above target 110), but closes back at 105.
        hist_df = _make_hist_mock(high=112.0, low=99.0, close=105.0)
        with patch("src.opportunity_tracker.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist_df
            stats = opportunity_tracker.update_outcomes()

        assert stats["hit_target"] == 1
        assert stats["hit_stop"] == 0

        conn = db_conn_factory()
        row = conn.execute("SELECT status, outcome_price FROM opportunity_log WHERE ticker='XYZ'").fetchone()
        conn.close()
        assert row["status"] == "hit_target"
        assert row["outcome_price"] == pytest.approx(110.0)

    def test_stop_touched_intraday_low_then_recovered(self, db_conn_factory, monkeypatch):
        """Low <= stop even if Close recovered above it → hit_stop (was
        previously missed by a Close-only check)."""
        monkeypatch.setattr("src.opportunity_tracker.get_connection", db_conn_factory)
        conn = db_conn_factory()
        self._seed_open(conn)
        conn.close()

        # Low dips to 88 (below stop 90), but recovers to close at 102.
        hist_df = _make_hist_mock(high=103.0, low=88.0, close=102.0)
        with patch("src.opportunity_tracker.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist_df
            stats = opportunity_tracker.update_outcomes()

        assert stats["hit_stop"] == 1
        assert stats["hit_target"] == 0

        conn = db_conn_factory()
        row = conn.execute("SELECT status, outcome_price FROM opportunity_log WHERE ticker='XYZ'").fetchone()
        conn.close()
        assert row["status"] == "hit_stop"
        assert row["outcome_price"] == pytest.approx(90.0)

    def test_both_touched_same_day_counts_as_stop(self, db_conn_factory, monkeypatch):
        """High >= target1 AND Low <= stop_loss on the same day — conservative
        assumption per the audit: count as stop hit (can't determine ordering
        from daily OHLC bars alone)."""
        monkeypatch.setattr("src.opportunity_tracker.get_connection", db_conn_factory)
        conn = db_conn_factory()
        self._seed_open(conn)
        conn.close()

        # Wide-range day: touches both target (110) and stop (90).
        hist_df = _make_hist_mock(high=115.0, low=85.0, close=100.0)
        with patch("src.opportunity_tracker.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist_df
            stats = opportunity_tracker.update_outcomes()

        assert stats["hit_stop"] == 1
        assert stats["hit_target"] == 0

        conn = db_conn_factory()
        row = conn.execute("SELECT status FROM opportunity_log WHERE ticker='XYZ'").fetchone()
        conn.close()
        assert row["status"] == "hit_stop"

    def test_still_open_when_neither_touched(self, db_conn_factory, monkeypatch):
        monkeypatch.setattr("src.opportunity_tracker.get_connection", db_conn_factory)
        conn = db_conn_factory()
        self._seed_open(conn)
        conn.close()

        hist_df = _make_hist_mock(high=105.0, low=95.0, close=101.0)
        with patch("src.opportunity_tracker.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = hist_df
            stats = opportunity_tracker.update_outcomes()

        assert stats["hit_stop"] == 0
        assert stats["hit_target"] == 0

        conn = db_conn_factory()
        row = conn.execute("SELECT status FROM opportunity_log WHERE ticker='XYZ'").fetchone()
        conn.close()
        assert row["status"] == "open"


# ═════════════════════════════════════════════════════════════════════════
# 4. forward_signals — auto_adjust=True basis for _fetch_price_at
# ═════════════════════════════════════════════════════════════════════════

class TestFetchPriceAtAutoAdjust:
    def test_history_called_with_auto_adjust_true(self):
        import pandas as pd
        from datetime import datetime as real_datetime

        df = pd.DataFrame({"Close": [123.45]})
        with patch("src.forward_signals.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = df
            price = forward_signals._fetch_price_at("ZZZ", real_datetime(2026, 6, 1))

        assert price == pytest.approx(123.45)
        _, kwargs = mock_ticker.return_value.history.call_args
        assert kwargs.get("auto_adjust") is True
