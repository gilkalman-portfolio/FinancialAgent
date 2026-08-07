"""
Tests for the signal-path hardening fixes (2026-07):

  1. Partial-bar Supertrend drop in ibkr_worker._check_ticker
       - a flip that exists only on the in-progress (last) bar must NOT fire
       - a flip on the last CLOSED bar fires exactly once (bars_ago == 1)
  2. Weekend gate in ibkr_worker._is_signal_hours (Saturday ET blocked)
  3. scan_type='scheduled' filter in signal_combiner._latest_scan_context
       - a catalyst explosion_score row (scan_type != 'scheduled') is ignored
  4. Dedup rollback on Telegram send failure (signal_combiner.release_dedup)
  5. Markdown escaping of dynamic text in the combined-alert messages

IBKR / yfinance / Telegram are all mocked. Uses a real file-backed SQLite DB
(same pattern as tests/test_order_funnel.py and tests/test_new_fixes.py).
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


# ── Shared DB fixture ─────────────────────────────────────────────────────────

@pytest.fixture()
def _db(tmp_path, monkeypatch):
    """File-backed test DB redirected from get_connection in every module used."""
    db_path = tmp_path / "test.db"

    def _get_conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    monkeypatch.setattr("src.database.get_connection", _get_conn)
    monkeypatch.setattr("src.signal_combiner.get_connection", _get_conn)
    monkeypatch.setattr("src.forward_signals.get_connection", _get_conn)

    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS watchlist_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            alert_type  TEXT NOT NULL,
            message     TEXT,
            sent_at     TEXT NOT NULL,
            score       REAL,
            price       REAL
        );
        CREATE TABLE IF NOT EXISTS scan_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at        TEXT NOT NULL,
            scan_type     TEXT NOT NULL DEFAULT 'manual',
            total_scanned INTEGER DEFAULT 0,
            notes         TEXT
        );
        CREATE TABLE IF NOT EXISTS scan_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER NOT NULL DEFAULT 1,
            ticker      TEXT NOT NULL,
            scanned_at  TEXT NOT NULL,
            price       REAL,
            explosion_score REAL,
            recommendation  TEXT,
            confidence      TEXT,
            rsi             REAL,
            macd_signal     TEXT,
            ma_trend        TEXT,
            pattern_sentiment TEXT,
            bullish_score   INTEGER,
            bearish_score   INTEGER,
            fundamental_score REAL,
            raw_data        TEXT,
            catalyst        TEXT,
            short_pct       REAL
        );
        CREATE TABLE IF NOT EXISTS forward_signals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker              TEXT NOT NULL,
            signal_ts           TEXT NOT NULL,
            signal_type         TEXT NOT NULL,
            entry_price         REAL NOT NULL,
            composite_score     REAL,
            catalyst_summary    TEXT,
            supertrend_level    REAL,
            supertrend_atr      REAL,
            ai_verdict          TEXT,
            telegram_sent_at    TEXT,
            price_after_7d      REAL,
            price_after_14d     REAL,
            price_after_30d     REAL,
            return_7d_pct       REAL,
            return_14d_pct      REAL,
            return_30d_pct      REAL,
            status              TEXT NOT NULL DEFAULT 'open',
            data_quality_flag   TEXT,
            fill_price          REAL,
            fill_source         TEXT
        );
        """
    )
    conn.close()
    return _get_conn


def _insert_scan(get_conn, ticker, score, scan_type, catalyst=None, recommendation=None):
    """Insert a scan_runs + scan_results pair with the given scan_type."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO scan_runs (run_at, scan_type) VALUES (?, ?)",
        (datetime.now().isoformat(), scan_type),
    )
    run_id = cur.lastrowid
    conn.execute(
        "INSERT INTO scan_results (run_id, ticker, scanned_at, price, explosion_score, "
        "recommendation, catalyst) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, ticker, datetime.now().isoformat(), score, score, recommendation, catalyst),
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Partial-bar Supertrend drop in _check_ticker
# ─────────────────────────────────────────────────────────────────────────────

class TestPartialBarDrop:
    """The in-progress last bar must be dropped before computing Supertrend."""

    def test_forming_bar_flip_does_not_fire(self):
        """A flip that exists ONLY because of the forming last bar must not fire.

        Build a bullish uptrend on the closed bars (Supertrend stays bullish,
        no flip), then append a sharp DOWN spike as the forming bar. Without the
        drop, the spike flips Supertrend bearish on the last bar and fires a SELL.
        With the drop (the fix), the forming bar is discarded → no flip → None.
        """
        from src.ibkr_worker import _check_ticker

        n = 40
        # Steady bullish trend across the closed bars.
        close = np.linspace(100, 130, n)
        df = pd.DataFrame(
            {
                "Open": close,
                "High": close + 0.5,
                "Low": close - 0.5,
                "Close": close,
                "Volume": np.ones(n) * 1_000_000,
            },
            index=pd.date_range("2026-01-05 09:00", periods=n, freq="h"),  # Monday
        )
        # Forming bar: violent crash far below the lower band → would flip bearish.
        df.iloc[-1, df.columns.get_loc("Low")] = 60.0
        df.iloc[-1, df.columns.get_loc("Close")] = 60.0
        df.iloc[-1, df.columns.get_loc("Open")] = 100.0

        mock_conn = MagicMock()
        mock_conn.historical_bars.return_value = df

        with patch("src.ibkr_worker._is_signal_hours", return_value=True):
            event = _check_ticker(mock_conn, "TEST")

        # The crash lived only on the forming bar; after dropping it, no flip.
        assert event is None

    def test_closed_bar_flip_fires_once(self):
        """A genuine flip on the last CLOSED bar (bars_ago == 1) fires exactly once."""
        from src.ibkr_worker import _check_ticker

        n = 40
        df = pd.DataFrame(
            {
                "Open": np.linspace(100, 100, n),
                "High": np.linspace(100, 100, n),
                "Low": np.linspace(100, 100, n),
                "Close": np.linspace(100, 100, n),
                "Volume": np.ones(n) * 1_000_000,
            },
            index=pd.date_range("2026-01-05 09:00", periods=n, freq="h"),  # Monday
        )
        mock_conn = MagicMock()
        mock_conn.historical_bars.return_value = df

        # Mock supertrend so the flip is on the last (closed, after-drop) bar.
        st_result = {"direction": "Bullish", "signal": "BUY", "level": 99.0, "bars_ago": 1}
        with patch("src.ibkr_worker.supertrend", return_value=st_result) as mock_st, \
             patch("src.ibkr_worker._is_signal_hours", return_value=True):
            event = _check_ticker(mock_conn, "TEST")

        assert event is not None
        assert event.signal == "BUY"
        assert event.bars_ago == 1
        # Verify the partial bar was dropped before supertrend was called:
        called_df = mock_st.call_args[0][0]
        assert len(called_df) == n - 1

    def test_stale_flip_still_rejected_after_drop(self):
        """bars_ago == 2 (older closed bar) must still be rejected as stale."""
        from src.ibkr_worker import _check_ticker

        n = 40
        df = pd.DataFrame(
            {"Open": np.ones(n) * 100, "High": np.ones(n) * 101,
             "Low": np.ones(n) * 99, "Close": np.ones(n) * 100,
             "Volume": np.ones(n) * 1_000_000},
            index=pd.date_range("2026-01-05 09:00", periods=n, freq="h"),
        )
        mock_conn = MagicMock()
        mock_conn.historical_bars.return_value = df

        st_result = {"direction": "Bullish", "signal": "BUY", "level": 99.0, "bars_ago": 2}
        with patch("src.ibkr_worker.supertrend", return_value=st_result), \
             patch("src.ibkr_worker._is_signal_hours", return_value=True):
            event = _check_ticker(mock_conn, "TEST")

        assert event is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Weekend gate in _is_signal_hours
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalHoursWeekendGate:

    def _patch_now(self, dt):
        """Patch datetime.now inside ibkr_worker to return a fixed ET datetime."""
        import src.ibkr_worker as w

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return dt

        return patch.object(w, "datetime", _FrozenDateTime)

    def test_saturday_blocked(self):
        from src.ibkr_worker import _is_signal_hours, _ET
        # 2026-07-04 is a Saturday. Midday ET, inside 04:00–20:00.
        sat = datetime(2026, 7, 4, 12, 0, tzinfo=_ET)
        with self._patch_now(sat):
            assert _is_signal_hours() is False

    def test_sunday_blocked(self):
        from src.ibkr_worker import _is_signal_hours, _ET
        # 2026-07-05 is a Sunday.
        sun = datetime(2026, 7, 5, 12, 0, tzinfo=_ET)
        with self._patch_now(sun):
            assert _is_signal_hours() is False

    def test_weekday_in_hours_allowed(self):
        from src.ibkr_worker import _is_signal_hours, _ET
        # 2026-07-06 is a Monday, 12:00 ET.
        mon = datetime(2026, 7, 6, 12, 0, tzinfo=_ET)
        with self._patch_now(mon):
            assert _is_signal_hours() is True

    def test_weekday_out_of_hours_blocked(self):
        from src.ibkr_worker import _is_signal_hours, _ET
        # Monday 02:00 ET — before the 04:00 window.
        mon_early = datetime(2026, 7, 6, 2, 0, tzinfo=_ET)
        with self._patch_now(mon_early):
            assert _is_signal_hours() is False


# ─────────────────────────────────────────────────────────────────────────────
# 3. scan_type filter in _latest_scan_context
# ─────────────────────────────────────────────────────────────────────────────

class TestScanTypeFilter:

    def test_ignores_non_scheduled_scan(self, _db):
        """A catalyst (non-scheduled) row must not be surfaced as composite score."""
        from src.signal_combiner import _latest_scan_context

        # Only a catalyst-scan row exists → explosion_score is a catalyst metric.
        _insert_scan(_db, "ABCD", 95.0, scan_type="catalyst", catalyst="pdufa")

        ctx = _latest_scan_context("ABCD")
        # No 'scheduled' row → nothing returned (composite_score not polluted).
        assert ctx == {}

    def test_uses_scheduled_scan(self, _db):
        """When a scheduled row exists, its composite score is returned."""
        from src.signal_combiner import _latest_scan_context

        _insert_scan(_db, "WXYZ", 68.0, scan_type="scheduled",
                     catalyst="earnings", recommendation="BUY")

        ctx = _latest_scan_context("WXYZ")
        assert ctx.get("composite_score") == 68.0
        assert ctx.get("catalyst") == "earnings"

    def test_prefers_scheduled_over_catalyst(self, _db):
        """A newer catalyst row must NOT override the scheduled composite score."""
        from src.signal_combiner import _latest_scan_context

        _insert_scan(_db, "MNOP", 62.0, scan_type="scheduled", recommendation="BUY")
        # A more-recent catalyst row with a high explosion score.
        _insert_scan(_db, "MNOP", 99.0, scan_type="catalyst", catalyst="pdufa")

        ctx = _latest_scan_context("MNOP")
        assert ctx.get("composite_score") == 62.0  # the scheduled one, not 99.0


# ─────────────────────────────────────────────────────────────────────────────
# 4. Dedup rollback (release_dedup)
# ─────────────────────────────────────────────────────────────────────────────

class TestDedupRollback:

    def test_release_allows_reclaim(self, _db):
        from src.signal_combiner import _try_claim_dedup, release_dedup

        assert _try_claim_dedup("ROLL", "combined_buy", "msg", 10.0, 60.0) is True
        # Second claim blocked by the first row.
        assert _try_claim_dedup("ROLL", "combined_buy", "msg", 10.0, 60.0) is False

        # Roll back → the slot is free again.
        release_dedup("ROLL", "combined_buy")
        assert _try_claim_dedup("ROLL", "combined_buy", "msg", 10.0, 60.0) is True

    def test_release_only_touches_matching_type(self, _db):
        from src.signal_combiner import _try_claim_dedup, release_dedup

        assert _try_claim_dedup("PAIR", "combined_buy", "b", 10.0, 60.0) is True
        assert _try_claim_dedup("PAIR", "combined_sell", "s", 10.0, 60.0) is True

        release_dedup("PAIR", "combined_buy")

        # BUY slot freed, SELL slot untouched.
        assert _try_claim_dedup("PAIR", "combined_buy", "b", 10.0, 60.0) is True
        assert _try_claim_dedup("PAIR", "combined_sell", "s", 10.0, 60.0) is False

    def test_worker_rolls_back_on_send_failure(self, _db, monkeypatch):
        """run_once must call release_dedup when the Telegram send fails."""
        import src.ibkr_worker as w
        from src.signal_combiner import CombinedAlert, _try_claim_dedup

        # Simulate the state after evaluate(): the dedup slot is already claimed.
        _try_claim_dedup("FAILT", "combined_buy", "msg", 12.0, 70.0)

        alert = CombinedAlert(
            ticker="FAILT", alert_type="combined_buy", entry_price=12.0,
            composite_score=70.0, catalyst_summary=None, supertrend_level=11.0,
            message="🟢 BUY — FAILT",
        )

        # Queue with one entry; _check_ticker returns an event; evaluate returns alert.
        fake_entry = MagicMock()
        fake_entry.ticker = "FAILT"

        mock_conn = MagicMock()
        mock_tracker = MagicMock()

        monkeypatch.setattr(w, "build_queue", lambda: [fake_entry])
        monkeypatch.setattr(w, "PositionTracker", lambda conn: mock_tracker)
        monkeypatch.setattr(w, "_check_ticker", lambda conn, t: MagicMock(ticker="FAILT"))
        monkeypatch.setattr(w, "evaluate", lambda ev: alert)
        # Telegram send genuinely FAILS (returns False, not None).
        monkeypatch.setattr(w, "_send_telegram", lambda msg: False)
        # Order submission should never be reached (guard with a raiser).
        monkeypatch.setattr(w, "_submit_order",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("submit reached")))

        fired = w.run_once(mock_conn)

        # No alert counted as fired, and the dedup slot was released → reclaimable.
        assert fired == 0
        assert _try_claim_dedup("FAILT", "combined_buy", "msg", 12.0, 70.0) is True

    def test_worker_does_not_roll_back_when_disabled(self, _db, monkeypatch):
        """A disabled Telegram channel (None) must NOT roll back the dedup claim."""
        import src.ibkr_worker as w
        from src.signal_combiner import CombinedAlert, _try_claim_dedup

        _try_claim_dedup("DISBL", "combined_buy", "msg", 12.0, 70.0)

        alert = CombinedAlert(
            ticker="DISBL", alert_type="combined_buy", entry_price=12.0,
            composite_score=70.0, catalyst_summary=None, supertrend_level=11.0,
            message="🟢 BUY — DISBL",
        )
        mock_conn = MagicMock()
        monkeypatch.setattr(w, "build_queue", lambda: [MagicMock(ticker="DISBL")])
        monkeypatch.setattr(w, "PositionTracker", lambda conn: MagicMock())
        monkeypatch.setattr(w, "_check_ticker", lambda conn, t: MagicMock(ticker="DISBL"))
        monkeypatch.setattr(w, "evaluate", lambda ev: alert)
        # Disabled channel returns None.
        monkeypatch.setattr(w, "_send_telegram", lambda msg: None)
        monkeypatch.setattr(w, "_submit_order", lambda *a, **k: {"status": "PAUSED"})

        w.run_once(mock_conn)

        # Dedup claim preserved → a reclaim attempt is blocked.
        assert _try_claim_dedup("DISBL", "combined_buy", "msg", 12.0, 70.0) is False


# ─────────────────────────────────────────────────────────────────────────────
# 5. Markdown escaping
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkdownEscaping:

    def test_escape_md_underscore(self):
        from src.signal_combiner import _escape_md
        assert _escape_md("sec_8k") == "sec\\_8k"

    def test_escape_md_all_specials(self):
        from src.signal_combiner import _escape_md
        out = _escape_md("a_b*c`d[e")
        assert out == "a\\_b\\*c\\`d\\[e"

    def test_buy_message_escapes_catalyst(self):
        from src.signal_combiner import _format_buy_message, SupertrendEvent
        ev = SupertrendEvent(ticker="ABC", direction="Bullish", signal="BUY",
                             level=48.0, last_price=50.0)
        ctx = {"composite_score": 70.0, "catalyst": "sec_8k", "recommendation": "STRONG_BUY"}
        msg = _format_buy_message(ev, ctx)
        # Lone underscores from catalyst/recommendation are escaped.
        assert "sec\\_8k" in msg
        assert "STRONG\\_BUY" in msg
        assert "sec_8k" not in msg.replace("sec\\_8k", "")

    def test_sell_message_escapes_recommendation(self):
        from src.signal_combiner import _format_sell_message, SupertrendEvent
        ev = SupertrendEvent(ticker="XY_Z", direction="Bearish", signal="SELL",
                             level=52.0, last_price=50.0)
        ctx = {"composite_score": 40.0, "recommendation": "take_profit"}
        msg = _format_sell_message(ev, ctx)
        assert "XY\\_Z" in msg
        assert "take\\_profit" in msg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
