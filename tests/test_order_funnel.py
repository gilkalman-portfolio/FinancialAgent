"""
End-to-end tests for the order funnel:
  signal_combiner → execution_engine → order_manager → ibkr_realtime

All IBKR and yfinance calls are mocked. Uses real SQLite for order_log assertions.
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
    # Patch ibkr_worker too so _update_order_log uses the test DB
    try:
        import src.ibkr_worker  # noqa: F401 — ensure module is loaded before patching
        monkeypatch.setattr("src.ibkr_worker.get_connection", _get_conn)
    except Exception:
        pass

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
            run_id            INTEGER NOT NULL REFERENCES scan_runs(id),
            ticker            TEXT NOT NULL,
            scanned_at        TEXT NOT NULL,
            price             REAL,
            explosion_score   REAL,
            recommendation    TEXT,
            confidence        TEXT,
            rsi               REAL,
            macd_signal       TEXT,
            ma_trend          TEXT,
            pattern_sentiment TEXT,
            bullish_score     INTEGER,
            bearish_score     INTEGER,
            fundamental_score REAL,
            raw_data          TEXT,
            catalyst          TEXT,
            short_pct         REAL
        );
    """)
    conn.close()

    yield _get_conn


@pytest.fixture
def mock_ibkr():
    client = MagicMock()
    client.place_bracket_order.return_value = 12345
    return client


@pytest.fixture
def buy_alert():
    return CombinedAlert(
        ticker="TEST",
        alert_type="combined_buy",
        entry_price=50.0,
        composite_score=72.0,
        catalyst_summary="earnings in 3 days",
        supertrend_level=48.0,
        message="mock message",
    )


def _make_engine_decision(shares=10, entry=50.0, stop=48.0, target=54.0, rr=2.0):
    """Build a minimal TradeDecision dict that order_manager needs."""
    return {
        "ticker": "TEST",
        "track": "A",
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
            "shares": shares,
            "dollar_invested": shares * entry,
            "dollar_risk": shares * (entry - stop),
            "stop_price": stop,
            "target_price": target,
            "rr_ratio": rr,
            "max_pct_cap_applied": False,
            "sector_adj_applied": False,
        },
        "noise_window": False,
        "sector_concentration_pct": 0.0,
        "entry_price": entry,
        "atr": 1.2,
        "signal_ts": "2026-01-01T12:00:00",
    }


def _read_order_log(get_conn):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM order_log ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _patch_engine(engine):
    """Context-manager-free helper: returns a stack of patches for evaluate_trade + normalize."""
    return (
        patch.object(engine, "evaluate_trade", return_value=_make_engine_decision()),
        patch.object(engine, "normalize_score_data", side_effect=lambda x: x),
    )


# ── Tests ───────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_submits_bracket_order(self, mock_ibkr, buy_alert, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        with patch.object(engine, "evaluate_trade", return_value=_make_engine_decision()):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(
                    ibkr_client=mock_ibkr,
                    execution_engine_module=engine,
                    paper_mode=True,
                )
                result = mgr.submit(buy_alert)

        assert result["status"] == "SUBMITTED"
        assert result["order_id"] == 12345
        assert result["shares"] == 10
        assert result["action"] == "BUY"

        msg = result["message"]
        assert "FinancialAgent — BUY TEST" in msg
        assert "Entry: $50.00" in msg
        assert "Shares: 10" in msg
        assert "Stop: $48.00" in msg
        assert "Target: $54.00" in msg
        assert "Cost basis: $500.00" in msg
        assert "Order ID: 12345" in msg

        mock_ibkr.place_bracket_order.assert_called_once_with(
            ticker="TEST", action="BUY", shares=10,
            entry_price=50.0, stop_price=48.0, target_price=54.0,
        )

        rows = _read_order_log(_in_memory_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "SUBMITTED"
        assert rows[0]["ibkr_order_id"] == 12345
        assert rows[0]["ticker"] == "TEST"


class TestVetoed:
    def test_vetoed_by_engine(self, mock_ibkr, buy_alert, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        with patch.object(engine, "evaluate_trade", return_value=None):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(
                    ibkr_client=mock_ibkr,
                    execution_engine_module=engine,
                    paper_mode=True,
                )
                result = mgr.submit(buy_alert)

        assert result["status"] == "VETOED"

        mock_ibkr.place_bracket_order.assert_not_called()

        rows = _read_order_log(_in_memory_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "VETOED"


class TestDailyLossLimit:
    def test_daily_loss_blocks_order(self, mock_ibkr, buy_alert, _in_memory_db):
        """
        With -$500 P&L on a $10k portfolio (5% loss > 2% limit),
        check_daily_loss_limit vetoes, evaluate_trade returns None,
        and order_manager records VETOED.
        """
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        mock_tracker = MagicMock()
        mock_tracker.get_daily_pnl.return_value = -500.0
        mock_tracker.get_portfolio_value.return_value = 10_000.0

        # 1. Verify check_daily_loss_limit directly returns DAILY_LOSS veto
        engine.set_position_tracker(mock_tracker)
        try:
            with patch.object(engine, "_get_max_daily_loss_pct", return_value=0.02):
                veto = engine.check_daily_loss_limit()
            assert not veto["passed"]
            assert "DAILY_LOSS" in veto["reason"]
        finally:
            engine.set_position_tracker(None)

        # 2. Full funnel: engine returns None → order_manager returns VETOED
        with patch.object(engine, "evaluate_trade", return_value=None):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(
                    ibkr_client=mock_ibkr,
                    execution_engine_module=engine,
                    paper_mode=True,
                    position_tracker=mock_tracker,
                )
                result = mgr.submit(buy_alert)

        assert result["status"] == "VETOED"
        mock_ibkr.place_bracket_order.assert_not_called()

        rows = _read_order_log(_in_memory_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "VETOED"


class TestTradingPaused:
    def test_paused_blocks_order(self, mock_ibkr, buy_alert, _in_memory_db):
        import src.order_manager as om
        from src.order_manager import OrderManager

        om.set_paused(True)
        try:
            engine = MagicMock()
            mgr = OrderManager(
                ibkr_client=mock_ibkr,
                execution_engine_module=engine,
                paper_mode=True,
            )
            result = mgr.submit(buy_alert)

            assert result["status"] == "PAUSED"

            mock_ibkr.place_bracket_order.assert_not_called()
            engine.evaluate_trade.assert_not_called()

            rows = _read_order_log(_in_memory_db)
            assert len(rows) == 1
            assert rows[0]["status"] == "PAUSED"
        finally:
            om.set_paused(False)


class TestPaperModeGate:
    def test_live_mode_without_env_raises(self, mock_ibkr, buy_alert, monkeypatch):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        monkeypatch.delenv("IBKR_LIVE", raising=False)

        with patch.object(engine, "evaluate_trade", return_value=_make_engine_decision()):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(
                    ibkr_client=mock_ibkr,
                    execution_engine_module=engine,
                    paper_mode=False,
                )
                with pytest.raises(RuntimeError, match="IBKR_LIVE"):
                    mgr.submit(buy_alert)


class TestSellMessage:
    def test_sell_message_with_position(self, mock_ibkr, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        # Insert a position so SELL message can compute P&L
        conn = _in_memory_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ibkr_positions (
                ticker TEXT PRIMARY KEY, shares REAL, avg_cost REAL,
                unrealized_pnl REAL, market_value REAL, last_synced TEXT
            )
        """)
        conn.execute(
            "INSERT INTO ibkr_positions (ticker, shares, avg_cost, unrealized_pnl, market_value, last_synced) "
            "VALUES ('TEST', 100, 45.00, 500.0, 5000.0, '2026-01-01')"
        )
        conn.commit()
        conn.close()

        sell_alert = CombinedAlert(
            ticker="TEST", alert_type="combined_sell", entry_price=50.0,
            composite_score=30.0, catalyst_summary=None, supertrend_level=52.0,
            message="mock sell",
        )
        decision = _make_engine_decision(shares=50, entry=50.0, stop=52.0, target=46.0)
        with patch.object(engine, "evaluate_trade", return_value=decision):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(
                    ibkr_client=mock_ibkr,
                    execution_engine_module=engine,
                    paper_mode=True,
                )
                result = mgr.submit(sell_alert)

        assert result["status"] == "SUBMITTED"
        msg = result["message"]
        assert "FinancialAgent — SELL TEST" in msg
        assert "Exit: $50.00" in msg
        assert "Shares: 50" in msg
        assert "+$250.00" in msg
        assert "avg cost $45.00" in msg
        assert "50 shares remaining" in msg
        assert "Order ID: 12345" in msg


class TestSellWithoutPosition:
    def test_sell_vetoed_no_position(self, mock_ibkr, _in_memory_db):
        import src.execution_engine as engine
        from src.order_manager import OrderManager

        # Create ibkr_positions table but leave it empty
        conn = _in_memory_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ibkr_positions (
                ticker TEXT PRIMARY KEY, shares REAL, avg_cost REAL,
                unrealized_pnl REAL, market_value REAL, last_synced TEXT
            )
        """)
        conn.commit()
        conn.close()

        mock_tracker = MagicMock()
        mock_tracker.get_current_exposure.return_value = 0.0

        sell_alert = CombinedAlert(
            ticker="TEST", alert_type="combined_sell", entry_price=50.0,
            composite_score=30.0, catalyst_summary=None, supertrend_level=52.0,
            message="mock sell",
        )

        with patch.object(engine, "evaluate_trade", return_value=None) as mock_eval:
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                with patch.object(engine, "_position_tracker", mock_tracker):
                    mgr = OrderManager(
                        ibkr_client=mock_ibkr,
                        execution_engine_module=engine,
                        paper_mode=True,
                        position_tracker=mock_tracker,
                    )
                    result = mgr.submit(sell_alert)

        assert result["status"] == "VETOED"
        mock_ibkr.place_bracket_order.assert_not_called()

        # Verify evaluate_trade was called with signal_type="SELL"
        mock_eval.assert_called_once()
        _, kwargs = mock_eval.call_args
        assert kwargs.get("signal_type") == "SELL"


class TestFillCallback:
    def test_fill_updates_order_log(self, mock_ibkr, buy_alert, _in_memory_db):
        import src.execution_engine as engine
        from src.ibkr_worker import _update_order_log
        from src.order_manager import OrderManager

        with patch.object(engine, "evaluate_trade", return_value=_make_engine_decision()):
            with patch.object(engine, "normalize_score_data", side_effect=lambda x: x):
                mgr = OrderManager(
                    ibkr_client=mock_ibkr,
                    execution_engine_module=engine,
                    paper_mode=True,
                )
                result = mgr.submit(buy_alert)

        assert result["status"] == "SUBMITTED"
        assert result["order_id"] == 12345

        _update_order_log(ibkr_order_id=12345, status="FILLED", fill_price=50.25)

        rows = _read_order_log(_in_memory_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "FILLED"
        assert rows[0]["fill_price"] == 50.25
