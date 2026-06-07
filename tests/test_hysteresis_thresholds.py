"""
Tests for hysteresis bands — Risk #3 fix (audit).

Simulates a ticker whose score oscillates around the old binary threshold
and asserts the watchlist does NOT thrash. Runs as a module:

    .venv\\Scripts\\python.exe -m tests.test_hysteresis_thresholds
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from src.hysteresis import (
    passes_hysteresis,
    AUTO_WL_SCORE_ENTRY, AUTO_WL_SCORE_EXIT, AUTO_WL_MIN_HOLD_DAYS,
    SQUEEZE_SI_ENTRY, SQUEEZE_SI_EXIT,
    CATALYST_SI_ENTRY, CATALYST_SI_EXIT,
    LIQUIDITY_ADV_ENTRY, LIQUIDITY_ADV_EXIT,
)


# ── Helper: pure-Python simulation of scheduler watchlist add/remove ──────────


class _SimWatchlist:
    """Models the auto-watchlist add/remove decision without touching SQLite."""

    def __init__(self, min_hold_days: int = AUTO_WL_MIN_HOLD_DAYS):
        self._items: dict[str, datetime] = {}  # ticker -> added_at
        self._min_hold = timedelta(days=min_hold_days)
        self.add_count: dict[str, int] = {}
        self.remove_count: dict[str, int] = {}

    def step(self, ticker: str, score: float, now: datetime) -> str:
        """Apply one scoring observation, return action: 'added' | 'removed' | 'hold' | 'skip'."""
        in_set = ticker in self._items

        # ENTRY: ticker not in watchlist, score >= entry threshold
        if not in_set and score >= AUTO_WL_SCORE_ENTRY:
            self._items[ticker] = now
            self.add_count[ticker] = self.add_count.get(ticker, 0) + 1
            return "added"

        # EXIT: in watchlist, score < exit threshold, AND min-hold satisfied
        if in_set and score < AUTO_WL_SCORE_EXIT:
            held = now - self._items[ticker]
            if held >= self._min_hold:
                del self._items[ticker]
                self.remove_count[ticker] = self.remove_count.get(ticker, 0) + 1
                return "removed"
            return "hold"  # too soon to drop

        # Sit in the deadband
        if in_set:
            return "hold"
        return "skip"

    def __contains__(self, ticker: str) -> bool:
        return ticker in self._items


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_passes_hysteresis_basic():
    # Not in set, below entry -> stays out
    assert passes_hysteresis(50, False, 70, 40) is False
    # Not in set, at entry -> enters
    assert passes_hysteresis(70, False, 70, 40) is True
    # In set, in deadband -> stays
    assert passes_hysteresis(49, True, 70, 40) is True
    assert passes_hysteresis(41, True, 70, 40) is True
    # In set, below exit -> leaves
    assert passes_hysteresis(39, True, 70, 40) is False
    # Validation
    try:
        passes_hysteresis(50, False, 40, 70)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for inverted thresholds")
    print("  ok  passes_hysteresis basic semantics")


def test_oscillation_no_thrash():
    """Scenario from the audit:
        75 -> 49 -> 55 -> 41 -> 35 -> 42 -> 38 -> 32
    Asserts:
      - Added once (at 75)
      - Stays through 49 / 55 / 41 (deadband)
      - Removed at 35 once min-hold (3 days) has passed
      - Does NOT re-add at 42 (below entry 70)
      - Stays out at 38 / 32
    """
    wl = _SimWatchlist()
    t0 = datetime(2026, 5, 1, 9, 0, 0)
    ticker = "TEST"

    # Day 0 — initial high score
    assert wl.step(ticker, 75, t0) == "added"
    assert ticker in wl

    # Day 1 — drop into deadband
    assert wl.step(ticker, 49, t0 + timedelta(days=1)) == "hold"
    assert ticker in wl

    # Day 2 — bounce up but still below entry threshold (already in, so deadband applies)
    assert wl.step(ticker, 55, t0 + timedelta(days=2)) == "hold"
    assert ticker in wl

    # Day 2.5 — dip just above exit, still hold
    assert wl.step(ticker, 41, t0 + timedelta(days=2, hours=12)) == "hold"
    assert ticker in wl

    # Day 4 — drop below exit, min-hold satisfied -> remove
    assert wl.step(ticker, 35, t0 + timedelta(days=4)) == "removed"
    assert ticker not in wl

    # Day 5 — bounces to 42 (above old 35 threshold, well below new 70 entry).
    # MUST NOT re-add. This is the core anti-thrash guarantee.
    assert wl.step(ticker, 42, t0 + timedelta(days=5)) == "skip"
    assert ticker not in wl

    # Day 6 — back down
    assert wl.step(ticker, 38, t0 + timedelta(days=6)) == "skip"
    assert wl.step(ticker, 32, t0 + timedelta(days=7)) == "skip"

    # Net counts: 1 add, 1 remove — no thrash
    assert wl.add_count[ticker] == 1, f"expected 1 add, got {wl.add_count[ticker]}"
    assert wl.remove_count[ticker] == 1, f"expected 1 remove, got {wl.remove_count[ticker]}"
    print("  ok  oscillation 75/49/55/41/35/42/38/32 -> 1 add, 1 remove, no thrash")


def test_min_hold_prevents_premature_exit():
    """A ticker added at 75 then immediately crashing to 20 must NOT be removed
    before min-hold-days has passed."""
    wl = _SimWatchlist()
    t0 = datetime(2026, 5, 1, 9, 0, 0)
    ticker = "FAST"

    assert wl.step(ticker, 75, t0) == "added"
    # 1 day later — score collapses
    assert wl.step(ticker, 20, t0 + timedelta(days=1)) == "hold"
    # 2 days later — still locked
    assert wl.step(ticker, 20, t0 + timedelta(days=2)) == "hold"
    # 3 days exactly — released
    assert wl.step(ticker, 20, t0 + timedelta(days=3)) == "removed"
    assert ticker not in wl
    print("  ok  min-hold-days locks exit for AUTO_WL_MIN_HOLD_DAYS")


def test_threshold_bands_documented_values():
    """The constants must match the spec from the audit fix."""
    assert AUTO_WL_SCORE_ENTRY == 70
    assert AUTO_WL_SCORE_EXIT == 40
    assert AUTO_WL_MIN_HOLD_DAYS == 3
    assert SQUEEZE_SI_ENTRY == 15.0 and SQUEEZE_SI_EXIT == 10.0
    assert CATALYST_SI_ENTRY == 10.0 and CATALYST_SI_EXIT == 5.0
    assert LIQUIDITY_ADV_ENTRY == 5_000_000.0
    assert LIQUIDITY_ADV_EXIT == 3_000_000.0
    # And every band must satisfy entry > exit
    for name, lo, hi in [
        ("auto-wl", AUTO_WL_SCORE_EXIT, AUTO_WL_SCORE_ENTRY),
        ("squeeze-si", SQUEEZE_SI_EXIT, SQUEEZE_SI_ENTRY),
        ("catalyst-si", CATALYST_SI_EXIT, CATALYST_SI_ENTRY),
        ("liquidity", LIQUIDITY_ADV_EXIT, LIQUIDITY_ADV_ENTRY),
    ]:
        assert hi > lo, f"{name}: entry ({hi}) must exceed exit ({lo})"
    print("  ok  documented threshold values match spec")


# ── Runner ────────────────────────────────────────────────────────────────────


def _run_all():
    tests = [
        test_passes_hysteresis_basic,
        test_oscillation_no_thrash,
        test_min_hold_prevents_premature_exit,
        test_threshold_bands_documented_values,
    ]
    failed = 0
    print(f"Running {len(tests)} hysteresis tests...")
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"RESULT: FAIL ({failed}/{len(tests)} failed)")
        return 1
    print(f"RESULT: PASS ({len(tests)}/{len(tests)} passed)")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
