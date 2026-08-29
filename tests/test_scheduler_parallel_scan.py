"""
Test: _run_scan_impl() (scheduler.py's daily full-universe scan) scores
tickers concurrently (Phase 1) while keeping DB writes, auto-exit checks,
and Telegram sends fully sequential in the original source/ticker order
(Phase 2).

Background (2026-08-26): the scheduler's daily scan (~2,463 tickers at
08:30/15:00) scored tickers one at a time via a nested
`for source, tickers in tickers_map.items(): for ticker in tickers:` loop
calling score_stock() inline. Because `schedule` runs every job in a single
thread, a long synchronous run took down every job scheduled after it — the
15:00 run was still executing at 16:12 (72+ min), causing the 15:50
Premarket Gap Alert to silently miss its window (it doesn't queue/retry).
See CLAUDE.md Incident Archive 2026-08-26.

This mirrors tests/test_watchlist_parallel_scan.py exactly, which
parallelized src/watchlist_manager.py's scan_watchlist()/scan_portfolio()
the same way two days earlier and is the template for this fix. The
thread-local HTTP session prerequisite (sec_api_client.py / insider_tracker.py)
that parallelization required is shared infrastructure — already fixed and
covered by tests/test_thread_local_sessions.py, not re-tested here.

These tests exercise _score_scan_universe() directly for the concurrency
mechanics (result parity, failure isolation, actual speedup, worker-count
bound) rather than routing everything through the full _run_scan_impl()
pipeline — that keeps each test focused on one property. Two integration
tests at the end run the real _run_scan_impl() against a temp DB to confirm
Phase 2's ordering and min_score filtering survived the refactor unchanged.

Run:
    python -m pytest tests/test_scheduler_parallel_scan.py -v
"""

from __future__ import annotations

import os
import tempfile
import time
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import scheduler


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="scheduler_parallel_")
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


# ── _score_scan_universe: the concurrency mechanics ───────────────────────────

def test_parallel_scan_matches_sequential_result_set():
    """Result list must be identical (same values, same positions) to what a
    plain sequential loop would produce — parallelism must not drop,
    duplicate, or reorder tickers."""
    tickers = [f"T{i}" for i in range(12)]

    def fake_score(ticker, forecast_days=30):
        return {"ticker": ticker, "score": len(ticker), "price": 10.0}

    with patch.object(scheduler, "score_stock", side_effect=fake_score):
        parallel_result = scheduler._score_scan_universe(tickers, forecast_days=30, max_workers=4)
        sequential_result = [fake_score(t) for t in tickers]

    assert parallel_result == sequential_result


def test_single_ticker_failure_does_not_drop_others():
    """One ticker's score_stock() raising must not abort the batch — the
    other tickers must still be scored, matching the old inline
    try/except-per-ticker behavior, at the correct positions."""
    tickers = ["GOOD1", "BROKEN", "GOOD2"]

    def fake_score(ticker, forecast_days=30):
        if ticker == "BROKEN":
            raise ValueError("boom")
        return {"ticker": ticker, "score": 50.0, "price": 10.0}

    with patch.object(scheduler, "score_stock", side_effect=fake_score):
        result = scheduler._score_scan_universe(tickers, forecast_days=30, max_workers=3)

    assert result[0] is not None and result[0]["ticker"] == "GOOD1"
    assert result[1] is None
    assert result[2] is not None and result[2]["ticker"] == "GOOD2"


def test_concurrency_actually_reduces_wall_time():
    """Proves this is really parallel, not just a relabeled sequential
    loop: N tickers at 0.2s each across `max_workers` workers should take
    close to ceil(N/max_workers)*0.2s, not N*0.2s."""
    tickers = [f"T{i}" for i in range(8)]
    max_workers = 4

    def fake_score(ticker, forecast_days=30):
        time.sleep(0.2)
        return {"ticker": ticker, "score": 50.0, "price": 10.0}

    with patch.object(scheduler, "score_stock", side_effect=fake_score):
        t0 = time.time()
        scheduler._score_scan_universe(tickers, forecast_days=30, max_workers=max_workers)
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

    with patch.object(scheduler, "score_stock", side_effect=fake_score):
        scheduler._score_scan_universe(tickers, forecast_days=30, max_workers=max_workers)

    assert concurrent["peak"] <= max_workers


def test_empty_ticker_list_returns_empty_list():
    assert scheduler._score_scan_universe([], forecast_days=30) == []


# ── Integration: real _run_scan_impl() against a temp DB ──────────────────────
#
# Both tests below patch every side-effect source _run_scan_impl() touches
# EXCEPT init_db()/save_scan_run(), which run for real against temp_db —
# save_result() itself is patched to a recorder so call order/filtering can
# be asserted directly instead of via a scan_results row read-back.

def test_run_scan_processes_tickers_in_original_order_not_completion_order(temp_db):
    """Phase 1 completion order is not guaranteed (SLOW finishes AFTER the
    other three despite being listed first), but _run_scan_impl()'s Phase 2
    side effects (save_result here) must still follow tickers_map's original
    source-then-ticker order — proving Phase 2's loop, not completion order,
    drives the result. Mirrors
    test_watchlist_parallel_scan.py::test_result_order_matches_watchlist_order_in_scan_watchlist.
    Uses the real default worker count (5) — comfortably >= the 4 tickers
    here, so no test-only max_workers seam is needed on a live-trading entry
    point."""
    fake_tickers_map = {
        "SectorA": ["SLOW", "T1"],
        "SectorB": ["T2", "T3"],
    }

    def fake_score(ticker, forecast_days=30):
        if ticker == "SLOW":
            time.sleep(0.15)  # finishes AFTER the other three despite being listed first
        return {"ticker": ticker, "score": 50.0, "price": 10.0}

    fake_cfg = {
        "enabled": True,
        "sectors": ["SectorA", "SectorB"],
        "max_stocks": 50,
        "min_score": 0,
        "forecast_days": 30,
        "telegram": False,
        "scan_indices": ["Russell 2000"],
        "auto_watchlist": False,
    }

    def fake_load_tickers(sector, max_stocks, index_names=None):
        return fake_tickers_map.get(sector, [])

    save_calls: list = []
    with patch.object(scheduler, "load_config", return_value=fake_cfg), \
         patch.object(scheduler, "_is_trading_day", return_value=True), \
         patch.object(scheduler, "load_tickers", side_effect=fake_load_tickers), \
         patch.object(scheduler, "watchlist_get_all", return_value=[]), \
         patch.object(scheduler, "score_stock", side_effect=fake_score), \
         patch.object(scheduler, "save_result",
                       side_effect=lambda run_id, r: save_calls.append(r["ticker"])), \
         patch.object(scheduler, "check_alerts", return_value=[]), \
         patch("src.backtester.run_backtest",
               return_value={"total": 0, "accuracy_pct": 0, "avg_return": 0.0}):
        scheduler._run_scan_impl()

    assert save_calls == ["SLOW", "T1", "T2", "T3"]


def test_run_scan_respects_min_score_filter_with_parallel_scoring(temp_db):
    """Sanity check that min_score filtering — which reads r["score"] from
    the Phase-1 precomputed result rather than a freshly inline-scored value
    — still behaves identically after the refactor: only tickers >=
    min_score get save_result() called."""
    fake_tickers_map = {"SectorA": ["HIGH", "LOW"]}

    def fake_score(ticker, forecast_days=30):
        return {"ticker": ticker, "score": 80.0 if ticker == "HIGH" else 10.0, "price": 10.0}

    fake_cfg = {
        "enabled": True,
        "sectors": ["SectorA"],
        "max_stocks": 50,
        "min_score": 45,
        "forecast_days": 30,
        "telegram": False,
        "scan_indices": ["Russell 2000"],
        "auto_watchlist": False,
    }

    def fake_load_tickers(sector, max_stocks, index_names=None):
        return fake_tickers_map.get(sector, [])

    save_calls: list = []
    with patch.object(scheduler, "load_config", return_value=fake_cfg), \
         patch.object(scheduler, "_is_trading_day", return_value=True), \
         patch.object(scheduler, "load_tickers", side_effect=fake_load_tickers), \
         patch.object(scheduler, "watchlist_get_all", return_value=[]), \
         patch.object(scheduler, "score_stock", side_effect=fake_score), \
         patch.object(scheduler, "save_result",
                       side_effect=lambda run_id, r: save_calls.append(r["ticker"])), \
         patch.object(scheduler, "check_alerts", return_value=[]), \
         patch("src.backtester.run_backtest",
               return_value={"total": 0, "accuracy_pct": 0, "avg_return": 0.0}):
        scheduler._run_scan_impl()

    assert save_calls == ["HIGH"]
