"""
Tests for src/challenge_portfolio.py — the standalone $10K Challenge paper
trading simulator (see that module's docstring for the strategy rules and
evidence basis).

No network calls anywhere in this suite: every yfinance/Massive touch point
(_fetch_history, compute_atr, fetch_latest_close, get_insider_transactions,
scan_supertrend_universe, get_index) is monkeypatched. The point of this
suite is the money-math and decision logic — cash accounting, position
sizing, the insider fail-closed contract, and the exit/entry rules — not
data-fetching, which the rest of this codebase already tests elsewhere.

Run:
    python -m pytest tests/test_challenge_portfolio.py -v
"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def temp_db(monkeypatch):
    """Real init_db() schema on a throwaway file — mirrors the fixture used
    across this project's other DB-backed test suites (e.g.
    tests/test_catalyst_event_study_watch_signals.py)."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="challenge_")
    import os
    os.close(fd)
    db_path = Path(path)
    import src.database as db
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield db_path
    try:
        db_path.unlink()
    except Exception:
        pass


@pytest.fixture
def cp(temp_db):
    """Reload the module under test fresh per-test so no state leaks."""
    import importlib
    import src.challenge_portfolio as mod
    importlib.reload(mod)
    return mod


# ── Cash accounting ─────────────────────────────────────────────────────

class TestCashAccounting:
    def test_starts_at_10000_with_no_trades(self, cp):
        assert cp.get_cash_balance() == cp.STARTING_CASH

    def test_buy_reduces_cash(self, cp):
        cp._record_trade("AAPL", "BUY", 10, 100.0, reason="entry_signal", stop_price=90.0)
        assert cp.get_cash_balance() == pytest.approx(cp.STARTING_CASH - 1000.0)

    def test_sell_increases_cash_back(self, cp):
        cp._record_trade("AAPL", "BUY", 10, 100.0, reason="entry_signal", stop_price=90.0)
        cp._record_trade("AAPL", "SELL", 10, 120.0, reason="stop_loss")
        assert cp.get_cash_balance() == pytest.approx(cp.STARTING_CASH - 1000.0 + 1200.0)

    def test_multiple_trades_never_drift(self, cp):
        cp._record_trade("AAPL", "BUY", 5, 200.0, reason="entry_signal", stop_price=180.0)
        cp._record_trade("MSFT", "BUY", 2, 400.0, reason="entry_signal", stop_price=360.0)
        cp._record_trade("AAPL", "SELL", 5, 210.0, reason="time_stop")
        expected = cp.STARTING_CASH - 1000.0 - 800.0 + 1050.0
        assert cp.get_cash_balance() == pytest.approx(expected)


# ── Position sizing ─────────────────────────────────────────────────────

class TestSizeEntry:
    def test_zero_when_stop_at_or_above_price(self, cp):
        assert cp._size_entry(price=20.0, stop=20.0, cash=10_000, total_equity=10_000) == 0
        assert cp._size_entry(price=20.0, stop=21.0, cash=10_000, total_equity=10_000) == 0

    def test_budget_cap_binds_on_tight_stop(self, cp):
        # equity=10000, target_value=2000, price=20 -> budget-capped at 100 shares
        # risk_budget=200, stop distance=0.5 -> risk-capped at 400 shares
        # budget cap (100) binds tighter.
        shares = cp._size_entry(price=20.0, stop=19.5, cash=10_000, total_equity=10_000)
        assert shares == 100

    def test_risk_cap_binds_on_wide_stop(self, cp):
        # equity=10000, target_value=2000, price=20 -> budget-capped at 100 shares
        # risk_budget=200, stop distance=10 -> risk-capped at 20 shares
        # risk cap (20) binds tighter.
        shares = cp._size_entry(price=20.0, stop=10.0, cash=10_000, total_equity=10_000)
        assert shares == 20

    def test_never_exceeds_available_cash(self, cp):
        shares = cp._size_entry(price=20.0, stop=15.0, cash=50.0, total_equity=10_000)
        assert shares == 2  # floor(50/20), even though risk/target budget allow far more

    def test_uses_max_positions_denominator_not_open_slots(self, cp):
        # target_value is always total_equity/MAX_POSITIONS regardless of how
        # many slots happen to be free right now -- avoids overallocating a
        # single late-cycle slot to a full 1/1 share of equity.
        assert cp.MAX_POSITIONS == 5
        shares = cp._size_entry(price=100.0, stop=90.0, cash=10_000, total_equity=10_000)
        # target_value = 10000/5 = 2000 -> budget cap = 20 shares
        # risk_budget = 200, stop distance=10 -> risk cap = 20 shares
        assert shares == 20


# ── Insider filter (fail-closed contract) ───────────────────────────────

class TestHasRecentInsiderBuy:
    def test_true_when_a_buy_is_present(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "get_insider_transactions",
                             lambda ticker, days: [{"type": "SELL"}, {"type": "BUY"}])
        assert cp.has_recent_insider_buy("XYZ") is True

    def test_false_when_only_sells(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "get_insider_transactions",
                             lambda ticker, days: [{"type": "SELL"}, {"type": "SELL"}])
        assert cp.has_recent_insider_buy("XYZ") is False

    def test_false_when_empty(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "get_insider_transactions", lambda ticker, days: [])
        assert cp.has_recent_insider_buy("XYZ") is False

    def test_fails_closed_on_exception(self, cp, monkeypatch):
        """The one behavior this strategy's entire evidence basis depends on:
        a lookup failure (or missing MASSIVE_API_KEY, which get_insider_
        transactions already handles by returning []) must NEVER be treated
        as 'passes the filter' -- that would silently trade the unfiltered
        Supertrend-flip signal already shown to have ~0% edge."""
        def _boom(ticker, days):
            raise RuntimeError("network error")
        monkeypatch.setattr(cp, "get_insider_transactions", _boom)
        assert cp.has_recent_insider_buy("XYZ") is False

    def test_passes_configured_lookback_window(self, cp, monkeypatch):
        seen = {}
        def _capture(ticker, days):
            seen["days"] = days
            return []
        monkeypatch.setattr(cp, "get_insider_transactions", _capture)
        cp.has_recent_insider_buy("XYZ")
        assert seen["days"] == cp.INSIDER_LOOKBACK_DAYS == 45


# ── Exit logic ───────────────────────────────────────────────────────────

class TestCheckExits:
    def test_stop_loss_triggers_sell_and_clears_position(self, cp, monkeypatch):
        cp._upsert_position("AAPL", 10, 100.0, stop_price=95.0, opened_date=cp._today())
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: 94.0)
        events = cp.check_exits()
        assert len(events) == 1
        assert events[0] == {"ticker": "AAPL", "action": "SELL", "reason": "stop_loss",
                              "price": 94.0, "shares": 10}
        assert cp.get_open_positions() == []

    def test_time_stop_triggers_after_30_days(self, cp, monkeypatch):
        old_date = (date.today() - timedelta(days=30)).isoformat()
        cp._upsert_position("AAPL", 10, 100.0, stop_price=50.0, opened_date=old_date)
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: 105.0)  # well above stop
        events = cp.check_exits()
        assert len(events) == 1
        assert events[0]["reason"] == "time_stop"
        assert cp.get_open_positions() == []

    def test_no_exit_before_time_stop_and_above_stop(self, cp, monkeypatch):
        recent_date = (date.today() - timedelta(days=5)).isoformat()
        cp._upsert_position("AAPL", 10, 100.0, stop_price=90.0, opened_date=recent_date)
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: 105.0)
        monkeypatch.setattr(cp, "compute_atr", lambda t: 2.0)  # no trail movement forced
        events = cp.check_exits()
        assert events == []
        assert len(cp.get_open_positions()) == 1

    def test_trailing_stop_only_ever_rises(self, cp, monkeypatch):
        recent_date = (date.today() - timedelta(days=5)).isoformat()
        cp._upsert_position("AAPL", 10, 100.0, stop_price=90.0, opened_date=recent_date)
        # price=120, ATR=2.5*2.5=implied stop 120-2.5*2=115 > 90 -> should trail up to 115
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: 120.0)
        monkeypatch.setattr(cp, "compute_atr", lambda t: 2.0)
        cp.check_exits()
        pos = cp.get_open_positions()[0]
        assert pos.stop_price == pytest.approx(120.0 - cp.STOP_ATR_MULT * 2.0)

        # Next cycle: price ticks up to 118 (still above the now-115 stop) but
        # ATR widens enough that the naive implied stop (118-2.5*3=110.5) would
        # be LOWER than the existing 115 -- the trail must not lower it.
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: 118.0)
        monkeypatch.setattr(cp, "compute_atr", lambda t: 3.0)  # implied stop 110.5 < 115
        cp.check_exits()
        pos = cp.get_open_positions()[0]
        assert pos.stop_price == pytest.approx(120.0 - cp.STOP_ATR_MULT * 2.0)  # unchanged, not lowered

    def test_price_fetch_failure_skips_without_crashing(self, cp, monkeypatch):
        cp._upsert_position("AAPL", 10, 100.0, stop_price=90.0, opened_date=cp._today())
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: None)
        events = cp.check_exits()
        assert events == []
        assert len(cp.get_open_positions()) == 1  # untouched, not force-closed


# ── Market-cap / no-coverage filter ─────────────────────────────────────

class TestPassesMarketCapFilter:
    def test_passes_small_cap_no_coverage(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "_yf_info", lambda t, ttl=3600: {
            "marketCap": 500_000_000, "forwardPE": None,
        })
        assert cp._passes_market_cap_filter("SMALL") is True

    def test_excluded_when_market_cap_too_large(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "_yf_info", lambda t, ttl=3600: {
            "marketCap": 3_000_000_000_000, "forwardPE": None,
        })
        assert cp._passes_market_cap_filter("MEGA") is False

    def test_excluded_when_market_cap_missing_or_zero(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "_yf_info", lambda t, ttl=3600: {"marketCap": 0})
        assert cp._passes_market_cap_filter("UNKNOWN") is False

    def test_excluded_when_analyst_coverage_present(self, cp, monkeypatch):
        # positive forwardPE is used as an "has analyst coverage" proxy,
        # matching insider_cluster_scanner.py's identical convention
        monkeypatch.setattr(cp, "_yf_info", lambda t, ttl=3600: {
            "marketCap": 500_000_000, "forwardPE": 18.5,
        })
        assert cp._passes_market_cap_filter("COVERED") is False

    def test_fails_closed_on_exception(self, cp, monkeypatch):
        def _boom(t, ttl=3600):
            raise RuntimeError("network error")
        monkeypatch.setattr(cp, "_yf_info", _boom)
        assert cp._passes_market_cap_filter("XYZ") is False


# ── Entry filtering ──────────────────────────────────────────────────────

class TestFindEntries:
    def test_returns_empty_when_no_open_slots(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "_load_universe", lambda: ["AAPL"])
        assert cp.find_entries(open_slots=0) == []

    def test_filters_below_min_price(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "_load_universe", lambda: ["PENNY", "GOOD"])
        monkeypatch.setattr(cp, "scan_supertrend_universe", lambda tickers: [
            {"ticker": "PENNY", "price": 1.0, "level": 0.9, "avg_volume": 1_000_000},
            {"ticker": "GOOD", "price": 50.0, "level": 45.0, "avg_volume": 500_000},
        ])
        monkeypatch.setattr(cp, "_passes_market_cap_filter", lambda t: True)
        monkeypatch.setattr(cp, "has_recent_insider_buy", lambda t: True)
        result = cp.find_entries(open_slots=5)
        assert [r["ticker"] for r in result] == ["GOOD"]

    def test_excludes_already_held_tickers(self, cp, monkeypatch):
        cp._upsert_position("GOOD", 10, 40.0, stop_price=35.0, opened_date=cp._today())
        monkeypatch.setattr(cp, "_load_universe", lambda: ["GOOD"])
        monkeypatch.setattr(cp, "scan_supertrend_universe", lambda tickers: [
            {"ticker": "GOOD", "price": 50.0, "level": 45.0, "avg_volume": 500_000},
        ])
        monkeypatch.setattr(cp, "_passes_market_cap_filter", lambda t: True)
        monkeypatch.setattr(cp, "has_recent_insider_buy", lambda t: True)
        assert cp.find_entries(open_slots=5) == []

    def test_excludes_candidates_without_insider_buy(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "_load_universe", lambda: ["A", "B"])
        monkeypatch.setattr(cp, "scan_supertrend_universe", lambda tickers: [
            {"ticker": "A", "price": 50.0, "level": 45.0, "avg_volume": 500_000},
            {"ticker": "B", "price": 50.0, "level": 45.0, "avg_volume": 900_000},
        ])
        monkeypatch.setattr(cp, "_passes_market_cap_filter", lambda t: True)
        monkeypatch.setattr(cp, "has_recent_insider_buy", lambda t: t == "B")
        result = cp.find_entries(open_slots=5)
        assert [r["ticker"] for r in result] == ["B"]

    def test_excludes_candidates_failing_market_cap_filter(self, cp, monkeypatch):
        """A mega-cap flip with insider buying must still be excluded --
        this is the exact gap the 2026-09-11 external-verification pass
        found and fixed (see module docstring)."""
        monkeypatch.setattr(cp, "_load_universe", lambda: ["MEGA", "SMALL"])
        monkeypatch.setattr(cp, "scan_supertrend_universe", lambda tickers: [
            {"ticker": "MEGA", "price": 200.0, "level": 190.0, "avg_volume": 5_000_000},
            {"ticker": "SMALL", "price": 50.0, "level": 45.0, "avg_volume": 300_000},
        ])
        monkeypatch.setattr(cp, "_passes_market_cap_filter", lambda t: t == "SMALL")
        monkeypatch.setattr(cp, "has_recent_insider_buy", lambda t: True)
        result = cp.find_entries(open_slots=5)
        assert [r["ticker"] for r in result] == ["SMALL"]

    def test_caps_at_open_slots_ranked_by_volume(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "_load_universe", lambda: ["A", "B", "C"])
        monkeypatch.setattr(cp, "scan_supertrend_universe", lambda tickers: [
            {"ticker": "A", "price": 50.0, "level": 45.0, "avg_volume": 100},
            {"ticker": "B", "price": 50.0, "level": 45.0, "avg_volume": 300},
            {"ticker": "C", "price": 50.0, "level": 45.0, "avg_volume": 200},
        ])
        monkeypatch.setattr(cp, "_passes_market_cap_filter", lambda t: True)
        monkeypatch.setattr(cp, "has_recent_insider_buy", lambda t: True)
        result = cp.find_entries(open_slots=2)
        assert [r["ticker"] for r in result] == ["B", "C"]


# ── Entry execution / max-positions enforcement ─────────────────────────

class TestExecuteEntries:
    def test_never_exceeds_max_positions(self, cp, monkeypatch):
        # 4 already open, 1 slot free, but 3 candidates offered -- only 1 may fill.
        for i in range(4):
            cp._upsert_position(f"HOLD{i}", 10, 50.0, stop_price=45.0, opened_date=cp._today())
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: 50.0)
        monkeypatch.setattr(cp, "compute_atr", lambda t: 2.0)
        candidates = [
            {"ticker": "A", "price": 50.0, "level": 45.0, "avg_volume": 100},
            {"ticker": "B", "price": 50.0, "level": 45.0, "avg_volume": 100},
            {"ticker": "C", "price": 50.0, "level": 45.0, "avg_volume": 100},
        ]
        events = cp.execute_entries(candidates)
        assert len(events) == 1
        assert len(cp.get_open_positions()) == cp.MAX_POSITIONS

    def test_skips_candidate_when_atr_unavailable(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: 50.0)
        monkeypatch.setattr(cp, "compute_atr", lambda t: None)
        events = cp.execute_entries([{"ticker": "A", "price": 50.0, "level": 45.0, "avg_volume": 100}])
        assert events == []
        assert cp.get_open_positions() == []

    def test_records_trade_and_position_on_fill(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: 50.0)
        monkeypatch.setattr(cp, "compute_atr", lambda t: 2.0)
        events = cp.execute_entries([{"ticker": "A", "price": 50.0, "level": 45.0, "avg_volume": 100}])
        assert len(events) == 1
        positions = cp.get_open_positions()
        assert len(positions) == 1
        assert positions[0].ticker == "A"
        assert positions[0].stop_price == pytest.approx(50.0 - cp.STOP_ATR_MULT * 2.0)
        assert cp.get_cash_balance() < cp.STARTING_CASH


# ── End-to-end daily cycle ────────────────────────────────────────────────

class TestRunDailyCycle:
    def test_full_cycle_with_no_positions_and_one_entry(self, cp, monkeypatch):
        monkeypatch.setattr(cp, "_load_universe", lambda: ["A"])
        monkeypatch.setattr(cp, "scan_supertrend_universe", lambda tickers: [
            {"ticker": "A", "price": 50.0, "level": 45.0, "avg_volume": 100},
        ])
        monkeypatch.setattr(cp, "_passes_market_cap_filter", lambda t: True)
        monkeypatch.setattr(cp, "has_recent_insider_buy", lambda t: True)
        monkeypatch.setattr(cp, "fetch_latest_close", lambda t: 50.0)
        monkeypatch.setattr(cp, "compute_atr", lambda t: 2.0)

        result = cp.run_daily_cycle()
        assert result["exits"] == []
        assert len(result["entries"]) == 1
        assert result["snapshot"]["open_positions"] == 1
        assert result["snapshot"]["total_equity"] == pytest.approx(cp.STARTING_CASH, rel=1e-6)

    def test_report_generation_does_not_crash_when_empty(self, cp):
        report = cp.generate_report()
        assert "$10K Challenge" in report
        assert "none" in report.lower()
