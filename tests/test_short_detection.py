"""
Tests for short-position detection and the long-only halt.

Context: on 2026-08-05 the paper account held 14 real short positions worth
-$2.37M against an $853k NLV, and not one appeared in ibkr_positions —
position_tracker.sync_positions() filtered every negative-share row out on the
assumption they were artifacts. Every veto, the /positions command and
alert_monitor were blind to them for weeks. These tests make that unrepeatable.
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    db_path = tmp_path / "t.db"

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        return c

    import src.position_tracker  # noqa: F401
    import src.order_manager  # noqa: F401
    monkeypatch.setattr("src.database.get_connection", _conn)
    monkeypatch.setattr("src.position_tracker.get_connection", _conn)
    monkeypatch.setattr("src.order_manager.get_connection", _conn)
    monkeypatch.setattr("src.execution_engine.get_connection", _conn)

    c = _conn()
    c.executescript("""
        CREATE TABLE ibkr_positions (
            ticker TEXT PRIMARY KEY, shares REAL, avg_cost REAL,
            unrealized_pnl REAL, market_value REAL, last_synced TEXT,
            exit_tier INTEGER DEFAULT 0);
        CREATE TABLE watchlist_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, alert_type TEXT,
            message TEXT, sent_at TEXT, score REAL, price REAL);
        CREATE TABLE daily_pnl (date TEXT PRIMARY KEY, day_pnl REAL,
            net_liquidation REAL, recorded_at TEXT);
    """)
    c.commit()
    c.close()

    from src import order_manager
    order_manager.set_paused(False)
    yield _conn
    order_manager.set_paused(False)


def _tracker(positions):
    from src.position_tracker import PositionTracker
    ibkr = MagicMock()
    ibkr.get_positions.return_value = positions
    return PositionTracker(ibkr)


LONG = {"shares": 100.0, "avg_cost": 50.0, "unrealized_pnl": 10.0, "market_value": 5010.0}
SHORT = {"shares": -13247.0, "avg_cost": 19.78, "unrealized_pnl": -4195.0,
         "market_value": -266264.0}


class TestShortsAreRecorded:
    def test_short_position_is_persisted_not_dropped(self, _db):
        """Regression: the filter that hid $2.37M of shorts for weeks."""
        with patch("src.telegram_notifier.TelegramNotifier"):
            _tracker({"AAA": LONG, "KSS": SHORT}).sync_positions()
        with _db() as c:
            rows = {r["ticker"]: r["shares"] for r in c.execute(
                "SELECT ticker, shares FROM ibkr_positions")}
        assert rows["KSS"] == pytest.approx(-13247.0)
        assert rows["AAA"] == pytest.approx(100.0)

    def test_shorts_are_not_deleted_as_closed(self, _db):
        with patch("src.telegram_notifier.TelegramNotifier"):
            t = _tracker({"KSS": SHORT})
            t.sync_positions()
            t.sync_positions()
        with _db() as c:
            n = c.execute("SELECT COUNT(*) n FROM ibkr_positions").fetchone()["n"]
        assert n == 1


class TestEmptyResponseIsNotFlat:
    """Regression: on 2026-08-05 a Gateway restart mid-login returned zero
    positions, sync_positions wiped 15 shorts and 22 longs from the DB, and they
    reappeared three minutes later. An empty answer from a not-ready broker is
    not the same as an empty account."""

    def _seeded(self, db):
        with db() as c:
            c.execute("INSERT INTO ibkr_positions (ticker, shares, avg_cost,"
                      " unrealized_pnl, market_value, last_synced)"
                      " VALUES ('KSS',-13247,19.78,-4195,-266264,'x')")
            c.execute("INSERT INTO ibkr_positions (ticker, shares, avg_cost,"
                      " unrealized_pnl, market_value, last_synced)"
                      " VALUES ('AAA',100,50,10,5010,'x')")

    def test_empty_positions_from_unready_account_keeps_rows(self, _db):
        from src.position_tracker import PositionTracker
        self._seeded(_db)
        ib = MagicMock()
        ib.get_positions.return_value = {}
        ib.get_account_summary.return_value = {"net_liquidation": 0.0}  # mid-login

        PositionTracker(ib).sync_positions()

        with _db() as c:
            n = c.execute("SELECT COUNT(*) n FROM ibkr_positions").fetchone()["n"]
        assert n == 2, "must not assume flat while the account is still loading"

    def test_empty_positions_from_ready_account_clears_rows(self, _db):
        from src.position_tracker import PositionTracker
        self._seeded(_db)
        ib = MagicMock()
        ib.get_positions.return_value = {}
        ib.get_account_summary.return_value = {"net_liquidation": 250_000.0}

        PositionTracker(ib).sync_positions()

        with _db() as c:
            n = c.execute("SELECT COUNT(*) n FROM ibkr_positions").fetchone()["n"]
        assert n == 0, "a live account reporting no positions really is flat"

    def test_readiness_check_failure_is_treated_as_not_ready(self, _db):
        from src.position_tracker import PositionTracker
        self._seeded(_db)
        ib = MagicMock()
        ib.get_positions.return_value = {}
        ib.get_account_summary.side_effect = RuntimeError("not connected")

        PositionTracker(ib).sync_positions()

        with _db() as c:
            n = c.execute("SELECT COUNT(*) n FROM ibkr_positions").fetchone()["n"]
        assert n == 2


class TestHaltOnShort:
    def test_trading_is_paused_when_a_short_appears(self, _db):
        from src import order_manager
        assert not order_manager.is_paused()
        with patch("src.telegram_notifier.TelegramNotifier"):
            _tracker({"KSS": SHORT}).sync_positions()
        assert order_manager.is_paused(), "a long-only bot must halt on a short"

    def test_no_pause_when_all_positions_are_long(self, _db):
        from src import order_manager
        with patch("src.telegram_notifier.TelegramNotifier"):
            _tracker({"AAA": LONG}).sync_positions()
        assert not order_manager.is_paused()

    def test_telegram_alarm_is_sent_with_the_detail(self, _db):
        with patch("src.telegram_notifier.TelegramNotifier") as tn:
            _tracker({"KSS": SHORT}).sync_positions()
            body = tn.return_value.send_message.call_args[0][0]
        assert "SHORT POSITIONS DETECTED" in body
        assert "KSS" in body
        assert "PAUSED" in body

    def test_alarm_is_throttled_but_pause_is_not(self, _db):
        """A 5-minute sync loop must not flood Telegram, but must keep the halt on."""
        from src import order_manager
        with patch("src.telegram_notifier.TelegramNotifier") as tn:
            t = _tracker({"KSS": SHORT})
            t.sync_positions()
            order_manager.set_paused(False)   # simulate an operator /resume
            t.sync_positions()
            assert tn.return_value.send_message.call_count == 1, "alarm must throttle"
        assert order_manager.is_paused(), "pause must be re-applied every cycle"

    def test_alarm_fires_again_for_a_different_ticker_set(self, _db):
        with patch("src.telegram_notifier.TelegramNotifier") as tn:
            _tracker({"KSS": SHORT}).sync_positions()
            _tracker({"KSS": SHORT, "EXPE": dict(SHORT, shares=-1711.0)}).sync_positions()
            assert tn.return_value.send_message.call_count == 2


class TestExposureIsLongOnly:
    def test_short_reports_zero_exposure(self, _db):
        with patch("src.telegram_notifier.TelegramNotifier"):
            t = _tracker({"KSS": SHORT})
            t.sync_positions()
        assert t.get_current_exposure("KSS") == 0.0, \
            "a short must not read as 'something to sell'"

    def test_long_reports_its_market_value(self, _db):
        with patch("src.telegram_notifier.TelegramNotifier"):
            t = _tracker({"AAA": LONG})
            t.sync_positions()
        assert t.get_current_exposure("AAA") == pytest.approx(5010.0)

    def test_sell_on_a_short_is_vetoed_by_layer_minus_1(self, _db):
        """The path that would have deepened the short."""
        import src.execution_engine as engine
        with patch("src.telegram_notifier.TelegramNotifier"):
            t = _tracker({"KSS": SHORT})
            t.sync_positions()
        engine.set_position_tracker(t)
        try:
            reasons = []
            out = engine.evaluate_trade(
                "KSS", {"price": 20.1}, signal_type="SELL", reasons_out=reasons)
            assert out is None
            assert any("L-1" in r for r in reasons), reasons
        finally:
            engine.set_position_tracker(None)
