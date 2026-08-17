"""
Tests for scheduler.run_weekly_rotation() — previously zero coverage.

Background (2026-08-17): run_weekly_rotation() duplicated its own
"find weakest incumbent" logic instead of reusing
auto_watchlist_agent._weakest_evictable_auto_ticker(), and that duplicate had
none of the safety checks the shared function has: no minimum-hold-time
protection, and "no scan_results at all" silently defaulted to a score of
0.0. INHD (a real ticker, catalyst score 87) was added by run_scan() at
08:10 and evicted by run_weekly_rotation() at 08:16 — six minutes later —
because it had no composite scan history yet and was picked as "weakest".

run_weekly_rotation() now imports and reuses _weakest_evictable_auto_ticker
and _candidate_beats_weakest from auto_watchlist_agent.py directly, so this
file only needs to verify the reuse actually took effect end-to-end, not
re-derive the underlying rules (those are covered in depth by
test_capacity_rotation.py).

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_weekly_rotation.py -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scheduler  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="weekly_rotation_")
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


def _add_ticker(ticker: str, notes: str, days_ago: int, avg_score: float | None):
    from src.database import DB_PATH, watchlist_add

    watchlist_add(ticker, notes=notes, alert_score=70, alert_pct=5.0)
    added_at = (datetime.now() - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE watchlist SET added_at = ? WHERE ticker = ?", (added_at, ticker))
        conn.commit()
    if avg_score is not None:
        _seed_scan_history(ticker, avg_score)


def _seed_scan_history(ticker: str, score: float):
    from src.database import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scan_runs (run_at, scan_type) VALUES (?, ?)",
            (datetime.now().isoformat(), "manual"),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for _ in range(3):
            conn.execute(
                "INSERT INTO scan_results (run_id, ticker, scanned_at, explosion_score) "
                "VALUES (?, ?, ?, ?)",
                (run_id, ticker, datetime.now().isoformat(), score),
            )
        conn.commit()


def _run(cfg=None, momentum_results=None, index_tickers=None):
    """Invoke run_weekly_rotation() with the DB-heavy internals real (via
    temp_db) but the external/heavy dependencies mocked, mirroring the
    existing house style in test_llm_universe_curation_job.py."""
    cfg = cfg or {"enabled": True, "telegram": True}
    index_df = None
    if index_tickers is not None:
        import pandas as pd
        index_df = pd.DataFrame({"ticker": index_tickers})

    with patch.object(scheduler, "load_config", return_value=cfg), \
         patch.object(scheduler, "init_db"), \
         patch("src.momentum_scanner.scan_momentum", return_value=momentum_results or []), \
         patch("src.index_loader.get_index", return_value=index_df), \
         patch.object(scheduler, "TelegramNotifier") as tg:
        scheduler.run_weekly_rotation()
    return tg


# ── Min-hold protection (the INHD incident) ─────────────────────────────────

def test_recently_added_ticker_is_never_evicted_minutes_later(temp_db):
    """Reproduces the INHD incident: a ticker added minutes ago must not be
    picked as 'weakest' just because it has no scan history yet."""
    from src.database import watchlist_get_all

    _add_ticker("INHD", "Auto [catalyst]: Earnings report Score 87 on 2026-08-17",
                days_ago=0, avg_score=None)  # added "just now" — 0 days held
    _add_ticker("STABLE", "Auto [momentum]: score 60 on 2026-08-01",
                days_ago=10, avg_score=60.0)

    _run(momentum_results=[{"ticker": "NEWPICK", "score": 95.0}], index_tickers=["NEWPICK"])

    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert "INHD" in tickers, "a ticker added minutes ago must survive weekly rotation"


def test_incumbent_within_min_hold_blocks_rotation_entirely(temp_db):
    """If the ONLY auto ticker is within its min-hold window, there is nothing
    eligible to evict — rotation must be a no-op, not fall back to evicting
    something else."""
    from src.database import watchlist_get_all

    _add_ticker("FRESH", "Auto [momentum]: score 20 on 2026-08-17", days_ago=1, avg_score=20.0)

    tg = _run(momentum_results=[{"ticker": "NEWPICK", "score": 95.0}], index_tickers=["NEWPICK"])

    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert tickers == {"FRESH"}, "expected no eviction — the only auto ticker is within min-hold"
    tg.return_value.send_message.assert_not_called()


# ── No-data-as-0 bug ─────────────────────────────────────────────────────────

def test_no_history_ticker_is_not_picked_as_weakest(temp_db):
    """A momentum/squeeze/catalyst/supertrend-only ticker with zero
    scan_results must not be treated as the weakest just because it has no
    data — a real, low-scoring incumbent must be picked instead."""
    from src.database import watchlist_get_all

    _add_ticker("NODATA", "Auto [supertrend]: flip on 2026-08-10", days_ago=10, avg_score=None)
    _add_ticker("REALWEAK", "Auto [momentum]: score 15 on 2026-08-01", days_ago=10, avg_score=15.0)

    _run(momentum_results=[{"ticker": "NEWPICK", "score": 95.0}], index_tickers=["NEWPICK"])

    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert "NODATA" in tickers, "no-history ticker must not be evicted in place of a real weak one"
    assert "REALWEAK" not in tickers


# ── Scale-mismatch fix ───────────────────────────────────────────────────────

def test_momentum_candidate_without_composite_history_cannot_bump_healthy_incumbent(temp_db):
    """A scan_momentum() candidate's score is on a different scale from the
    composite average — without real composite history of its own, it must
    not be able to numerically 'beat' a live (non-dead-weight) incumbent."""
    from src.database import watchlist_get_all

    _add_ticker("HEALTHY", "Auto [momentum]: score 55 on 2026-08-01", days_ago=10, avg_score=55.0)

    _run(momentum_results=[{"ticker": "NEWPICK", "score": 99.0}], index_tickers=["NEWPICK"])

    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert tickers == {"HEALTHY"}, (
        "a momentum-scale score of 99 must not beat a composite average of 55 — "
        "the candidate has no composite history to compare fairly"
    )


def test_momentum_candidate_can_evict_genuine_dead_weight(temp_db):
    """A momentum candidate with no composite history CAN still claim a slot
    from an incumbent whose real composite average has already fallen below
    AUTO_WL_SCORE_EXIT — same dead-weight rule as capacity rotation."""
    from src.database import watchlist_get_all

    _add_ticker("DEADWEIGHT", "Auto [momentum]: score 20 on 2026-08-01", days_ago=10, avg_score=20.0)

    _run(momentum_results=[{"ticker": "NEWPICK", "score": 99.0}], index_tickers=["NEWPICK"])

    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert tickers == {"NEWPICK"}, "expected genuine dead weight to be rotated out"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
