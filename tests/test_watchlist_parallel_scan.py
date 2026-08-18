"""
Test: scan_watchlist()/scan_portfolio() score tickers concurrently (Phase 1)
while keeping alert-checking, DB writes, and Telegram sends fully sequential
in the original ticker order (Phase 2).

Background (2026-08-18): scan_watchlist() looped over the watchlist calling
score_stock() one ticker at a time — ~7-10s/ticker, ~6-7 minutes for a
50-ticker watchlist, measured live. Parallelizing this required first fixing
two module-level shared `requests.Session()` singletons (sec_api_client.py,
insider_tracker.py) reachable from every score_stock() call — see
tests/test_thread_local_sessions.py and CLAUDE.md Incident Archive
2026-08-18 for why that was a prerequisite, not optional hardening.

These tests deliberately exercise _score_all_parallel() directly for the
concurrency mechanics (order independence, failure isolation, actual
speedup, worker-count bound) rather than routing everything through the
full scan_watchlist() pipeline — that keeps each test focused on one
property instead of re-testing alert logic already covered by
tests/test_price_alert_fixes.py and tests/test_recent_features.py. One
integration test at the end runs the real scan_watchlist() end-to-end
against a temp DB to confirm Phase 2's DB-write path survived the refactor.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_watchlist_parallel_scan.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.watchlist_manager as wm  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="watchlist_parallel_")
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


# ── _score_all_parallel: the concurrency mechanics ────────────────────────────

def test_parallel_scan_matches_sequential_result_set():
    """Result set (ticker -> result) must be identical to what a plain
    sequential loop would produce — parallelism must not drop or duplicate
    tickers."""
    tickers = [f"T{i}" for i in range(12)]

    def fake_score(ticker, forecast_days=30):
        return {"ticker": ticker, "score": len(ticker), "price": 10.0}

    with patch.object(wm, "score_stock", side_effect=fake_score):
        parallel_result = wm._score_all_parallel(tickers, max_workers=4)
        sequential_result = {t: fake_score(t) for t in tickers}

    assert parallel_result == sequential_result


def test_result_order_matches_watchlist_order_in_scan_watchlist():
    """Phase 1 completion order is not guaranteed (fast tickers finish before
    slow ones), but scan_watchlist()'s OUTPUT must still follow the original
    watchlist order — proving Phase 2's loop, not completion order, drives
    the result list."""
    items = [
        {"ticker": "SLOW", "alert_score": 60, "alert_pct": 5.0, "price_above": None, "price_below": None},
        {"ticker": "FAST", "alert_score": 60, "alert_pct": 5.0, "price_above": None, "price_below": None},
    ]

    def fake_score(ticker, forecast_days=30):
        if ticker == "SLOW":
            time.sleep(0.15)  # finishes AFTER "FAST" despite being listed first
        return {"ticker": ticker, "score": 50.0, "price": 10.0}

    with patch.object(wm, "watchlist_get_all", return_value=items), \
         patch.object(wm, "score_stock", side_effect=fake_score), \
         patch.object(wm, "get_last_saved_score", return_value=None), \
         patch.object(wm, "_cooldown_passed", return_value=True), \
         patch.object(wm, "_last_alert_price", return_value=None), \
         patch.object(wm, "watchlist_save_alert"), \
         patch.object(wm, "TelegramNotifier"):
        results = wm.scan_watchlist(max_workers=2)

    assert [r["ticker"] for r in results] == ["SLOW", "FAST"]


def test_single_ticker_failure_does_not_drop_others():
    """One ticker's score_stock() raising must not abort the batch — the
    other tickers must still be scored, matching the old inline
    try/except-per-ticker behavior. (Log output isn't asserted here — this
    codebase's tests don't assert on loguru output elsewhere either, and the
    functional behavior below is the actual property under test.)"""
    tickers = ["GOOD1", "BROKEN", "GOOD2"]

    def fake_score(ticker, forecast_days=30):
        if ticker == "BROKEN":
            raise ValueError("boom")
        return {"ticker": ticker, "score": 50.0, "price": 10.0}

    with patch.object(wm, "score_stock", side_effect=fake_score):
        result = wm._score_all_parallel(tickers, max_workers=3, label="Watchlist scan")

    assert result["GOOD1"] is not None
    assert result["GOOD2"] is not None
    assert result["BROKEN"] is None


def test_concurrency_actually_reduces_wall_time():
    """Proves this is really parallel, not just a relabeled sequential
    loop: N tickers at 0.2s each across `max_workers` workers should take
    close to ceil(N/max_workers)*0.2s, not N*0.2s."""
    tickers = [f"T{i}" for i in range(8)]
    max_workers = 4

    def fake_score(ticker, forecast_days=30):
        time.sleep(0.2)
        return {"ticker": ticker, "score": 50.0, "price": 10.0}

    with patch.object(wm, "score_stock", side_effect=fake_score):
        t0 = time.time()
        wm._score_all_parallel(tickers, max_workers=max_workers)
        elapsed = time.time() - t0

    sequential_would_take = len(tickers) * 0.2
    assert elapsed < sequential_would_take * 0.6, (
        f"expected meaningful speedup from parallelism, took {elapsed:.2f}s "
        f"(sequential would be ~{sequential_would_take:.2f}s)"
    )


def test_max_workers_respected():
    """Peak concurrent score_stock() calls must never exceed max_workers,
    even with more tickers in flight than workers available."""
    max_workers = 3
    tickers = [f"T{i}" for i in range(10)]
    concurrent = {"current": 0, "peak": 0}
    lock = threading.Lock()

    def fake_score(ticker, forecast_days=30):
        with lock:
            concurrent["current"] += 1
            concurrent["peak"] = max(concurrent["peak"], concurrent["current"])
        time.sleep(0.05)
        with lock:
            concurrent["current"] -= 1
        return {"ticker": ticker, "score": 50.0, "price": 10.0}

    with patch.object(wm, "score_stock", side_effect=fake_score):
        wm._score_all_parallel(tickers, max_workers=max_workers)

    assert concurrent["peak"] <= max_workers


def test_empty_ticker_list_returns_empty_dict():
    assert wm._score_all_parallel([]) == {}


# ── Integration: real scan_watchlist() against a temp DB ─────────────────────

def test_scan_watchlist_writes_alerts_correctly_with_parallel_scoring(temp_db):
    """End-to-end sanity check: with real DB writes (not mocked), a fresh
    watchlist ticker scanned via the parallel path still gets its
    price_change baseline recorded — proving Phase 2's DB-write path
    survived the refactor unchanged."""
    from src.database import watchlist_add, watchlist_get_alerts

    watchlist_add("PARATEST", notes="test", alert_score=60, alert_pct=5.0)

    def fake_score(ticker, forecast_days=30):
        return {"ticker": ticker, "score": 45.0, "price": 25.0,
                "rsi": 50, "macd": "Neutral", "short_pct": 5.0, "squeeze_active": False}

    with patch.object(wm, "score_stock", side_effect=fake_score), \
         patch.object(wm, "TelegramNotifier"):
        results = wm.scan_watchlist(max_workers=2)

    assert len(results) == 1
    assert results[0]["ticker"] == "PARATEST"
    alerts = watchlist_get_alerts(ticker="PARATEST", limit=10)
    baseline_rows = [a for a in alerts if a["alert_type"] == "price_change"]
    assert baseline_rows, "price_change baseline must be written on first scan"
