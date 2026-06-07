"""
Tests for:
  1. bars_ago filter in _check_ticker (ibkr_worker.py)
  2. _try_claim_dedup atomicity (signal_combiner.py)
  3. BEAR regime veto in check_hard_vetos (execution_engine.py)
  4. forward_signals data quality flag for IBKR placeholder price
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Shared DB fixture ─────────────────────────────────────────────────────────

@pytest.fixture()
def _db(tmp_path, monkeypatch):
    """
    File-backed test DB redirected from src.database.get_connection.
    Creates all tables required by signal_combiner and forward_signals.
    Returns the factory callable.
    """
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
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watchlist_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            alert_type  TEXT NOT NULL,
            message     TEXT,
            sent_at     TEXT NOT NULL,
            score       REAL,
            price       REAL
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
    """)
    conn.close()
    return _get_conn


# ─────────────────────────────────────────────────────────────────────────────
# 1. bars_ago filter in _check_ticker
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckTickerBarsAgo:
    """_check_ticker must only fire when bars_ago == 1 (fresh flip)."""

    def _make_fake_df(self):
        """Return a minimal DataFrame that satisfies len >= ATR_PERIOD + 2."""
        import pandas as pd
        import numpy as np
        n = 20
        dates = pd.date_range("2026-01-01", periods=n, freq="h")
        close = np.linspace(100, 105, n)
        return pd.DataFrame(
            {"Open": close, "High": close + 1, "Low": close - 1, "Close": close,
             "Volume": np.ones(n) * 1_000_000},
            index=dates,
        )

    def test_bars_ago_1_fires(self):
        """supertrend returns bars_ago=1 with a BUY signal → event returned."""
        from src.ibkr_worker import _check_ticker

        fake_df = self._make_fake_df()
        mock_conn = MagicMock()
        mock_conn.historical_bars.return_value = fake_df

        supertrend_result = {
            "direction": "Bullish",
            "signal": "BUY",
            "level": 99.5,
            "bars_ago": 1,
        }
        with patch("src.ibkr_worker.supertrend", return_value=supertrend_result):
            event = _check_ticker(mock_conn, "TEST")

        assert event is not None
        assert event.ticker == "TEST"
        assert event.signal == "BUY"
        assert event.bars_ago == 1

    def test_bars_ago_2_filtered(self):
        """supertrend returns bars_ago=2 → stale flip, _check_ticker returns None."""
        from src.ibkr_worker import _check_ticker

        fake_df = self._make_fake_df()
        mock_conn = MagicMock()
        mock_conn.historical_bars.return_value = fake_df

        supertrend_result = {
            "direction": "Bullish",
            "signal": "BUY",
            "level": 99.5,
            "bars_ago": 2,
        }
        with patch("src.ibkr_worker.supertrend", return_value=supertrend_result):
            event = _check_ticker(mock_conn, "TEST")

        assert event is None

    def test_no_flip_filtered(self):
        """supertrend returns signal=None, bars_ago=0 → no event."""
        from src.ibkr_worker import _check_ticker

        fake_df = self._make_fake_df()
        mock_conn = MagicMock()
        mock_conn.historical_bars.return_value = fake_df

        supertrend_result = {
            "direction": "Bullish",
            "signal": None,
            "level": 99.5,
            "bars_ago": 0,
        }
        with patch("src.ibkr_worker.supertrend", return_value=supertrend_result):
            event = _check_ticker(mock_conn, "TEST")

        assert event is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. _try_claim_dedup
# ─────────────────────────────────────────────────────────────────────────────

class TestTryClaimDedup:
    """_try_claim_dedup must be atomic: first call claims, second is rejected."""

    def test_first_call_wins(self, _db):
        from src.signal_combiner import _try_claim_dedup

        first = _try_claim_dedup("AAPL", "combined_buy", "msg", 150.0, 72.0)
        second = _try_claim_dedup("AAPL", "combined_buy", "msg", 150.0, 72.0)

        assert first is True
        assert second is False

    def test_different_type_both_win(self, _db):
        """combined_buy and combined_sell are separate dedup keys for the same ticker."""
        from src.signal_combiner import _try_claim_dedup

        result_buy = _try_claim_dedup("AAPL", "combined_buy", "buy msg", 150.0, 72.0)
        result_sell = _try_claim_dedup("AAPL", "combined_sell", "sell msg", 148.0, 30.0)

        assert result_buy is True
        assert result_sell is True

    def test_after_expiry_wins_again(self, _db):
        """An expired row (> 24h old) should not block a new claim."""
        from src.signal_combiner import _try_claim_dedup

        # Insert a row that is 25 hours old directly into the DB
        old_ts = (datetime.now() - timedelta(hours=25)).isoformat()
        conn = _db()
        conn.execute(
            "INSERT INTO watchlist_alerts (ticker, alert_type, message, sent_at, score, price) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("TSLA", "combined_buy", "old msg", old_ts, 65.0, 200.0),
        )
        conn.commit()
        conn.close()

        result = _try_claim_dedup("TSLA", "combined_buy", "new msg", 205.0, 66.0)

        assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. execution_engine BEAR veto
# ─────────────────────────────────────────────────────────────────────────────

class TestBearVeto:
    """In BEAR regime, BUY is blocked; SELL (exits) must be allowed through."""

    def _bear_regime(self):
        return {
            "regime": "BEAR",
            "vix": 32.0,
            "spy_price": 480.0,
            "spy_sma200": 510.0,
            "spy_vs_sma200_pct": -5.9,
            "multiplier": 0.5,
            "cached_at": "2026-01-01T00:00:00",
        }

    def _score_data(self):
        return {
            "score": 70,
            "rsi": 55,
            "macd_signal": "bullish",
            "ma_trend": "uptrend",
            "volume_ratio": 1.5,
            "si_pct": 5.0,
            "fundamentals_score": 7.0,
            "dcf_score": 8.0,
            "catalyst_type": "",
            "days_to_event": 999,
            "explosion_score": 0,
            "price": 100.0,
            "avg_volume": 1_000_000,
        }

    def test_bear_veto_blocks_buy(self):
        from src.execution_engine import check_hard_vetos

        veto = check_hard_vetos(
            ticker="TEST",
            price=100.0,
            atr=2.0,
            score_data=self._score_data(),
            regime=self._bear_regime(),
            signal_type="BUY",
        )

        assert veto["passed"] is False
        assert "BEAR" in veto["reason"]

    def test_bear_veto_allows_sell(self):
        from src.execution_engine import check_hard_vetos

        veto = check_hard_vetos(
            ticker="TEST",
            price=100.0,
            atr=2.0,
            score_data=self._score_data(),
            regime=self._bear_regime(),
            signal_type="SELL",
        )

        # BEAR regime should NOT veto a SELL — exits are always allowed
        assert veto["passed"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 4. forward_signals data quality guard
# ─────────────────────────────────────────────────────────────────────────────

class TestForwardSignalsDataQuality:
    """record_signal must flag entry_price=105.0 as SUSPECT in the DB."""

    def test_record_signal_flags_ibkr_placeholder(self, _db):
        from src.forward_signals import SignalRecord, record_signal

        rec = SignalRecord(
            ticker="PLUG",
            signal_type="BUY",
            entry_price=105.0,          # known IBKR paper-account placeholder
            composite_score=68.0,
            catalyst_summary="earnings tomorrow",
            supertrend_level=103.5,
        )

        signal_id = record_signal(rec)
        assert signal_id is not None and signal_id > 0

        conn = _db()
        row = conn.execute(
            "SELECT data_quality_flag FROM forward_signals WHERE id = ?",
            (signal_id,),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["data_quality_flag"] == "SUSPECT"
