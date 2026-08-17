"""
Test: src/trading_calendar.py — the shared holiday-aware trading-day check
extracted 2026-08-17 so scheduler.py and price_alert_monitor.py (the
momentum/Supertrend universe background threads) stop diverging on whether
today is a real trading day.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_trading_calendar.py -v
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import src.trading_calendar as tc

_ET = ZoneInfo("America/New_York")


def _fake_now(year, month, day, hour=12):
    return datetime(year, month, day, hour, 0, tzinfo=_ET)


class TestHolidaysExcluded:
    @pytest.mark.parametrize("y,m,d,label", [
        (2026, 1, 1, "New Year's Day"),
        (2026, 7, 4, "Independence Day (falls on a Saturday in 2026 — observed separately)"),
        (2026, 12, 25, "Christmas"),
        (2026, 11, 26, "Thanksgiving (4th Thursday of Nov 2026)"),
    ])
    def test_fixed_and_floating_holidays(self, y, m, d, label):
        with patch("src.trading_calendar.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: _fake_now(y, m, d)
            assert tc.is_trading_day() is False, label


class TestOrdinaryTradingDayIncluded:
    def test_ordinary_weekday(self):
        # 2026-08-18 is a Tuesday, no holiday.
        with patch("src.trading_calendar.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: _fake_now(2026, 8, 18)
            assert tc.is_trading_day() is True

    def test_weekend_excluded(self):
        # 2026-08-16 is a Sunday.
        with patch("src.trading_calendar.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: _fake_now(2026, 8, 16)
            assert tc.is_trading_day() is False


class TestThreadsNowHolidayAware:
    def test_is_market_hours_false_on_holiday_during_market_hours(self):
        """The actual bug this closes: momentum/Supertrend threads used to
        only check weekday, so a market holiday during normal trading hours
        (e.g. Thanksgiving 10am) still ran a full-universe scan."""
        import src.price_alert_monitor as pam

        with patch("src.trading_calendar.datetime") as mock_dt:
            # Thanksgiving 2026 (Nov 26) at 10:00 ET — inside 09:30-16:00.
            fake = _fake_now(2026, 11, 26, hour=10)
            mock_dt.now.side_effect = lambda tz=None: fake
            with patch.object(pam, "datetime") as mock_pam_dt:
                mock_pam_dt.now.side_effect = lambda tz=None: fake
                assert pam._is_market_hours() is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
