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
already uses, but triggered on demand instead of once a week.

Updated 2026-08-17: a same-day incident showed the original rule compared
scores from different scales (a raw momentum/squeeze/catalyst score vs. a
composite scan_results average) and defaulted "no scan_results at all" to
0.0, wrongly evicting strong picks that simply hadn't gone through the main
composite scan. Both `_evict_for_capacity` (candidate side) and
`_weakest_evictable_auto_ticker` (incumbent side) now key off real
scan_results history alone, not which scanner discovered the ticker:

  - Candidate HAS real composite scan history: must STRICTLY beat the
    weakest incumbent's recent composite average.
  - Candidate has NO composite scan history (regardless of source): can only
    claim a slot the weakest incumbent's composite average has already
    fallen below AUTO_WL_SCORE_EXIT (40) for — i.e. genuine dead weight,
    never a ticker still earning its place. An incumbent with no composite
    history of its own is excluded from being "weakest" entirely, rather
    than defaulted to a fake 0.0.
  - An incumbent with an open IBKR position is never evictable, regardless
    of score — see CLAUDE.md Incident Archive 2026-08-17.

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
    avg_score=None means no scan_results rows at all — such a ticker is now
    excluded from "weakest" selection entirely (see module docstring), not
    defaulted to a fake 0.0."""
    from src.database import DB_PATH, watchlist_add

    watchlist_add(ticker, notes=notes, alert_score=70, alert_pct=5.0)
    added_at = (datetime.now() - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE watchlist SET added_at = ? WHERE ticker = ?", (added_at, ticker))
        conn.commit()
    if avg_score is not None:
        _seed_scan_history(ticker, avg_score)


def _seed_scan_history(ticker: str, score: float):
    """Give `ticker` a real 3-row composite scan_results history — independent
    of watchlist membership, since a candidate that hasn't been added yet
    (e.g. a fresh momentum/squeeze/catalyst pick) can still have gone through
    the main composite scan separately."""
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

    # STRONG needs real composite history to fairly beat WEAK's composite
    # average (see module docstring) — the momentum-scale "score" field in
    # the result dict below is informational-only, never compared numerically.
    _seed_scan_history("STRONG", 90.0)
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
    """If no auto ticker exists at all, a manually-added ticker must never be
    evicted — _is_auto_ticker() excludes manual picks before scoring is even
    considered, regardless of whether they have scan_results."""
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

    _seed_scan_history("STRONG", 90.0)
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

    _seed_scan_history("STRONG1", 90.0)
    _seed_scan_history("STRONG2", 85.0)
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


# ── No-data vs real-data incumbents (2026-08-17 incident regression tests) ──

def test_no_history_incumbent_is_never_picked_as_weakest(temp_db):
    """The exact shape of the 2026-08-17 incident: a ticker discovered only
    via momentum/squeeze/catalyst/supertrend (no scan_results at all) must
    never be treated as 'weakest' just because it has no data — a real,
    genuinely-low-scoring incumbent must be picked instead."""
    from src.auto_watchlist_agent import _weakest_evictable_auto_ticker
    from src.database import watchlist_get_all

    _add_ticker("NODATA", "Auto [momentum]: score 97 on 2026-08-17", days_ago=3, avg_score=None)
    _add_ticker("REALWEAK", "Auto [momentum]: score 15 on 2026-08-01", days_ago=10, avg_score=15.0)

    weakest = _weakest_evictable_auto_ticker(watchlist_get_all())
    assert weakest is not None
    assert weakest[0] == "REALWEAK", (
        "expected the ticker with real (if low) composite history to be picked as "
        "weakest, not the one with no scan_results at all"
    )


def test_all_incumbents_lack_history_returns_none(temp_db):
    """If every eligible auto ticker lacks composite history, there is nothing
    reliable to rank — must return None (safe fallback: caller blocks the new
    candidate, same as before capacity rotation existed) rather than picking
    an arbitrary one via a fake 0.0 tie."""
    from src.auto_watchlist_agent import _weakest_evictable_auto_ticker
    from src.database import watchlist_get_all

    _add_ticker("NODATA1", "Auto [supertrend]: flip on 2026-08-17", days_ago=5, avg_score=None)
    _add_ticker("NODATA2", "Auto [momentum]: score 80 on 2026-08-17", days_ago=5, avg_score=None)

    assert _weakest_evictable_auto_ticker(watchlist_get_all()) is None


# ── Open IBKR position protection (2026-08-17) ──────────────────────────────

def _mark_open_position(ticker: str, shares: float = 100.0):
    from src.database import DB_PATH

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO ibkr_positions (ticker, shares, avg_cost, unrealized_pnl, "
            "market_value, last_synced) VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, shares, 10.0, 0.0, shares * 10.0, datetime.now().isoformat()),
        )
        conn.commit()


def test_held_position_is_never_evicted_even_as_numeric_weakest(temp_db):
    """A ticker with a real open IBKR position must never be sacrificed to
    free a watchlist slot, no matter how low its score is — evicting it would
    strip a held position of monitoring_queue coverage. See CLAUDE.md
    Incident Archive 2026-08-17."""
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _fill_watchlist_to_cap(cap=5, weakest_score=10.0, weakest_days_ago=10)  # WEAK is dead weight
    _mark_open_position("WEAK")

    _seed_scan_history("STRONG", 90.0)
    results = [{"ticker": "STRONG", "price": 20.0, "score": 90.0,
                "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000}]
    added = aw_run(results, "momentum", _base_cfg(max_total=5))

    assert added == [], "expected the held position to block eviction — no slot could be freed"
    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert "WEAK" in tickers, "a real open position must never be evicted"
    assert "STRONG" not in tickers


def test_held_position_is_skipped_in_favor_of_next_weakest(temp_db):
    """When the numeric 'weakest' incumbent is actually a held position, the
    function must fall through to the next-weakest eligible ticker instead of
    just giving up."""
    from src.auto_watchlist_agent import run as aw_run
    from src.database import watchlist_get_all

    _add_ticker("HELD", "Auto [momentum]: score 5 on 2026-08-01", days_ago=10, avg_score=5.0)
    _mark_open_position("HELD")
    _add_ticker("WEAK", "Auto [momentum]: score 30 on 2026-08-01", days_ago=10, avg_score=30.0)
    for i in range(3):
        _add_ticker(f"MANUAL{i}", "manual pick", days_ago=30, avg_score=None)
    assert len(watchlist_get_all()) == 5

    _seed_scan_history("STRONG", 90.0)
    results = [{"ticker": "STRONG", "price": 20.0, "score": 90.0,
                "vol_ratio": 0, "price_change_5d": 0, "avg_volume": 5_000_000}]
    added = aw_run(results, "momentum", _base_cfg(max_total=5))

    assert [r["ticker"] for r in added] == ["STRONG"]
    tickers = {w["ticker"] for w in watchlist_get_all()}
    assert "HELD" in tickers, "the held position must survive even though it scores lower"
    assert "WEAK" not in tickers, "the non-held ticker should be evicted instead"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
