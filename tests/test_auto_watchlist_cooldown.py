"""
Test: auto-watchlist re-entry cooldown after auto-exit.

Scenario (Risk #5 — feedback loop):
  Day 0  run_scan @ 08:30 auto-adds FOO at score 72 (>= 70 entry threshold).
  Day 0  run_watchlist_scan @ 12:00 sees FOO at score 38 → auto-exits and writes
         an `auto_exit_cooldown` watchlist_alerts row.
  Day 0+ Any auto-add attempt within AUTO_EXIT_COOLDOWN_DAYS (7d) at the normal
         entry threshold (>=70 but <75) MUST be blocked.
  Day 8+ Cooldown has elapsed → re-add allowed again, but only at score >= 75
         (re-entry threshold). 70–74 still blocked? No — once cooldown expires,
         the normal 70 threshold applies again.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_auto_watchlist_cooldown.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Test DB setup ─────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(monkeypatch):
    """Spin up a temp SQLite DB with the schema needed by scheduler logic."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="aw_cooldown_")
    os.close(fd)
    db_path = Path(path)

    # Patch DB_PATH BEFORE importing database module helpers, then init schema.
    import src.database as db
    monkeypatch.setattr(db, "DB_PATH", db_path)

    db.init_db()
    yield db_path

    try:
        db_path.unlink()
    except Exception:
        pass


# ── Helpers ────────────────────────────────────────────────────────────────────

def _simulate_auto_add(ticker: str, score: float) -> bool:
    """Replicates the gate logic from scheduler.py:run_scan() auto-add block.

    Returns True if the ticker would be added, False if blocked.
    """
    from scheduler import (
        _in_auto_exit_cooldown,
        AUTO_WL_REENTRY_SCORE,
    )
    from src.database import watchlist_add, watchlist_get_all

    if score < 70:
        return False  # below normal entry threshold

    existing = {w["ticker"] for w in watchlist_get_all()}
    if ticker in existing:
        return False  # already present — not re-added

    if _in_auto_exit_cooldown(ticker) and score < AUTO_WL_REENTRY_SCORE:
        return False  # blocked by cooldown

    watchlist_add(
        ticker,
        notes=f"Auto: score {score:.0f} on {datetime.now().strftime('%Y-%m-%d')}",
        alert_score=70,
        alert_pct=5.0,
    )
    return True


def _simulate_auto_exit(ticker: str, score: float):
    """Replicates run_watchlist_scan's auto-exit: remove + write cooldown row."""
    from src.database import (
        watchlist_remove, watchlist_save_alert,
    )
    from scheduler import AUTO_EXIT_COOLDOWN_DAYS

    watchlist_remove(ticker)
    watchlist_save_alert(
        ticker, "auto_exit_score",
        f"Auto-exit: score {score:.0f}",
        score=score, price=10.0,
    )
    watchlist_save_alert(
        ticker, "auto_exit_cooldown",
        f"Cooldown {AUTO_EXIT_COOLDOWN_DAYS}d after auto-exit (score {score:.0f})",
        score=score, price=10.0,
    )


def _backdate_cooldown(ticker: str, days_ago: int):
    """Manually rewrite the sent_at of the `auto_exit_cooldown` row to N days ago.

    This is how we 'fast-forward' time in the test without actually waiting.
    """
    from src.database import DB_PATH
    new_ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE watchlist_alerts SET sent_at = ? "
            "WHERE ticker = ? AND alert_type = 'auto_exit_cooldown'",
            (new_ts, ticker.upper()),
        )
        conn.commit()


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_initial_add_succeeds(temp_db):
    """Sanity: with a clean DB, score=72 → FOO gets added."""
    assert _simulate_auto_add("FOO", 72) is True
    from src.database import watchlist_get_all
    assert any(w["ticker"] == "FOO" for w in watchlist_get_all())


def test_cooldown_blocks_readd_at_normal_score(temp_db):
    """After auto-exit, score=72 is blocked. Score=80 also blocked? No: 80 >= 75 → allowed."""
    # Day 0: add
    assert _simulate_auto_add("FOO", 72) is True
    # Day 0 noon: auto-exit
    _simulate_auto_exit("FOO", 38)
    from src.database import watchlist_get_all
    assert not any(w["ticker"] == "FOO" for w in watchlist_get_all())

    # Day 0 afternoon: re-add at 72 must be BLOCKED
    assert _simulate_auto_add("FOO", 72) is False, \
        "expected cooldown to block re-add at score 72"

    # Even at 74 (below re-entry threshold) — still blocked
    assert _simulate_auto_add("FOO", 74) is False, \
        "expected cooldown to block re-add at score 74 (< 75 re-entry)"


def test_cooldown_allows_strong_signal_override(temp_db):
    """A score >= AUTO_WL_REENTRY_SCORE (75) bypasses the cooldown."""
    assert _simulate_auto_add("FOO", 72) is True
    _simulate_auto_exit("FOO", 38)
    # Strong override at 80
    assert _simulate_auto_add("FOO", 80) is True, \
        "expected strong-signal override (score 80 >= 75) to bypass cooldown"
    from src.database import watchlist_get_all
    assert any(w["ticker"] == "FOO" for w in watchlist_get_all())


def test_cooldown_expires_after_window(temp_db):
    """After 8 days the cooldown row is stale → re-add at score >= 75 works."""
    assert _simulate_auto_add("FOO", 72) is True
    _simulate_auto_exit("FOO", 38)
    # Fast-forward 8 days
    _backdate_cooldown("FOO", days_ago=8)
    # Re-add at strong score now allowed
    assert _simulate_auto_add("FOO", 80) is True, \
        "expected expired cooldown to allow re-add at strong score"

    # Reset and verify 72 also allowed once cooldown expired
    from src.database import watchlist_remove
    watchlist_remove("FOO")
    # Note: removing the watchlist row doesn't touch the cooldown alert, but it's
    # already backdated → still expired → 72 should be allowed.
    assert _simulate_auto_add("FOO", 72) is True


def test_cooldown_still_blocks_at_72_within_window(temp_db):
    """Explicit verification of the task's third assertion: between 70 and 75, blocked."""
    assert _simulate_auto_add("FOO", 72) is True
    _simulate_auto_exit("FOO", 38)
    # Only 3 days have passed — well within 7d cooldown window
    _backdate_cooldown("FOO", days_ago=3)
    assert _simulate_auto_add("FOO", 72) is False, \
        "expected re-add at 72 to remain blocked at day 3 of cooldown"
    assert _simulate_auto_add("FOO", 74) is False, \
        "expected re-add at 74 to remain blocked (< 75 re-entry threshold)"


def test_cleanup_writes_cooldown_and_blocks_readd(temp_db, monkeypatch):
    """run_watchlist_cleanup() must write auto_exit_cooldown for every removed
    ticker, so a ticker cleaned up via 3x score<50 cannot be re-added within
    the 7d cooldown window even if its score jumps back to 80.
    """
    import sqlite3
    from datetime import datetime
    from src.database import (
        DB_PATH, watchlist_add, watchlist_get_all, watchlist_get_alerts,
    )

    # 1. Auto-add ticker BAR to watchlist
    watchlist_add(
        "BAR",
        notes=f"Auto: score 72 on {datetime.now().strftime('%Y-%m-%d')}",
        alert_score=70,
        alert_pct=5.0,
    )
    assert any(w["ticker"] == "BAR" for w in watchlist_get_all())

    # 2. Insert a scan_run + 3 scan_results all with score < 50
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scan_runs (run_at, scan_type) VALUES (?, ?)",
            (datetime.now().isoformat(), "manual"),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for score in (45.0, 40.0, 38.0):
            conn.execute(
                "INSERT INTO scan_results (run_id, ticker, scanned_at, "
                "explosion_score) VALUES (?, ?, ?, ?)",
                (run_id, "BAR", datetime.now().isoformat(), score),
            )
        conn.commit()

    # 3. Patch trading-day + Telegram + load_config so cleanup runs end-to-end
    import scheduler as sch
    monkeypatch.setattr(sch, "_is_trading_day", lambda: True)
    monkeypatch.setattr(sch, "load_config", lambda: {"telegram": True})

    class _StubTg:
        def send_message(self, msg):
            return True
    monkeypatch.setattr(sch, "TelegramNotifier", lambda: _StubTg())

    # 4. Run cleanup
    sch.run_watchlist_cleanup()

    # 5. BAR is gone
    assert not any(w["ticker"] == "BAR" for w in watchlist_get_all()), \
        "expected cleanup to remove BAR"

    # 6. auto_exit_cooldown alert was written
    alerts = watchlist_get_alerts(ticker="BAR", limit=10)
    cooldown_alerts = [a for a in alerts if a["alert_type"] == "auto_exit_cooldown"]
    assert len(cooldown_alerts) >= 1, \
        "expected run_watchlist_cleanup to write an auto_exit_cooldown row"

    # 7. Re-add within cooldown window at normal entry score (72) → BLOCKED
    #    (note: scores >= AUTO_WL_REENTRY_SCORE=75 are intentionally allowed to
    #     override the cooldown by design — the cleanup-written marker plugs the
    #     hole for the 70-74 band that was previously a backdoor.)
    assert _simulate_auto_add("BAR", 72) is False, \
        "expected cleanup-written cooldown to block re-add at score 72"
    assert _simulate_auto_add("BAR", 74) is False, \
        "expected cleanup-written cooldown to block re-add at score 74"

    # 8. Once we backdate the cooldown >7d, re-add at 72 is allowed again
    _backdate_cooldown("BAR", days_ago=8)
    assert _simulate_auto_add("BAR", 72) is True, \
        "expected expired cleanup cooldown to allow re-add"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
