"""
Test: backtester.get_top_signals()/get_worst_signals() exclude unmatured
signals (pct_change IS NULL) instead of letting them surface as a fake
best/worst result.

Background (2026-08-18): a signal's pct_change stays NULL until the
outcome-check horizon matures. Neither query filtered on that. SQLite sorts
NULL before every other value in ASC order, so get_worst_signals() (ORDER BY
pct_change ASC) always put an all-NULL ticker group at the very top of the
"worst" list — page_backtest.py then formatted that None with `:+.1f}%` and
crashed the entire Backtest page, every time, as soon as any unmatured
signal existed (which is most of the time — 567/10601 rows were NULL when
this was caught). get_top_signals() had the same latent bug, just masked
whenever enough real results existed to fill its LIMIT before the NULL rows
sorted in.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_backtester_null_pct_change.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtester import get_top_signals, get_worst_signals  # noqa: E402


@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="backtest_null_")
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


def _insert(conn, ticker, signal, pct_change, days_ahead=7, score=70.0):
    conn.execute(
        """INSERT INTO backtest_results
           (ticker, signal, score, price_at_signal, price_after, pct_change, days_ahead, signal_date)
           VALUES (?, ?, ?, 100.0, ?, ?, ?, ?)""",
        (ticker, signal, score,
         None if pct_change is None else 100.0 * (1 + pct_change / 100),
         pct_change, days_ahead, datetime.now().isoformat()),
    )


def test_worst_signals_excludes_all_null_ticker(temp_db):
    """A ticker whose only row is unmatured (NULL) must never appear —
    this is the exact shape that crashed the live Backtest page."""
    from src.backtester import init_backtest_tables
    from src.database import get_connection

    init_backtest_tables()
    with get_connection() as conn:
        _insert(conn, "UNMATURED", "BUY", None)
        _insert(conn, "REALBAD", "BUY", -12.5)
        conn.commit()

    worst = get_worst_signals(10)
    tickers = [r["ticker"] for r in worst]
    assert "UNMATURED" not in tickers
    assert "REALBAD" in tickers
    for r in worst:
        assert r["pct_change"] is not None


def test_top_signals_excludes_all_null_ticker(temp_db):
    from src.backtester import init_backtest_tables
    from src.database import get_connection

    init_backtest_tables()
    with get_connection() as conn:
        _insert(conn, "UNMATURED", "STRONG BUY", None)
        _insert(conn, "REALGOOD", "STRONG BUY", 18.3)
        conn.commit()

    top = get_top_signals(10)
    tickers = [r["ticker"] for r in top]
    assert "UNMATURED" not in tickers
    assert "REALGOOD" in tickers
    for r in top:
        assert r["pct_change"] is not None


def test_worst_signals_mixed_matured_and_unmatured_rows_for_same_ticker(temp_db):
    """A ticker with both a matured and an unmatured row (different
    days_ahead horizons) must rank on its real matured value, not NULL."""
    from src.backtester import init_backtest_tables
    from src.database import get_connection

    init_backtest_tables()
    with get_connection() as conn:
        _insert(conn, "MIXED", "BUY", None, days_ahead=30)
        _insert(conn, "MIXED", "BUY", -8.0, days_ahead=7)
        conn.commit()

    worst = get_worst_signals(10)
    row = next(r for r in worst if r["ticker"] == "MIXED")
    assert row["pct_change"] == -8.0
