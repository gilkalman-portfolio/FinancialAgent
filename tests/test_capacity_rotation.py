"""
Test: capacity rotation in auto_watchlist_agent.run().

Background: watchlist_policy.max_items_total was a hard silent block — once
the watchlist hit the cap, EVERY new candidate from EVERY source (squeeze,
catalyst, momentum, supertrend) was dropped with no eviction, regardless of
how strong the candidate was or how stale the weakest incumbent had become.
Observed live on 2026-08-14: momentum found 192-356 hits across several
30-min cycles and added 0, because the watchlist was already at 30/30.

_evict_for_capacity() replaces the silent block with a rotation: evict the
weakest eligible AUTO-added incumbent to make room, using the same
"replace weakest with something better" logic scheduler.run_weekly_rotation()
already uses, but triggered on demand instead of once a week. Two rules:

  - Scored candidate (squeeze/catalyst/momentum): must STRICTLY beat the
    weakest incumbent's recent average score.
  - Unscored candidate (supertrend — no composite gate by design): can only
    claim a slot the weakest incumbent's score has already fallen below
    AUTO_WL_SCORE_EXIT (40) for — i.e. genuine dead weight, never a ticker
    still earning its place.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_capacity_rotation.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="cap_rotation_")
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


@pytest.fixture(autouse=True)
def stub_telegram(monkeypatch):
    class _StubTg:
        def send_message(self, msg):
            return True
    monkeypatch.setattr("src.auto_watchlist_agent.TelegramNotifier", lambda: _StubTg())


def _add_ticker(ticker: str, notes: str, days_ago: int, avg_score: float | None):
    """Add a watchlist ticker with a controllable added_at and a fake 3-scan
    score history (so get_recent_scan_scores() returns something deterministic).
    avg_score=None means no scan_results rows at all (weakest fallback of 0.0)."""
    from src.database import DB_PATH, watchlist_add

    watchlist_add(ticker, notes=notes, alert_score=70, alert_pct=5.0)
    added_at = (datetime.now() - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE watchlist SET added_at = ? WHERE ticker = ?", (added_at, ticker))
        if avg_score is not None:
            conn.execute(
                "INSERT INTO scan_runs (run_at, scan_type) VALUES (?, ?)",
                (datetime.now().isoformat(), "manual"),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for _ in range(3):
                conn.execute(
                    "INSERT INTO scan_results (run_id, ticker, scanned_at, explosion_score) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, ticker, datetime.now().isoformat(), avg_score),
                )
        conn.commit()


def _fill_watchlist_to_cap(cap: int, weakest_score: float = 45.0, weakest_days_ago: int = 10):
    """Fill the watchlist to exactly `cap` entries: one controllable 'WEAK'
    auto ticker (fully eligible: old enough + has a known score) plus filler
    manually-added tickers for the rest."""
    _add_ticker("WEAK", "Auto [momentum]: score 45 on 2026-08-01", weakest_days_ago, weakest_score)
    for i in range(cap - 1):
        _add_ticker(f"MANUAL{i}", "manual pick", days_ago=30, avg_score=None)


def _base_cfg(max_total: int = 5) -> dict:
    return {
        "telegram": True,
        "auto_watchlist": {
            "enabled": True,
            "sources": {
                "momentum":   {"enabled": True, "min_score": 0},
                "supertrend": {"enabled": True, "price_floor": 5.0},
            },
            "deduplication": {"cooldown_minutes": 1440},
            "watchlist_policy": {
                "max_items_per_source": 10,
                "max_items_total": max_total,
                "require_liquidity_check": False,
            },
        },
    }


# ── Scored candidate (momentum-shaped) ──────────────────────────────────────

def test_scored_candidate_evicts_weaker_incumbent(temp_db):
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _fill_watchlist_to_cap(cap=5, weakest_score=45.0, weakest_days_ago=10)
    assert len(watchlist_get_all()) == 5

    results = [{"ticker": "STRONG", "price": 20.0, "score": 90.0,
                "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000}]
    added = aw_run(results, "momentum", _base_cfg(max_total=5))

    assert [r["ticker"] for r in added] == ["STRONG"]
    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert "WEAK" not in tickers, "expected the weakest incumbent to be evicted"
    assert "STRONG" in tickers
    assert len(watchlist_get_all()) == 5, "capacity invariant: must not exceed max_total"


def test_scored_candidate_weaker_than_incumbent_is_blocked(temp_db):
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _fill_watchlist_to_cap(cap=5, weakest_score=80.0, weakest_days_ago=10)

    results = [{"ticker": "MEDIOCRE", "price": 20.0, "score": 60.0,
                "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000}]
    added = aw_run(results, "momentum", _base_cfg(max_total=5))

    assert added == [], "candidate must not beat a strong incumbent — no eviction"
    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert "WEAK" in tickers
    assert "MEDIOCRE" not in tickers
    assert len(watchlist_get_all()) == 5


def test_incumbent_within_min_hold_is_never_evicted(temp_db):
    """A candidate scoring far above the weakest incumbent still can't evict
    it if the incumbent was added less than AUTO_WL_MIN_HOLD_DAYS (3) ago."""
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _fill_watchlist_to_cap(cap=5, weakest_score=20.0, weakest_days_ago=1)  # added yesterday

    results = [{"ticker": "STRONG", "price": 20.0, "score": 95.0,
                "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000}]
    added = aw_run(results, "momentum", _base_cfg(max_total=5))

    assert added == [], "expected min-hold to protect the incumbent even though it scores worse"
    assert any(w["ticker"] == "WEAK" for w in watchlist_get_all())


# ── Unscored candidate (supertrend-shaped) ──────────────────────────────────

def test_unscored_candidate_evicts_dead_weight_incumbent(temp_db):
    """A supertrend-style candidate (no score field at all) CAN claim a slot
    if the weakest incumbent has already fallen below AUTO_WL_SCORE_EXIT (40)."""
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _fill_watchlist_to_cap(cap=5, weakest_score=25.0, weakest_days_ago=10)  # dead weight

    results = [{"ticker": "FLIPCO", "price": 20.0, "level": 18.0, "avg_volume": 5_000_000}]
    assert "score" not in results[0] and "explosion_score" not in results[0]

    added = aw_run(results, "supertrend", _base_cfg(max_total=5))

    assert [r["ticker"] for r in added] == ["FLIPCO"]
    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert "WEAK" not in tickers
    assert "FLIPCO" in tickers


def test_unscored_candidate_cannot_bump_a_healthy_incumbent(temp_db):
    """The core fairness rule: a scoreless flip must never displace a ticker
    that is still above the exit bar, even though it has no score of its own
    to lose a numeric comparison with."""
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _fill_watchlist_to_cap(cap=5, weakest_score=55.0, weakest_days_ago=10)  # healthy, not dead weight

    results = [{"ticker": "FLIPCO", "price": 20.0, "level": 18.0, "avg_volume": 5_000_000}]
    added = aw_run(results, "supertrend", _base_cfg(max_total=5))

    assert added == [], "expected the healthy incumbent to be protected from an unscored candidate"
    assert any(w["ticker"] == "WEAK" for w in watchlist_get_all())


# ── Eviction bookkeeping ─────────────────────────────────────────────────────

def test_eviction_never_touches_manually_added_tickers(temp_db):
    """If every auto ticker is protected (min-hold or score), a manually-added
    ticker must never be evicted even though it's technically the 'weakest'
    by score (manual tickers have no scan_results, avg=0.0)."""
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all, watchlist_add

    # Fill entirely with manual tickers — no auto tickers exist at all.
    for i in range(5):
        watchlist_add(f"MANUAL{i}", notes="manual pick", alert_score=70, alert_pct=5.0)
    assert len(watchlist_get_all()) == 5

    results = [{"ticker": "STRONG", "price": 20.0, "score": 95.0,
                "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000}]
    added = aw_run(results, "momentum", _base_cfg(max_total=5))

    assert added == [], "no auto ticker exists to evict — must block, not touch a manual pick"
    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert tickers == {f"MANUAL{i}" for i in range(5)}


def test_eviction_writes_cooldown_before_remove(temp_db):
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_alerts

    _fill_watchlist_to_cap(cap=5, weakest_score=45.0, weakest_days_ago=10)

    results = [{"ticker": "STRONG", "price": 20.0, "score": 90.0,
                "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000}]
    aw_run(results, "momentum", _base_cfg(max_total=5))

    alerts = watchlist_get_alerts(ticker="WEAK", limit=10)
    types = {a["alert_type"] for a in alerts}
    assert "auto_exit_score" in types
    assert "auto_exit_cooldown" in types


def test_multiple_candidates_respect_capacity_invariant(temp_db):
    """Two candidates arrive in one run() call while at cap; only as many
    evictions happen as are actually justified, and the list never exceeds
    max_total at any point."""
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _add_ticker("WEAK1", "Auto [momentum]: score 40 on 2026-08-01", 10, 40.0)
    _add_ticker("WEAK2", "Auto [momentum]: score 50 on 2026-08-01", 10, 50.0)
    for i in range(3):
        _add_ticker(f"MANUAL{i}", "manual pick", days_ago=30, avg_score=None)
    assert len(watchlist_get_all()) == 5

    results = [
        {"ticker": "STRONG1", "price": 20.0, "score": 90.0,
         "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000},
        {"ticker": "STRONG2", "price": 20.0, "score": 85.0,
         "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000},
    ]
    added = aw_run(results, "momentum", _base_cfg(max_total=5))

    assert {r["ticker"] for r in added} == {"STRONG1", "STRONG2"}
    assert len(watchlist_get_all()) == 5, "capacity invariant must hold after multiple evictions"
    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert "WEAK1" not in tickers and "WEAK2" not in tickers


def test_candidate_already_on_watchlist_does_not_trigger_eviction(temp_db):
    """A candidate that re-appears in results (e.g. a second flip on a ticker
    already being tracked) doesn't need a new slot. At capacity, it must be
    processed as a normal re-alert — NOT treated as a growth attempt that
    evicts some unrelated weak ticker to make room for nothing."""
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _fill_watchlist_to_cap(cap=5, weakest_score=45.0, weakest_days_ago=10)
    watchlist_before = {w["ticker"] for w in watchlist_get_all()}
    assert "MANUAL0" in watchlist_before

    # MANUAL0 is already on the list — re-appearing here must not evict WEAK.
    results = [{"ticker": "MANUAL0", "price": 20.0, "level": 18.0, "avg_volume": 5_000_000}]
    added = aw_run(results, "supertrend", _base_cfg(max_total=5))

    assert [r["ticker"] for r in added] == ["MANUAL0"]
    tickers_after = {w["ticker"] for w in watchlist_get_all()}
    assert tickers_after == watchlist_before, \
        "expected no eviction — the candidate was already on the list, no new slot was needed"


def test_per_source_cap_blocks_before_wasting_an_eviction(temp_db):
    """max_items_per_source must be checked before attempting capacity
    eviction — otherwise a candidate that's about to be blocked anyway (per
    source exhausted this cycle) still evicts a ticker it can never use."""
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _add_ticker("WEAK1", "Auto [momentum]: score 30 on 2026-08-01", 10, 30.0)
    _add_ticker("WEAK2", "Auto [momentum]: score 35 on 2026-08-01", 10, 35.0)
    for i in range(3):
        _add_ticker(f"MANUAL{i}", "manual pick", days_ago=30, avg_score=None)
    assert len(watchlist_get_all()) == 5

    cfg = _base_cfg(max_total=5)
    cfg["auto_watchlist"]["watchlist_policy"]["max_items_per_source"] = 1

    results = [
        {"ticker": "STRONG1", "price": 20.0, "score": 95.0,
         "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000},
        {"ticker": "STRONG2", "price": 20.0, "score": 90.0,
         "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000},
    ]
    added = aw_run(results, "momentum", cfg)

    assert [r["ticker"] for r in added] == ["STRONG1"], \
        "only the first candidate should be added — per-source cap is 1"
    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert "WEAK2" in tickers, \
        "expected WEAK2 to survive — STRONG2 was blocked by the per-source cap, not capacity"
    assert "WEAK1" not in tickers, "expected only WEAK1 to be evicted, for STRONG1"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
