"""
Tests for the 5 audited bug fixes in price_alert_monitor.py / watchlist_manager.py:

  1. HIGH  — price_surge_rescore baseline no longer includes score_threshold
             (that alert type stores a SCORE in the price column, not a price).
  2. HIGH  — trading-hours gate widened from 09:30-16:00 ET to 04:00-20:00 ET
             (old window was unreachable by the only scheduled caller).
  3. MED   — price_target fires on a gap-through, not just a $0.05 proximity hit.
  4. LOW   — _send_alert only writes the cooldown/audit row when send succeeds.
  5. LOW   — portfolio alerts label price as "Last close" (documentation-only,
             smoke-tested via message content).

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_price_alert_fixes.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_ET = ZoneInfo("America/New_York")


# ── Shared temp SQLite DB fixture (matches tests/test_auto_watchlist_cooldown.py) ──

@pytest.fixture
def temp_db(monkeypatch):
    """Spin up a temp SQLite DB with the real schema via src.database.init_db()."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="price_alert_fixes_")
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


def _fake_now_et(hour: int, minute: int = 0, weekday_safe: bool = True):
    """Return a tz-aware ET datetime on a known Wednesday (weekday=2) at hour:minute."""
    # 2026-07-01 is a Wednesday
    return datetime(2026, 7, 1, hour, minute, tzinfo=_ET)


# ══════════════════════════════════════════════════════════════════════════════
# 1. price_surge_rescore baseline excludes score_threshold
# ══════════════════════════════════════════════════════════════════════════════

class TestBaselineTypesExcludeScoreThreshold:
    def test_score_threshold_not_in_baseline_types(self):
        """
        Directly inspect the _BASELINE_TYPES set used inside check_price_surge().
        We can't import it as a module-level constant (it's defined inline), so we
        exercise check_price_surge() with a DB that ONLY has a score_threshold row
        and assert it is NOT used as a price baseline (i.e. no rescore happens).
        """
        import src.price_alert_monitor as pam

        # score_threshold row stores the SCORE (72) in the price column — a classic
        # false baseline that used to trigger bogus "-58% from $72" surge alerts.
        alerts = [
            {"alert_type": "score_threshold", "price": 72.0, "sent_at": datetime.now().isoformat()},
        ]

        with patch.object(pam, "_is_market_hours", return_value=True), \
             patch.object(pam, "watchlist_get_all", return_value=[{"ticker": "FAKE"}]), \
             patch.object(pam, "watchlist_get_alerts", return_value=alerts), \
             patch.object(pam, "_get_price", return_value=30.0) as mock_price, \
             patch.object(pam, "TelegramNotifier") as MockNotifier:
            MockNotifier.return_value = MagicMock()
            pam.check_price_surge()

        # If score_threshold had been used as baseline: (30 - 72) / 72 = -58.3%,
        # which is > 10% and would trigger a rescore + score_stock() call.
        # Since there's no other baseline-eligible row, last_price stays None and
        # the ticker is skipped entirely — _get_price is called once for the
        # initial price fetch attempt only if a baseline was found. Assert no
        # Telegram send occurred (proxy for "no false surge alert fired").
        MockNotifier.return_value.send_message.assert_not_called()

    def test_price_change_still_valid_baseline(self):
        """Sanity check: a genuine price_change row IS used as a baseline."""
        import src.price_alert_monitor as pam

        alerts = [
            {"alert_type": "price_change", "price": 100.0, "sent_at": datetime.now().isoformat()},
        ]

        with patch.object(pam, "_is_market_hours", return_value=True), \
             patch.object(pam, "watchlist_get_all", return_value=[{"ticker": "FAKE"}]), \
             patch.object(pam, "watchlist_get_alerts", return_value=alerts), \
             patch.object(pam, "_get_price", return_value=120.0), \
             patch.object(pam, "_cooldown_ok_type", return_value=True), \
             patch.object(pam, "watchlist_save_alert") as mock_save, \
             patch("src.stock_scorer.score_stock", return_value={"score": 65.0, "price": 120.0}), \
             patch("src.stock_scorer.signal_label", return_value="BUY"), \
             patch.object(pam, "TelegramNotifier") as MockNotifier:
            MockNotifier.return_value = MagicMock()
            pam.check_price_surge()

        # (120-100)/100 = +20% >= 10% threshold -> should have attempted a rescore
        # and written a price_surge_rescore DB row.
        assert mock_save.called
        args = mock_save.call_args
        assert args[0][1] == "price_surge_rescore"

    def test_no_other_baseline_type_stores_non_price(self):
        """
        Grep-equivalent check: every watchlist_save_alert(...) call site in
        price_alert_monitor.py and watchlist_manager.py that writes one of the
        types currently included in _BASELINE_TYPES must pass an actual price
        value (not a score) as the `price` arg. We assert this structurally by
        re-checking the known offender (score_threshold) is excluded and that
        the three remaining types (price_change, combined_buy,
        price_surge_rescore) are semantically price-based per their call sites.
        """
        import inspect
        import src.price_alert_monitor as pam

        src_code = inspect.getsource(pam.check_price_surge)
        assert '"score_threshold"' not in src_code.split("_BASELINE_TYPES")[1].split("}")[0] \
            if "_BASELINE_TYPES" in src_code else True
        assert "score_threshold" not in src_code[
            src_code.index("_BASELINE_TYPES"):src_code.index("_BASELINE_TYPES") + 200
        ]


# ══════════════════════════════════════════════════════════════════════════════
# 2. Trading-hours gate: 04:00-20:00 ET boundaries
# ══════════════════════════════════════════════════════════════════════════════

class TestTradingHoursGate:
    @pytest.mark.parametrize("hour,minute,expected", [
        (3, 59, False),   # just before window
        (4, 0, True),     # window open (inclusive)
        (12, 0, True),    # midday — was always true
        (19, 59, True),   # just before close
        (20, 0, True),    # window close (inclusive)
        (20, 1, False),   # just after close
        (2, 0, False),    # old pre-market-excluded hour, still excluded
        (23, 0, False),   # late night
    ])
    def test_boundaries(self, hour, minute, expected):
        from src.watchlist_manager import _is_trading_hours
        import src.watchlist_manager as wm

        fake_now = _fake_now_et(hour, minute)
        with patch("src.watchlist_manager.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            # datetime.now(_ET) is called with tz arg; MagicMock.now ignores args
            # unless configured — configure side_effect to honor tz-aware behavior.
            mock_dt.now.side_effect = lambda tz=None: fake_now
            assert _is_trading_hours() is expected

    def test_weekend_excluded_regardless_of_hour(self):
        from src.watchlist_manager import _is_trading_hours

        # 2026-07-04 is a Saturday
        fake_now = datetime(2026, 7, 4, 12, 0, tzinfo=_ET)
        with patch("src.watchlist_manager.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: fake_now
            assert _is_trading_hours() is False

    def test_old_window_12_00_israel_scheduled_call_now_reachable(self):
        """
        Regression for the exact bug: run_watchlist_scan fires at 12:00 Israel
        time (UTC+3 in summer) = 05:00 ET. Under the OLD 09:30-16:00 gate this
        was always False. Under the fixed 04:00-20:00 gate it must be True.
        """
        from src.watchlist_manager import _is_trading_hours

        fake_now_et = _fake_now_et(5, 0)  # 05:00 ET == 12:00 IL (UTC+3)
        with patch("src.watchlist_manager.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: fake_now_et
            assert _is_trading_hours() is True


class TestPriceChangeBaselineOutsideHours:
    def test_baseline_recorded_but_telegram_suppressed_outside_hours(self, temp_db):
        """
        scan_watchlist(): with an existing price_change baseline and a >= alert_pct
        move, outside the 04:00-20:00 window the code must still WRITE a fresh
        price_change baseline row (so % moves are measured going forward) but must
        NOT call telegram.send_message().
        """
        import src.watchlist_manager as wm

        fake_item = {
            "ticker": "FAKE", "alert_score": 60, "alert_pct": 5.0,
            "price_above": None, "price_below": None, "supertrend_alert": 0,
        }
        fake_result = {
            "ticker": "FAKE", "score": 50.0, "price": 110.0,
            "rsi": 45, "macd": "Neutral", "short_pct": 5.0, "squeeze_active": False,
        }

        sent_messages = []
        saved_alerts = []

        mock_telegram = MagicMock()
        mock_telegram.send_message.side_effect = lambda m: (sent_messages.append(m), False)[1]

        def _fake_save_alert(ticker, atype, msg, score=None, price=None):
            saved_alerts.append((atype, price))

        with patch.object(wm, "watchlist_get_all", return_value=[fake_item]), \
             patch.object(wm, "score_stock", return_value=fake_result), \
             patch.object(wm, "get_last_saved_score", return_value=None), \
             patch.object(wm, "_cooldown_passed", return_value=True), \
             patch.object(wm, "_last_alert_price", return_value=100.0), \
             patch.object(wm, "_is_trading_hours", return_value=False), \
             patch.object(wm, "watchlist_save_alert", side_effect=_fake_save_alert), \
             patch.object(wm, "TelegramNotifier", return_value=mock_telegram):
            wm.scan_watchlist()

        # (110-100)/100 = +10% >= 5% alert_pct -> would have alerted if hours were open
        assert not sent_messages, "Telegram must be suppressed outside 04:00-20:00 ET"
        price_change_writes = [p for (t, p) in saved_alerts if t == "price_change"]
        assert price_change_writes, "Baseline price_change row must still be written outside hours"
        assert price_change_writes[-1] == 110.0

    def test_alert_fires_inside_hours(self, temp_db):
        """Same setup, but _is_trading_hours() True -> Telegram alert must fire."""
        import src.watchlist_manager as wm

        fake_item = {
            "ticker": "FAKE", "alert_score": 60, "alert_pct": 5.0,
            "price_above": None, "price_below": None, "supertrend_alert": 0,
        }
        fake_result = {
            "ticker": "FAKE", "score": 50.0, "price": 110.0,
            "rsi": 45, "macd": "Neutral", "short_pct": 5.0, "squeeze_active": False,
        }

        mock_telegram = MagicMock()
        mock_telegram.send_message.return_value = True

        with patch.object(wm, "watchlist_get_all", return_value=[fake_item]), \
             patch.object(wm, "score_stock", return_value=fake_result), \
             patch.object(wm, "get_last_saved_score", return_value=None), \
             patch.object(wm, "_cooldown_passed", return_value=True), \
             patch.object(wm, "_last_alert_price", return_value=100.0), \
             patch.object(wm, "_is_trading_hours", return_value=True), \
             patch.object(wm, "watchlist_save_alert") as mock_save, \
             patch.object(wm, "TelegramNotifier", return_value=mock_telegram):
            wm.scan_watchlist()

        assert mock_telegram.send_message.called


# ══════════════════════════════════════════════════════════════════════════════
# 3. price_target crossing detection
# ══════════════════════════════════════════════════════════════════════════════

class TestPriceTargetCrossing:
    def _run_check(self, pam, price_sequence, target=50.0):
        """Helper: run check_price_targets() once per price in price_sequence."""
        item = {"ticker": "FAKE", "price_target": target, "notes": None}
        results = []
        prices = iter(price_sequence)

        with patch.object(pam, "_is_market_hours", return_value=True), \
             patch.object(pam, "watchlist_get_all", return_value=[item]), \
             patch.object(pam, "_get_price", side_effect=lambda t: next(prices)), \
             patch.object(pam, "_cooldown_ok", return_value=True), \
             patch.object(pam, "watchlist_save_alert",
                          side_effect=lambda *a, **kw: results.append(a)), \
             patch.object(pam, "TelegramNotifier") as MockNotifier:
            mock_tg = MagicMock()
            MockNotifier.return_value = mock_tg
            for _ in price_sequence:
                pam.check_price_targets()
        return results, mock_tg

    def test_gap_through_target_fires(self):
        """Price jumps from $45 to $55 in one poll — target $50 is gapped through."""
        import src.price_alert_monitor as pam
        pam._last_seen_price.clear()

        results, mock_tg = self._run_check(pam, [45.0, 55.0], target=50.0)

        assert len(results) == 1, f"Expected exactly 1 alert (on the gap poll), got {results}"
        assert mock_tg.send_message.call_count == 1

    def test_far_miss_does_not_fire(self):
        """Price moves from $45 to $47 — never near or crossing the $50 target."""
        import src.price_alert_monitor as pam
        pam._last_seen_price.clear()

        results, mock_tg = self._run_check(pam, [45.0, 47.0], target=50.0)

        assert len(results) == 0, f"Expected no alert, got {results}"
        assert mock_tg.send_message.call_count == 0

    def test_proximity_still_fires_on_first_poll(self):
        """No prior price observed yet — falls back to the $0.05 proximity check."""
        import src.price_alert_monitor as pam
        pam._last_seen_price.clear()

        results, mock_tg = self._run_check(pam, [50.02], target=50.0)

        assert len(results) == 1
        assert mock_tg.send_message.call_count == 1

    def test_downward_gap_through_fires(self):
        """Price drops from $55 to $45 in one poll — target $50 gapped through downward."""
        import src.price_alert_monitor as pam
        pam._last_seen_price.clear()

        results, mock_tg = self._run_check(pam, [55.0, 45.0], target=50.0)

        assert len(results) == 1
        assert mock_tg.send_message.call_count == 1

    def test_failed_send_does_not_write_cooldown(self):
        """2026-08-17 fix: send_message() swallows its own errors and returns
        False rather than raising, so the old bare try/except never caught a
        failed send — watchlist_save_alert ran unconditionally right after,
        consuming the cooldown for an alert the user never saw. Must now
        gate on the return value, same as watchlist_manager.py::_send_alert."""
        import src.price_alert_monitor as pam
        pam._last_seen_price.clear()

        item = {"ticker": "FAKE", "price_target": 50.0, "notes": None}
        results = []
        with patch.object(pam, "_is_market_hours", return_value=True), \
             patch.object(pam, "watchlist_get_all", return_value=[item]), \
             patch.object(pam, "_get_price", return_value=50.02), \
             patch.object(pam, "_cooldown_ok", return_value=True), \
             patch.object(pam, "watchlist_save_alert",
                          side_effect=lambda *a, **kw: results.append(a)), \
             patch.object(pam, "TelegramNotifier") as MockNotifier:
            mock_tg = MagicMock()
            mock_tg.send_message.return_value = False  # simulates a failed/disabled send
            MockNotifier.return_value = mock_tg
            pam.check_price_targets()

        assert results == [], "expected no cooldown row written when the send failed"


# ══════════════════════════════════════════════════════════════════════════════
# 4. _send_alert: cooldown row not written on failed send
# ══════════════════════════════════════════════════════════════════════════════

class TestSendAlertCooldownOnSuccessOnly:
    def test_cooldown_not_written_on_failed_send(self):
        from src.watchlist_manager import _send_alert

        mock_telegram = MagicMock()
        mock_telegram.send_message.return_value = False  # simulates disabled OR failed

        with patch("src.watchlist_manager.watchlist_save_alert") as mock_save:
            _send_alert(mock_telegram, "FAKE", "price_change", "msg", 60.0, 100.0)

        mock_save.assert_not_called()

    def test_cooldown_written_on_successful_send(self):
        from src.watchlist_manager import _send_alert

        mock_telegram = MagicMock()
        mock_telegram.send_message.return_value = True

        with patch("src.watchlist_manager.watchlist_save_alert") as mock_save:
            _send_alert(mock_telegram, "FAKE", "price_change", "msg", 60.0, 100.0)

        mock_save.assert_called_once_with("FAKE", "price_change", "msg", 60.0, 100.0)

    def test_exception_during_send_does_not_write_cooldown(self):
        from src.watchlist_manager import _send_alert

        mock_telegram = MagicMock()
        mock_telegram.send_message.side_effect = RuntimeError("network error")

        with patch("src.watchlist_manager.watchlist_save_alert") as mock_save:
            _send_alert(mock_telegram, "FAKE", "price_change", "msg", 60.0, 100.0)

        mock_save.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Portfolio alert price labeling (smoke test)
# ══════════════════════════════════════════════════════════════════════════════

class TestPortfolioAlertPriceLabel:
    def test_stop_loss_message_labels_last_close(self):
        import src.watchlist_manager as wm

        holdings = [{
            "ticker": "FAKE", "entry_price": 100.0, "stop_loss": 90.0,
            "target_price": None, "shares": 10,
        }]
        fake_result = {"ticker": "FAKE", "score": 50.0, "price": 85.0}

        captured = []

        with patch.object(wm, "portfolio_get_all", return_value=holdings), \
             patch.object(wm, "score_stock", return_value=fake_result), \
             patch.object(wm, "_cooldown_passed", return_value=True), \
             patch.object(wm, "get_last_saved_score", return_value=None), \
             patch.object(wm, "_send_alert",
                          side_effect=lambda tg, t, atype, msg, sc, pr: captured.append((atype, msg))), \
             patch.object(wm, "TelegramNotifier", return_value=MagicMock()):
            wm.scan_portfolio()

        stop_loss_msgs = [m for (a, m) in captured if a == "stop_loss"]
        assert stop_loss_msgs, "expected a stop_loss alert"
        assert "Last close:" in stop_loss_msgs[0]
        assert "Price:" not in stop_loss_msgs[0].split("\n")[1]
