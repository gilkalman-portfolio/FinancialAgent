"""
Tests for SELL order-path fixes (audit 2026-07):

  1. SELL places a plain LMT order (no bracket children).
  2. SELL shares are capped to the currently-held position.
  3. SELL skips confluence + R:R vetoes; BUY still enforces them.
  4. Veto reason(s) from evaluate_trade propagate into order_log notes.

All IBKR and yfinance calls are mocked. Uses a real file-backed SQLite DB
for order_log / ibkr_positions assertions (mirrors tests/test_order_funnel.py).
"""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.signal_combiner import CombinedAlert


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch, tmp_path):
    """Redirect every get_connection() call to a shared file-backed test DB."""
    db_path = tmp_path / "test.db"

    def _get_conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    monkeypatch.setattr("src.database.get_connection", _get_conn)
    monkeypatch.setattr("src.order_manager.get_connection", _get_conn)
    monkeypatch.setattr("src.execution_engine.get_connection", _get_conn)

    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ibkr_positions (
            ticker       TEXT PRIMARY KEY,
            shares       REAL,
            avg_cost     REAL,
            unrealized_pnl REAL,
            market_value REAL,
            last_synced  TEXT
        );
        CREATE TABLE IF NOT EXISTS order_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker        TEXT NOT NULL,
            action        TEXT NOT NULL,
            shares        INTEGER NOT NULL,
            entry_price   REAL NOT NULL,
            stop_price    REAL NOT NULL,
            target_price  REAL NOT NULL,
            status        TEXT NOT NULL,
            fill_price    REAL,
            ibkr_order_id INTEGER,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            notes         TEXT
        );
        CREATE TABLE IF NOT EXISTS scan_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at        TEXT NOT NULL,
            scan_type     TEXT NOT NULL DEFAULT 'manual',
            total_scanned INTEGER DEFAULT 0,
            notes         TEXT
        );
        CREATE TABLE IF NOT EXISTS scan_results (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id            INTEGER,
            ticker            TEXT NOT NULL,
            scanned_at        TEXT NOT NULL,
            price             REAL,
            explosion_score   REAL,
            raw_data          TEXT
        );
    """)
    conn.close()

    yield _get_conn


@pytest.fixture
def mock_ibkr():
    client = MagicMock()
    client.place_bracket_order.return_value = 12345
    client.place_limit_order.return_value = 67890
    return client


def _insert_position(get_conn, ticker, shares, avg_cost=45.0):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO ibkr_positions "
        "(ticker, shares, avg_cost, unrealized_pnl, market_value, last_synced) "
        "VALUES (?, ?, ?, 0.0, ?, '2026-01-01')",
        (ticker, shares, avg_cost, shares * avg_cost),
    )
    conn.commit()
    conn.close()


def _read_order_log(get_conn):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM order_log ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _sell_alert(ticker="TEST", price=50.0):
    return CombinedAlert(
        ticker=ticker, alert_type="combined_sell", entry_price=price,
        composite_score=30.0, catalyst_summary=None, supertrend_level=52.0,
        message="mock sell",
    )


def _buy_alert(ticker="TEST", price=50.0):
    return CombinedAlert(
        ticker=ticker, alert_type="combined_buy", entry_price=price,
        composite_score=72.0, catalyst_summary="earnings in 3 days",
        supertrend_level=48.0, message="mock buy",
    )


def _decision(shares=10, entry=50.0, stop=48.0, target=54.0, rr=2.0):
    return {
        "ticker": "TEST", "track": "A",
        "regime": {
            "regime": "BULL", "vix": 18.0, "spy_price": 520.0,
            "spy_sma200": 490.0, "spy_vs_sma200_pct": 6.1,
            "multiplier": 1.0, "cached_at": "2026-01-01T00:00:00",
        },
        "veto": {"passed": True, "reason": ""},
        "confluence": {
            "track": "A",
            "pillars": {"technical": 30, "fundamental": 20, "catalyst": 15, "total": 65},
            "catalyst_weight_boosted": False, "notes": [],
        },
        "sizing": {
            "shares": shares, "dollar_invested": shares * entry,
            "dollar_risk": shares * abs(entry - stop),
            "stop_price": stop, "target_price": target, "rr_ratio": rr,
            "max_pct_cap_applied": False, "sector_adj_applied": False,
        },
        "noise_window": False, "sector_concentration_pct": 0.0,
        "entry_price": entry, "atr": 1.2, "signal_ts": "2026-01-01T12:00:00",
    }


# ── 1. SELL places a plain LMT order, no bracket ────────────────────────────

class TestSellPlainOrder:
    def test_sell_uses_limit_order_not_bracket(self, mock_ibkr, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        _insert_position(_in_memory_db, "TEST", shares=100, avg_cost=45.0)

        with patch.object(engine, "evaluate_trade", return_value=_decision(shares=40)):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(mock_ibkr, engine, paper_mode=True)
                result = mgr.submit(_sell_alert(price=50.0))

        assert result["status"] == "SUBMITTED"
        assert result["action"] == "SELL"
        assert result["order_id"] == 67890

        # SELL always closes the full held position (100), not engine sizing (40).
        # Plain limit order path, NOT bracket.
        mock_ibkr.place_limit_order.assert_called_once_with(
            ticker="TEST", action="SELL", shares=100, limit_price=50.0,
        )
        mock_ibkr.place_bracket_order.assert_not_called()

        # order_log has no phantom stop/target for the SELL
        rows = _read_order_log(_in_memory_db)
        assert rows[0]["status"] == "SUBMITTED"
        assert rows[0]["stop_price"] == 0.0
        assert rows[0]["target_price"] == 0.0

        # SELL message shows no stop/target
        msg = result["message"]
        assert "SELL TEST" in msg
        assert "Stop" not in msg
        assert "Target" not in msg


# ── 2. SELL shares capped to held position ──────────────────────────────────

class TestSellSharesCapped:
    def test_sell_caps_to_held_shares(self, mock_ibkr, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        # Held = 30, but sizing wants 500 → must cap to 30
        _insert_position(_in_memory_db, "TEST", shares=30, avg_cost=45.0)

        with patch.object(engine, "evaluate_trade", return_value=_decision(shares=500)):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(mock_ibkr, engine, paper_mode=True)
                result = mgr.submit(_sell_alert(price=50.0))

        assert result["status"] == "SUBMITTED"
        assert result["shares"] == 30  # capped, not 500
        mock_ibkr.place_limit_order.assert_called_once_with(
            ticker="TEST", action="SELL", shares=30, limit_price=50.0,
        )

    def test_sell_closes_full_position_regardless_of_engine_sizing(self, mock_ibkr, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        # Held = 1000, sizing wants 40. SELL must close the full position (1000),
        # not just 40 — a Supertrend flip means the trend reversed; partial exit
        # leaves 96% of the position exposed with a 24h dedup blocking re-entry.
        _insert_position(_in_memory_db, "TEST", shares=1000, avg_cost=45.0)

        with patch.object(engine, "evaluate_trade", return_value=_decision(shares=40)):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(mock_ibkr, engine, paper_mode=True)
                result = mgr.submit(_sell_alert(price=50.0))

        assert result["shares"] == 1000  # full close, not engine's 40

    def test_sell_vetoed_when_no_shares_held(self, mock_ibkr, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        # No position row → held == 0. Engine decision passes (mocked), but
        # order_manager's own share-cap guard must veto.
        with patch.object(engine, "evaluate_trade", return_value=_decision(shares=40)):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(mock_ibkr, engine, paper_mode=True)
                result = mgr.submit(_sell_alert(price=50.0))

        assert result["status"] == "VETOED"
        mock_ibkr.place_limit_order.assert_not_called()
        mock_ibkr.place_bracket_order.assert_not_called()
        rows = _read_order_log(_in_memory_db)
        assert rows[0]["status"] == "VETOED"
        assert "no open position" in rows[0]["notes"].lower()


# ── 3. SELL skips confluence/R:R; BUY still enforces them ────────────────────

class TestSellSkipsEntryVetoes:
    def test_sell_passes_with_weak_confluence(self):
        """SELL exit with a setup that would fail confluence + R:R must still pass."""
        import src.execution_engine as engine

        weak = {
            "price": 50.0,
            # deliberately terrible entry-quality metrics
            "rsi": 90, "macd_signal": "bearish", "ma_trend": "strong_downtrend",
            "volume_ratio": 0.5, "si_pct": 0, "fundamentals_score": 0,
            "dcf_score": 0, "catalyst_type": "", "days_to_event": 999,
            "explosion_score": 0, "avg_volume": 100,  # tiny liquidity
        }

        with patch.object(engine, "get_regime", return_value={
            "regime": "BULL", "vix": 18.0, "spy_price": 520.0, "spy_sma200": 490.0,
            "spy_vs_sma200_pct": 6.1, "multiplier": 1.0, "cached_at": "x",
        }):
            with patch.object(engine, "_get_atr", return_value=1.0):
                with patch.object(engine, "_get_sector", return_value="Technology"):
                    engine.set_position_tracker(None)
                    try:
                        buy = engine.evaluate_trade("TEST", dict(weak), signal_type="BUY")
                        sell = engine.evaluate_trade("TEST", dict(weak), signal_type="SELL")
                    finally:
                        engine.set_position_tracker(None)

        assert buy is None, "BUY must be vetoed by confluence/liquidity on a weak setup"
        assert sell is not None, "SELL exit must NOT be blocked by entry-quality gates"
        assert sell["ticker"] == "TEST"

    def test_buy_still_vetoed_by_confluence(self):
        """Guard: the SELL exemption must not weaken the BUY path."""
        import src.execution_engine as engine

        reasons: list[str] = []
        weak = {
            "price": 50.0, "rsi": 90, "macd_signal": "bearish",
            "ma_trend": "strong_downtrend", "volume_ratio": 0.5, "si_pct": 0,
            "fundamentals_score": 0, "dcf_score": 0, "catalyst_type": "",
            "days_to_event": 999, "explosion_score": 0, "avg_volume": 10_000_000,
        }
        with patch.object(engine, "get_regime", return_value={
            "regime": "BULL", "vix": 18.0, "spy_price": 520.0, "spy_sma200": 490.0,
            "spy_vs_sma200_pct": 6.1, "multiplier": 1.0, "cached_at": "x",
        }):
            with patch.object(engine, "_get_atr", return_value=1.0):
                with patch.object(engine, "_get_sector", return_value="Technology"):
                    engine.set_position_tracker(None)
                    buy = engine.evaluate_trade(
                        "TEST", dict(weak), signal_type="BUY", reasons_out=reasons
                    )
        assert buy is None
        assert reasons  # a concrete veto reason was captured
        assert any("confluence" in r.lower() for r in reasons)


# ── 4. Veto reason propagates into order_log notes ──────────────────────────

class TestVetoReasonPropagation:
    def test_veto_reason_in_order_log_notes(self, mock_ibkr, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        def _fake_eval(ticker, score_data, reasons_out=None, **kw):
            if reasons_out is not None:
                reasons_out.append("Regime BEAR — exits only, no new longs")
            return None

        with patch.object(engine, "evaluate_trade", side_effect=_fake_eval):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(mock_ibkr, engine, paper_mode=True)
                result = mgr.submit(_buy_alert(price=50.0))

        assert result["status"] == "VETOED"
        assert "BEAR" in result["reason"]

        rows = _read_order_log(_in_memory_db)
        assert rows[0]["status"] == "VETOED"
        assert "BEAR" in rows[0]["notes"]
        # generic fallback must NOT be used when a real reason exists
        assert "hard veto, confluence, or R:R" not in rows[0]["notes"]

    def test_generic_fallback_when_no_reason(self, mock_ibkr, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        # evaluate_trade returns None but appends nothing → generic note
        with patch.object(engine, "evaluate_trade", return_value=None):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(mock_ibkr, engine, paper_mode=True)
                result = mgr.submit(_buy_alert(price=50.0))

        assert result["status"] == "VETOED"
        rows = _read_order_log(_in_memory_db)
        assert "hard veto, confluence, or R:R" in rows[0]["notes"]
