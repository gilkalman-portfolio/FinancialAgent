"""
Tests for benchmark-relative reporting in the weekly digest, and for the
account-event guard on the daily P&L fallback.

Both fix metrics that actively misled: the digest reported a 55.9% win rate for
three months while the same signals were at -0.25% excess vs IWM, and the P&L
fallback would book tomorrow's paper-account reset as a +$146k profit day.
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
    p = tmp_path / "t.db"

    def _conn():
        c = sqlite3.connect(str(p))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        return c

    import src.forward_signals  # noqa: F401
    import src.position_tracker  # noqa: F401
    monkeypatch.setattr("src.database.get_connection", _conn)
    monkeypatch.setattr("src.forward_signals.get_connection", _conn)
    monkeypatch.setattr("src.position_tracker.get_connection", _conn)

    c = _conn()
    c.executescript("""
        CREATE TABLE forward_signals (id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, signal_ts TEXT, signal_type TEXT, entry_price REAL,
            composite_score REAL, return_7d_pct REAL, return_14d_pct REAL,
            return_30d_pct REAL, fill_price REAL, data_quality_flag TEXT);
        CREATE TABLE ibkr_positions (ticker TEXT PRIMARY KEY, shares REAL,
            avg_cost REAL, unrealized_pnl REAL, market_value REAL, last_synced TEXT);
        CREATE TABLE daily_pnl (date TEXT PRIMARY KEY, day_pnl REAL,
            net_liquidation REAL, recorded_at TEXT);
    """)
    c.commit()
    c.close()
    yield _conn


# ── Digest reports excess, not just win rate ───────────────────────────────

class TestDigestReportsExcess:
    def _signals(self, db, n=10, ret=5.0):
        with db() as c:
            for i in range(n):
                c.execute(
                    "INSERT INTO forward_signals (ticker, signal_ts, signal_type,"
                    " entry_price, return_7d_pct) VALUES (?,?,'BUY',10,?)",
                    (f"T{i}", (datetime.now() - timedelta(days=2)).isoformat(), ret))

    def test_insignificant_excess_is_labelled_as_not_edge(self, _db):
        from src import forward_signals as fs
        self._signals(_db)
        d = fs.weekly_digest(7)
        d["excess"] = {"benchmark": "IWM", "n": 145, "mean_excess_pct": -0.25,
                       "t_stat": -0.33, "beat_rate_pct": 49.7, "significant": False}
        msg = fs.format_digest_message(d)
        assert "vs IWM" in msg
        assert "-0.25%" in msg
        assert "NOT distinguishable from zero" in msg
        assert "market beta, not edge" in msg

    def test_significant_excess_is_labelled_meaningful(self, _db):
        from src import forward_signals as fs
        self._signals(_db)
        d = fs.weekly_digest(7)
        d["excess"] = {"benchmark": "IWM", "n": 300, "mean_excess_pct": 1.4,
                       "t_stat": 3.1, "beat_rate_pct": 58.0, "significant": True}
        msg = fs.format_digest_message(d)
        assert "statistically meaningful" in msg
        assert "market beta" not in msg

    def test_missing_benchmark_says_unbenchmarked(self, _db):
        """Silence here is what caused the problem — it must be stated."""
        from src import forward_signals as fs
        self._signals(_db)
        d = fs.weekly_digest(7)
        d["excess"] = None
        msg = fs.format_digest_message(d)
        assert "UNBENCHMARKED" in msg

    def test_benchmark_failure_never_breaks_the_digest(self, _db):
        from src import forward_signals as fs
        self._signals(_db)
        with patch.object(fs, "benchmark_excess", side_effect=RuntimeError("no net")):
            d = fs.weekly_digest(7)
        assert d["excess"] is None
        assert isinstance(fs.format_digest_message(d), str)

    def test_excess_math_is_signal_minus_benchmark(self, _db):
        """Signals +5%, benchmark +3% -> excess +2%."""
        from src import forward_signals as fs
        import pandas as pd
        # 20 days old, so the 7-day benchmark window has actually elapsed.
        with _db() as c:
            for i in range(12):
                c.execute(
                    "INSERT INTO forward_signals (ticker, signal_ts, signal_type,"
                    " entry_price, return_7d_pct) VALUES (?,?,'BUY',10,5.0)",
                    (f"T{i}", (datetime.now() - timedelta(days=20)).isoformat()))

        idx = pd.date_range(datetime.now() - timedelta(days=60), periods=60, freq="D")
        close = pd.Series([100.0 * (1.03 ** (i / 7.0)) for i in range(60)], index=idx)
        fake = MagicMock()
        fake.history.return_value = pd.DataFrame({"Close": close})

        with patch("yfinance.Ticker", return_value=fake):
            out = fs.benchmark_excess(lookback_days=90)
        assert out is not None
        assert out["mean_excess_pct"] == pytest.approx(2.0, abs=0.15)
        assert out["n"] == 12


# ── Daily P&L must not book account events as profit ───────────────────────

class TestAccountEventGuard:
    def _tracker(self, nlv, day_pnl=0.0):
        from src.position_tracker import PositionTracker
        ib = MagicMock()
        ib.get_account_summary.return_value = {"net_liquidation": nlv, "day_pnl": day_pnl}
        return PositionTracker(ib)

    def _prior(self, db, date, nlv):
        with db() as c:
            c.execute("INSERT INTO daily_pnl (date, day_pnl, net_liquidation, recorded_at)"
                      " VALUES (?,0,?,?)", (date, nlv, datetime.now().isoformat()))

    def _positions(self, db, gross):
        with db() as c:
            c.execute("INSERT INTO ibkr_positions (ticker, shares, avg_cost,"
                      " unrealized_pnl, market_value, last_synced) VALUES"
                      " ('AAA',100,10,0,?,?)", (gross, datetime.now().isoformat()))

    def test_paper_reset_rebound_is_not_booked_as_profit(self, _db):
        """Tomorrow's reset: NLV 853k -> 1,000k with a near-empty book."""
        from src import position_tracker
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._prior(_db, yesterday, 853_301)
        self._positions(_db, gross=5_000)          # tiny book after reset

        t = self._tracker(nlv=1_000_000)
        with patch.object(position_tracker, "datetime") as dt:
            dt.now.return_value = datetime.now().replace(hour=20, minute=0)
            dt.side_effect = datetime
            t.record_daily_pnl()

        with _db() as c:
            row = c.execute("SELECT day_pnl FROM daily_pnl WHERE date != ?",
                            (yesterday,)).fetchone()
        assert row is not None
        assert row["day_pnl"] == 0.0, \
            f"a +$146k reset rebound must not book as P&L, got {row['day_pnl']}"

    def test_fabricated_gain_against_a_stale_prior_nlv_is_rejected(self, _db):
        """Observed live 2026-08-05: prior stored NLV 853,301 vs actual 1,349,564
        produced a recorded +$496k 'profit'. 37% of NLV in a day is not a market
        move."""
        from src import position_tracker
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._prior(_db, yesterday, 853_301)
        self._positions(_db, gross=2_884_074)     # the huge short book

        t = self._tracker(nlv=1_349_564)
        with patch.object(position_tracker, "datetime") as dt:
            dt.now.return_value = datetime.now().replace(hour=20, minute=0)
            dt.side_effect = datetime
            t.record_daily_pnl()

        with _db() as c:
            row = c.execute("SELECT day_pnl FROM daily_pnl WHERE date != ?",
                            (yesterday,)).fetchone()
        assert row["day_pnl"] == 0.0, \
            f"a +$496k jump must not be recorded as P&L, got {row['day_pnl']}"

    def test_large_real_loss_is_still_recorded(self, _db):
        """Losses get more room: discarding a real crash would zero day_pnl and
        silently disable the Layer 0 veto."""
        from src import position_tracker
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._prior(_db, yesterday, 1_000_000)
        self._positions(_db, gross=900_000)

        t = self._tracker(nlv=820_000)            # -18%: brutal, but a real day
        with patch.object(position_tracker, "datetime") as dt:
            dt.now.return_value = datetime.now().replace(hour=20, minute=0)
            dt.side_effect = datetime
            t.record_daily_pnl()

        with _db() as c:
            row = c.execute("SELECT day_pnl FROM daily_pnl WHERE date != ?",
                            (yesterday,)).fetchone()
        assert row["day_pnl"] == pytest.approx(-180_000.0)

    def test_real_move_within_gross_exposure_is_kept(self, _db):
        from src import position_tracker
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._prior(_db, yesterday, 1_000_000)
        self._positions(_db, gross=500_000)

        t = self._tracker(nlv=990_000)             # -$10k on a $500k book: plausible
        with patch.object(position_tracker, "datetime") as dt:
            dt.now.return_value = datetime.now().replace(hour=20, minute=0)
            dt.side_effect = datetime
            t.record_daily_pnl()

        with _db() as c:
            row = c.execute("SELECT day_pnl FROM daily_pnl WHERE date != ?",
                            (yesterday,)).fetchone()
        assert row["day_pnl"] == pytest.approx(-10_000.0)

    def test_stale_nlv_is_refused_as_a_sizing_input(self, _db):
        """The $853k -> $250k reset: a stale stored NLV is 3.4x the truth and
        would size every trade 3.4x too large."""
        from src.position_tracker import PositionTracker
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        with _db() as c:
            c.execute("INSERT INTO daily_pnl VALUES (?,0,853301,?)",
                      (old, datetime.now().isoformat()))
        ib = MagicMock()
        ib.get_account_summary.return_value = {"net_liquidation": 0.0}  # IBKR gap
        assert PositionTracker(ib).get_portfolio_value() == 0.0, \
            "must refuse a stale value, not size from it"

    def test_recent_nlv_is_used(self, _db):
        from src.position_tracker import PositionTracker
        today = datetime.now().strftime("%Y-%m-%d")
        with _db() as c:
            c.execute("INSERT INTO daily_pnl VALUES (?,0,250000,?)",
                      (today, datetime.now().isoformat()))
        ib = MagicMock()
        ib.get_account_summary.return_value = {"net_liquidation": 0.0}
        assert PositionTracker(ib).get_portfolio_value() == pytest.approx(250_000.0)

    def test_live_ibkr_value_wins_over_db(self, _db):
        from src.position_tracker import PositionTracker
        today = datetime.now().strftime("%Y-%m-%d")
        with _db() as c:
            c.execute("INSERT INTO daily_pnl VALUES (?,0,999999,?)",
                      (today, datetime.now().isoformat()))
        ib = MagicMock()
        ib.get_account_summary.return_value = {"net_liquidation": 250_000.0}
        assert PositionTracker(ib).get_portfolio_value() == pytest.approx(250_000.0)

    def test_gross_exposure_counts_shorts_by_absolute_value(self, _db):
        from src.position_tracker import PositionTracker
        with _db() as c:
            c.execute("INSERT INTO ibkr_positions VALUES ('L',100,10,0,50000,'x')")
            c.execute("INSERT INTO ibkr_positions VALUES ('S',-100,10,0,-30000,'x')")
        assert PositionTracker(MagicMock())._gross_exposure() == pytest.approx(80_000.0)
