"""
Hysteresis helper — eliminates scoring cliffs by separating ENTRY and EXIT
thresholds with a deadband.

Rationale
---------
Binary thresholds (e.g. "score >= 70 enters watchlist, score < 50 exits")
create a thrash region: a ticker oscillating around the boundary is
repeatedly added and removed. The fix is hysteresis — once you're in a
state, you stay in until you cross a *second*, looser boundary in the
opposite direction.

Usage
-----
    from src.hysteresis import passes_hysteresis

    is_in_watchlist = passes_hysteresis(
        current_value=score,
        previously_in_set=ticker in existing_watchlist,
        entry_thr=70,
        exit_thr=40,
    )

Project bands (see CLAUDE.md → Risk #3 fix):

    Threshold                        ENTRY   EXIT
    ──────────────────────────────────────────────
    Auto-watchlist score              70      40
    Composite-for-BUY                 60      —  (gate removed 2026-06-03, Supertrend-only)
    Squeeze SI%                       15      10
    Catalyst SI%                      10       5
    Liquidity ADV ($)                 5M       3M
"""

from __future__ import annotations


# ── Project-wide hysteresis bands (single source of truth) ───────────────────
AUTO_WL_SCORE_ENTRY = 70
AUTO_WL_SCORE_EXIT = 40
AUTO_WL_MIN_HOLD_DAYS = 3

# Auto-exit cooldown: after a ticker is auto-exited, block re-entry from any
# auto-watchlist source for AUTO_EXIT_COOLDOWN_DAYS unless the candidate score
# is exceptionally strong (>= AUTO_WL_REENTRY_SCORE). Breaks add→exit→re-add
# thrash loops at the boundary.
AUTO_EXIT_COOLDOWN_DAYS = 7
AUTO_WL_REENTRY_SCORE = 75

COMPOSITE_BUY_ENTRY = 60
COMPOSITE_BUY_EXIT = 50

SQUEEZE_SI_ENTRY = 15.0
SQUEEZE_SI_EXIT = 10.0

CATALYST_SI_ENTRY = 10.0
CATALYST_SI_EXIT = 5.0

LIQUIDITY_ADV_ENTRY = 5_000_000.0
LIQUIDITY_ADV_EXIT = 3_000_000.0


def passes_hysteresis(
    current_value: float,
    previously_in_set: bool,
    entry_thr: float,
    exit_thr: float,
) -> bool:
    """
    Decide whether an item belongs to a "qualifying set" given hysteresis.

    Args:
        current_value:    The latest observed value (e.g. score, SI%, ADV).
        previously_in_set: Whether this item was already in the set last cycle.
        entry_thr:        Value must be >= this to enter (when not in set).
        exit_thr:         Value must be < this to leave (when already in set).
                          MUST be < entry_thr.

    Returns:
        True  → item should be IN the set this cycle.
        False → item should be OUT.

    Behaviour:
        - Not in set, value < entry_thr        → stays out
        - Not in set, value >= entry_thr       → enters
        - In set,     value >= exit_thr        → stays in (deadband)
        - In set,     value < exit_thr         → leaves
    """
    if entry_thr <= exit_thr:
        raise ValueError(
            f"Hysteresis requires entry_thr > exit_thr "
            f"(got entry={entry_thr}, exit={exit_thr})"
        )
    if previously_in_set:
        # Only drop out when we cross the looser EXIT boundary
        return current_value >= exit_thr
    # Not previously in — need the tougher ENTRY boundary
    return current_value >= entry_thr
