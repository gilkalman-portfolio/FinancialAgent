"""
Tests for the News-Catalyst Event-Study Measurement's data-capture layer:
src.forward_signals.record_watch_signals() and the schema/query changes it
required in src/forward_signals.py, src/database.py, and
src/telegram_command_handler.py.

Background (CLAUDE.md, "PLANNED: News-Catalyst Event-Study Measurement",
designed 2026-08-25/26): measure whether a momentum/supertrend technical hit
plus fresh news of a given sentiment direction predicts forward returns
differently than a hit with no news — logged as signal_type='WATCH' rows in
the existing forward_signals table, for BOTH tickers with news and tickers
without (the no-news group is the control, not something to skip).

Two things this suite exists to pin down:
  1. Dedup key is (ticker, sentiment_direction), not (ticker, time_window) and
     not (ticker, article_id) — a same-direction repeat must not create a new
     row; a direction change must.
  2. Introducing WATCH rows into forward_signals at full-scanner-hit frequency
     (hundreds/cycle) must NOT corrupt anything that previously assumed every
     row was a real BUY/SELL trade signal: the weekly digest, the LLM curation
     comparison, record_fill()'s "most recent row for this ticker" lookup, and
     telegram_command_handler's /status "last signal" line all read
     forward_signals without a signal_type filter before this feature existed
     and needed one added.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_catalyst_event_study_watch_signals.py -v
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def temp_db(monkeypatch):
    """Real init_db() schema (including the new migration) on a throwaway file."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="event_study_")
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


def _news(sentiment="positive", title="Company announces big news", minutes_ago=10):
    return {
        "title": title,
        "published_utc": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        "sentiment": sentiment,
        "publisher_name": "Example Wire",
        "url": "https://example.com/a",
    }


# ── database migration ───────────────────────────────────────────────────────

class TestMigration:
    def test_new_columns_exist_after_init_db(self, temp_db):
        import sqlite3
        conn = sqlite3.connect(str(temp_db))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(forward_signals)")}
        conn.close()
        for col in ("price_after_1d", "price_after_2d", "price_after_3d",
                    "return_1d_pct", "return_2d_pct", "return_3d_pct"):
            assert col in cols, f"missing migrated column {col}"

    def test_existing_buy_row_gets_null_in_new_columns(self, temp_db):
        from src.forward_signals import SignalRecord, record_signal
        import sqlite3

        sid = record_signal(SignalRecord(ticker="OLD", signal_type="BUY", entry_price=10.0))

        conn = sqlite3.connect(str(temp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT price_after_1d, price_after_2d, price_after_3d, "
            "return_1d_pct, return_2d_pct, return_3d_pct FROM forward_signals WHERE id=?",
            (sid,),
        ).fetchone()
        conn.close()
        assert all(row[c] is None for c in row.keys())


# ── _sentiment_direction ─────────────────────────────────────────────────────

class TestSentimentDirection:
    def test_no_news_is_no_news(self):
        from src.forward_signals import _sentiment_direction
        assert _sentiment_direction(None) == "no_news"

    def test_news_without_sentiment_is_no_news(self):
        """An article was found but has no ticker-specific sentiment (rare
        edge case in fetch_recent_news_catalyst) -- zero directional
        information, folds into the same control bucket."""
        from src.forward_signals import _sentiment_direction
        assert _sentiment_direction({"title": "x", "sentiment": None}) == "no_news"

    def test_positive_sentiment_lowercased(self):
        from src.forward_signals import _sentiment_direction
        assert _sentiment_direction({"sentiment": "Positive"}) == "positive"

    def test_negative_sentiment(self):
        from src.forward_signals import _sentiment_direction
        assert _sentiment_direction({"sentiment": "negative"}) == "negative"


# ── record_watch_signals: dedup ──────────────────────────────────────────────

class TestRecordWatchSignalsDedup:
    def test_first_hit_is_recorded(self, temp_db):
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "FOO", "price": 12.5, "score": 80}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=_news("positive")):
            stats = record_watch_signals(hits, "momentum")
        assert stats == {"checked": 1, "recorded": 1, "deduped": 0, "news_found": 1}

    def test_same_direction_repeat_is_deduped_not_reinserted(self, temp_db):
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "FOO", "price": 12.5, "score": 80}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=_news("positive")):
            record_watch_signals(hits, "momentum")
            stats2 = record_watch_signals(hits, "momentum")
        assert stats2 == {"checked": 1, "recorded": 0, "deduped": 1, "news_found": 1}

        import sqlite3
        conn = sqlite3.connect(str(temp_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM forward_signals WHERE ticker='FOO' AND signal_type='WATCH'"
        ).fetchone()[0]
        conn.close()
        assert n == 1, "same-direction repeat must not create a second row"

    def test_direction_change_creates_a_new_row(self, temp_db):
        """positive -> negative is a genuinely new observation, not noise."""
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "FOO", "price": 12.5, "score": 80}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=_news("positive")):
            record_watch_signals(hits, "momentum")
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=_news("negative")):
            stats2 = record_watch_signals(hits, "momentum")
        assert stats2["recorded"] == 1
        assert stats2["deduped"] == 0

        import sqlite3
        conn = sqlite3.connect(str(temp_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM forward_signals WHERE ticker='FOO' AND signal_type='WATCH'"
        ).fetchone()[0]
        conn.close()
        assert n == 2

    def test_news_appearing_after_no_news_is_a_new_observation(self, temp_db):
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "FOO", "price": 12.5, "score": 80}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=None):
            stats1 = record_watch_signals(hits, "momentum")
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=_news("positive")):
            stats2 = record_watch_signals(hits, "momentum")
        assert stats1["recorded"] == 1
        assert stats2["recorded"] == 1
        assert stats2["deduped"] == 0

    def test_repeated_no_news_state_is_deduped(self, temp_db):
        """The no-news control group must also be dedup'd on repeat, exactly
        like a real sentiment direction -- the control state is not noise to
        skip logging, but it is also not exempt from the same dedup rule."""
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "FOO", "price": 12.5, "score": 80}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=None):
            record_watch_signals(hits, "momentum")
            stats2 = record_watch_signals(hits, "momentum")
        assert stats2 == {"checked": 1, "recorded": 0, "deduped": 1, "news_found": 0}

    def test_dedup_is_per_ticker_not_global(self, temp_db):
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "FOO", "price": 12.5}, {"ticker": "BAR", "price": 8.0}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=_news("positive")):
            stats = record_watch_signals(hits, "momentum")
        assert stats["recorded"] == 2


# ── record_watch_signals: no-news control group is logged, not skipped ──────

class TestControlGroupLogged:
    def test_no_news_hit_is_recorded_with_no_news_direction(self, temp_db):
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "CTRL", "price": 20.0}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=None):
            stats = record_watch_signals(hits, "supertrend")
        assert stats["recorded"] == 1
        assert stats["news_found"] == 0

        import sqlite3
        conn = sqlite3.connect(str(temp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ai_verdict, catalyst_summary, signal_type, entry_price "
            "FROM forward_signals WHERE ticker='CTRL'"
        ).fetchone()
        conn.close()
        assert row["signal_type"] == "WATCH"
        assert row["ai_verdict"] == "no_news"
        assert row["entry_price"] == 20.0
        assert "no fresh news" in row["catalyst_summary"]


# ── record_watch_signals: robustness ─────────────────────────────────────────

class TestRecordWatchSignalsRobustness:
    def test_empty_hits_returns_zeroed_stats_without_error(self, temp_db):
        from src.forward_signals import record_watch_signals
        assert record_watch_signals([], "momentum") == {
            "checked": 0, "recorded": 0, "deduped": 0, "news_found": 0
        }

    def test_news_lookup_failure_falls_back_to_no_news_not_raise(self, temp_db):
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "FAIL", "price": 5.0}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst",
                   side_effect=RuntimeError("Massive API down")):
            stats = record_watch_signals(hits, "momentum")
        assert stats["recorded"] == 1
        assert stats["news_found"] == 0

    def test_hit_missing_price_is_skipped(self, temp_db):
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "NOPRICE"}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=None):
            stats = record_watch_signals(hits, "momentum")
        assert stats["checked"] == 0
        assert stats["recorded"] == 0

    def test_supertrend_hit_carries_level_as_supertrend_level(self, temp_db):
        """supertrend hits have 'level'/'avg_volume' instead of momentum's
        'score' -- composite_score should be None, not raise a KeyError."""
        from src.forward_signals import record_watch_signals
        hits = [{"ticker": "STCO", "price": 30.0, "level": 27.5, "avg_volume": 1_000_000}]
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=None):
            record_watch_signals(hits, "supertrend")

        import sqlite3
        conn = sqlite3.connect(str(temp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT composite_score, supertrend_level FROM forward_signals WHERE ticker='STCO'"
        ).fetchone()
        conn.close()
        assert row["composite_score"] is None
        assert row["supertrend_level"] == 27.5


# ── downstream call sites must exclude WATCH rows ────────────────────────────

class TestWatchRowsExcludedFromTradingAggregates:
    def _seed(self, temp_db):
        import sqlite3
        conn = sqlite3.connect(str(temp_db))
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price, "
            "return_7d_pct, status) VALUES (?, ?, 'BUY', 100.0, 5.0, 'matured')",
            ("REAL", now),
        )
        conn.commit()
        conn.close()

    def test_weekly_digest_excludes_watch_rows(self, temp_db):
        from src.forward_signals import record_watch_signals, weekly_digest
        self._seed(temp_db)
        # A pile of WATCH rows recorded "now" would otherwise dwarf the 1 real
        # BUY row and pollute avg_return_7d_pct.
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=None):
            record_watch_signals(
                [{"ticker": f"W{i}", "price": 10.0} for i in range(5)], "momentum"
            )
        d = weekly_digest(days=7)
        assert d["by_type"] == {"BUY": 1}
        assert d["total_signals"] == 1

    def test_record_fill_never_targets_a_watch_row(self, temp_db):
        """A WATCH row recorded after a BUY row for the same ticker must not
        steal the fill callback meant for the BUY row."""
        from src.forward_signals import SignalRecord, record_signal, record_fill

        record_signal(SignalRecord(ticker="FILLCO", signal_type="BUY", entry_price=50.0))

        import sqlite3
        # Insert a newer WATCH row directly (simulates the momentum monitor
        # finding the same ticker moments later).
        conn = sqlite3.connect(str(temp_db))
        conn.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price, status) "
            "VALUES (?, ?, 'WATCH', 51.0, 'open')",
            ("FILLCO", (datetime.now() + timedelta(seconds=5)).isoformat()),
        )
        conn.commit()
        conn.close()

        ok = record_fill("FILLCO", 50.25, ibkr_order_id=999)
        assert ok is True

        conn = sqlite3.connect(str(temp_db))
        conn.row_factory = sqlite3.Row
        rows = {r["signal_type"]: r["fill_price"]
                for r in conn.execute("SELECT signal_type, fill_price FROM forward_signals WHERE ticker='FILLCO'")}
        conn.close()
        assert rows["BUY"] == 50.25
        assert rows["WATCH"] is None


class TestStatusCommandExcludesWatchRows:
    def test_last_signal_query_filters_to_buy_sell(self, temp_db):
        """telegram_command_handler's /status 'last signal' line must read the
        actual most recent BUY/SELL row, not a WATCH row recorded after it."""
        import sqlite3
        conn = sqlite3.connect(str(temp_db))
        conn.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price, status) "
            "VALUES ('REALBUY', ?, 'BUY', 10.0, 'open')",
            (datetime.now().isoformat(),),
        )
        conn.execute(
            "INSERT INTO forward_signals (ticker, signal_ts, signal_type, entry_price, status) "
            "VALUES ('WATCHCO', ?, 'WATCH', 10.0, 'open')",
            ((datetime.now() + timedelta(minutes=5)).isoformat(),),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ticker, signal_type, signal_ts FROM forward_signals "
            "WHERE signal_type IN ('BUY', 'SELL') "
            "ORDER BY signal_ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row["ticker"] == "REALBUY"


# ── update_outcomes() backfills the new short horizons ───────────────────────

class TestUpdateOutcomesShortHorizons:
    def test_watch_row_gets_1d_backfilled_once_matured(self, temp_db, monkeypatch):
        from src.forward_signals import record_watch_signals, update_outcomes
        import sqlite3

        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=None):
            record_watch_signals([{"ticker": "BKFL", "price": 40.0}], "momentum")

        # Backdate signal_ts so the 1d horizon has matured, mirroring how a
        # live-verification run would exercise this without waiting a day.
        conn = sqlite3.connect(str(temp_db))
        old_ts = (datetime.now() - timedelta(days=2)).isoformat()
        conn.execute("UPDATE forward_signals SET signal_ts=? WHERE ticker='BKFL'", (old_ts,))
        conn.commit()
        conn.close()

        class _FakeHist:
            empty = False
            def __getitem__(self, key):
                import pandas as pd
                return pd.Series([44.0])

        with patch("src.forward_signals.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = _FakeHist()
            stats = update_outcomes()

        assert stats["filled"] >= 1

        conn = sqlite3.connect(str(temp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT price_after_1d, return_1d_pct FROM forward_signals WHERE ticker='BKFL'"
        ).fetchone()
        conn.close()
        assert row["price_after_1d"] == 44.0
        assert row["return_1d_pct"] == pytest.approx(10.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
