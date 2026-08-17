"""
Shared US market trading-day/holiday calendar — single source of truth.

Extracted 2026-08-17 from scheduler.py::_is_trading_day(). Every
schedule.every().day.at(...) job in scheduler.py already gated on this
logic, but the two full-universe background threads (momentum monitor,
Supertrend universe monitor — both in price_alert_monitor.py's
_is_market_hours()) only checked weekday, not holidays: on July 4th /
Thanksgiving / Christmas / etc. falling on a weekday, they kept running
full yfinance scans while every other job correctly stood down. Rather than
duplicate the holiday list a third time (this codebase already has
_min_hold_satisfied and _cooldown_ok each reimplemented independently more
than once), both scheduler.py and price_alert_monitor.py now import from
here. See CLAUDE.md Incident Archive 2026-08-17.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def is_trading_day() -> bool:
    """True only on US market trading days (Mon-Fri, excluding NYSE federal
    holidays). Good Friday is not computed (rare edge case, matches the
    pre-extraction behavior)."""
    now = datetime.now(_ET)
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    year, month, day = now.year, now.month, now.day
    # Fixed holidays
    if (month, day) in [(1, 1), (7, 4), (12, 25)]:
        return False
    # New Year's / Christmas / Independence Day observed (if on weekend → adjacent weekday)
    if (month, day) in [(1, 2), (7, 3), (12, 24), (12, 26)]:
        if now.weekday() == 4:      # observed on Friday when holiday falls on Saturday
            return False
        if now.weekday() == 0:      # observed on Monday when holiday falls on Sunday
            return False
    # MLK Day — 3rd Monday of January
    if month == 1 and now.weekday() == 0 and 15 <= day <= 21:
        return False
    # Presidents Day — 3rd Monday of February
    if month == 2 and now.weekday() == 0 and 15 <= day <= 21:
        return False
    # Memorial Day — last Monday of May
    if month == 5 and now.weekday() == 0 and day >= 25:
        return False
    # Juneteenth — June 19 (observed)
    if (month, day) == (6, 19):
        return False
    if (month, day) == (6, 18) and now.weekday() == 4:   # observed Friday
        return False
    if (month, day) == (6, 20) and now.weekday() == 0:   # observed Monday
        return False
    # Labor Day — 1st Monday of September
    if month == 9 and now.weekday() == 0 and day <= 7:
        return False
    # Thanksgiving — 4th Thursday of November
    if month == 11 and now.weekday() == 3 and 22 <= day <= 28:
        return False
    # Good Friday — not easily computable, skip (rare edge case)
    return True
