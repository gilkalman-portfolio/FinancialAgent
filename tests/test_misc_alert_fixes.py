"""
Tests for the QA audit fixes in this pass (2026-07-03):
  1. scheduler.run_catalyst_alert — cooldown rows written only AFTER a
     successful Telegram send (mocked failure -> no watchlist_alerts rows)
  2. src.market_feed.get_market_mood — neutral-heavy day is not mislabeled Bearish
  3. src.news_catalyst_monitor — freshness gate rejects ts=0 / missing timestamp
  4. src.news_catalyst_monitor._build_telegram_message — impact['reason'] and
     "also affected" tickers are Markdown-escaped
  5. scheduler._escape_md — local helper behaves like the canonical one
  6. src.alert_monitor.check_dead_threads — market-day-aware window avoids
     Monday false positives for market-dependent thread types
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ══════════════════════════════════════════════════════════════════════════════
# Shared tmp-SQLite fixture (pattern lifted from tests/test_order_funnel.py)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def _in_memory_db(monkeypatch, tmp_path):
    """Redirect get_connection() to a tmp file-backed DB and create the
    watchlist_alerts table used by watchlist_save_alert / _alert_sent_recently."""
    db_path = tmp_path / "test.db"

    def _get_conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    monkeypatch.setattr("src.database.get_connection", _get_conn)

    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watchlist_alerts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker     TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            message    TEXT,
            sent_at    TEXT NOT NULL,
            score      REAL,
            price      REAL
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _count_alerts(db_path, alert_type="catalyst_si_alert"):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    n = conn.execute(
        "SELECT COUNT(*) c FROM watchlist_alerts WHERE alert_type = ?", (alert_type,)
    ).fetchone()["c"]
    conn.close()
    return n


# ══════════════════════════════════════════════════════════════════════════════
# 1. run_catalyst_alert — cooldown-after-send ordering
# ══════════════════════════════════════════════════════════════════════════════

class TestCatalystAlertCooldownOrdering:
    CANDIDATE = {
        "ticker": "ABCD",
        "si_pct": 15.0,
        "days_to_event": 3,
        "price": 10.0,
        "explosion_score": 80,
        "catalyst_detail": "FDA decision",
        "catalyst": "PDUFA",
        "catalyst_date": "2026-07-10",
    }

    def _patch_common(self, monkeypatch, send_result: bool):
        monkeypatch.setattr("scheduler._is_trading_day", lambda: True)
        monkeypatch.setattr("scheduler.load_config", lambda: {"enabled": True, "telegram": True})
        monkeypatch.setattr(
            "src.catalyst_scanner.scan_catalysts",
            lambda **kwargs: [dict(self.CANDIDATE)],
        )
        monkeypatch.setattr("src.auto_watchlist_agent.run", lambda *a, **k: [])

        mock_notifier = MagicMock()
        mock_notifier.send_message.return_value = send_result
        monkeypatch.setattr("scheduler.TelegramNotifier", lambda: mock_notifier)
        return mock_notifier

    def test_send_failure_writes_no_cooldown_rows(self, monkeypatch, _in_memory_db):
        """If Telegram send fails, no watchlist_alerts cooldown rows should be
        written — otherwise the candidate is silently lost for 24h with no
        alert ever having reached Telegram."""
        import scheduler
        mock_notifier = self._patch_common(monkeypatch, send_result=False)

        scheduler.run_catalyst_alert()

        assert mock_notifier.send_message.called
        assert _count_alerts(_in_memory_db) == 0

    def test_send_success_writes_cooldown_rows(self, monkeypatch, _in_memory_db):
        """On a successful send, the cooldown row for the ticker IS written."""
        import scheduler
        mock_notifier = self._patch_common(monkeypatch, send_result=True)

        scheduler.run_catalyst_alert()

        assert mock_notifier.send_message.called
        assert _count_alerts(_in_memory_db) == 1

    def test_message_escapes_markdown_special_chars(self, monkeypatch, _in_memory_db):
        """catalyst_detail containing Markdown special chars must be escaped in
        the outgoing message so a Telegram parse error can't drop the alert."""
        import scheduler
        candidate = dict(self.CANDIDATE)
        candidate["catalyst_detail"] = "Phase 3 data [readout]* early"
        monkeypatch.setattr("scheduler._is_trading_day", lambda: True)
        monkeypatch.setattr("scheduler.load_config", lambda: {"enabled": True, "telegram": True})
        monkeypatch.setattr("src.catalyst_scanner.scan_catalysts", lambda **kwargs: [candidate])
        monkeypatch.setattr("src.auto_watchlist_agent.run", lambda *a, **k: [])

        mock_notifier = MagicMock()
        mock_notifier.send_message.return_value = True
        monkeypatch.setattr("scheduler.TelegramNotifier", lambda: mock_notifier)

        scheduler.run_catalyst_alert()

        # _escape_md escapes *, _, `, [ (not ]) — matches src/news_catalyst_monitor.py's pattern
        sent_text = mock_notifier.send_message.call_args[0][0]
        assert r"\[readout]\*" in sent_text
        assert "[readout]*" not in sent_text


# ══════════════════════════════════════════════════════════════════════════════
# 2. market_feed.get_market_mood — neutral-heavy day
# ══════════════════════════════════════════════════════════════════════════════

class TestMarketMood:
    def test_neutral_heavy_day_is_not_bearish(self):
        """40% bullish / 10% bearish / 50% neutral should be net-bullish (or at
        worst neutral), never Bearish — old logic labeled Bearish whenever
        bullish share < 45%, ignoring the bearish count entirely."""
        from src.market_feed import get_market_mood
        articles = (
            [{"sentiment": "Bullish"}] * 4
            + [{"sentiment": "Bearish"}] * 1
            + [{"sentiment": "Neutral"}] * 5
        )
        result = get_market_mood(articles)
        assert result["label"] != "Bearish"
        assert result["bullish"] == 4
        assert result["bearish"] == 1
        assert result["neutral"] == 5

    def test_clearly_bearish_day_still_labeled_bearish(self):
        from src.market_feed import get_market_mood
        articles = (
            [{"sentiment": "Bullish"}] * 1
            + [{"sentiment": "Bearish"}] * 7
            + [{"sentiment": "Neutral"}] * 2
        )
        result = get_market_mood(articles)
        assert result["label"] == "Bearish"

    def test_clearly_bullish_day_labeled_bullish(self):
        from src.market_feed import get_market_mood
        articles = [{"sentiment": "Bullish"}] * 8 + [{"sentiment": "Bearish"}] * 1 + [{"sentiment": "Neutral"}] * 1
        result = get_market_mood(articles)
        assert result["label"] == "Bullish"

    def test_empty_articles_returns_neutral_default(self):
        from src.market_feed import get_market_mood
        result = get_market_mood([])
        assert result["label"] == "Neutral"
        assert result["score"] == 50

    def test_return_format_unchanged(self):
        """Keep the same return dict shape (label/bullish/bearish/neutral/score)."""
        from src.market_feed import get_market_mood
        result = get_market_mood([{"sentiment": "Bullish"}])
        assert set(result.keys()) == {"label", "bullish", "bearish", "neutral", "score"}


# ══════════════════════════════════════════════════════════════════════════════
# 3. news_catalyst_monitor — freshness gate rejects ts=0 / missing
# ══════════════════════════════════════════════════════════════════════════════

class TestNewsCatalystFreshnessGate:
    def _base_mocks(self, monkeypatch, articles):
        monkeypatch.setattr(
            "src.news_catalyst_monitor._get_tracked_tickers",
            lambda scope: {"ABCD": {"source": "watchlist", "entry_price": None, "shares": 0, "notes": ""}},
        )
        monkeypatch.setattr("src.news_catalyst_monitor.get_ticker_news", lambda *a, **k: articles)
        monkeypatch.setattr("src.news_catalyst_monitor.news_seen_contains", lambda key: False)
        monkeypatch.setattr("src.news_catalyst_monitor.news_seen_add", lambda *a, **k: None)
        monkeypatch.setattr("src.news_catalyst_monitor.news_seen_cleanup", lambda *a, **k: None)
        monkeypatch.setattr("src.news_catalyst_monitor.catalyst_score", lambda headline: 10)  # always above threshold

    def test_zero_timestamp_article_is_skipped(self, monkeypatch):
        """An article with ts=0 (missing timestamp) must NOT bypass the
        freshness gate — old code's `if article_ts and ...` treated falsy ts
        as automatically fresh."""
        from src.news_catalyst_monitor import run_catalyst_check

        articles = [{"headline": "Undated stale catalyst article", "ts": 0, "url": "", "source": "test"}]
        self._base_mocks(monkeypatch, articles)

        # If the freshness gate is bypassed, run_full_analysis would be invoked.
        with patch("src.news_impact_analyzer.run_full_analysis") as mock_llm:
            sent = run_catalyst_check(force=True)

        assert sent == 0
        mock_llm.assert_not_called()

    def test_fresh_article_with_valid_timestamp_proceeds_to_llm(self, monkeypatch):
        """Sanity check: a genuinely fresh article (ts = now) is NOT skipped
        by the freshness gate itself (it may still be filtered downstream by
        impact relevance, which we short-circuit here)."""
        from src.news_catalyst_monitor import run_catalyst_check
        import time as _time

        articles = [{
            "headline": "Fresh breaking catalyst news",
            "ts": _time.time(),
            "url": "http://example.com",
            "source": "test",
        }]
        self._base_mocks(monkeypatch, articles)

        with patch("src.news_impact_analyzer.run_full_analysis") as mock_llm:
            mock_llm.return_value = {"sentiment": "neutral", "affected": [], "summary": ""}
            run_catalyst_check(force=True)

        mock_llm.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# 4. news_catalyst_monitor — Markdown escaping in the built message
# ══════════════════════════════════════════════════════════════════════════════

class TestNewsCatalystMarkdownEscaping:
    def test_impact_reason_is_escaped(self):
        from src.news_catalyst_monitor import _build_telegram_message

        tracker_info = {"source": "watchlist", "entry_price": None, "shares": 0}
        analysis = {"sentiment": "positive", "summary": "", "affected": []}
        impact = {
            "ticker": "ABCD",
            "impact": "positive",
            "layer": "direct",
            "reason": "Beats on EPS [guidance]* raised_significantly",
            "magnitude": 3,
            "direct": True,
        }

        msg = _build_telegram_message(
            ticker="ABCD", tracker_info=tracker_info, headline="Some headline",
            article_url="", source="test", analysis=analysis, impact=impact,
            current_price=10.0,
        )

        # _escape_md escapes *, _, `, [ (not ]) — matches src/news_catalyst_monitor.py's pattern
        assert r"\[guidance]\*" in msg
        assert r"raised\_significantly" in msg
        # Unescaped originals should not appear
        assert "[guidance]*" not in msg

    def test_other_affected_tickers_are_escaped(self):
        from src.news_catalyst_monitor import _build_telegram_message

        tracker_info = {"source": "watchlist", "entry_price": None, "shares": 0}
        analysis = {
            "sentiment": "positive",
            "summary": "",
            "affected": [
                {"ticker": "ABCD", "impact": "positive", "reason": "direct", "magnitude": 3},
                {"ticker": "EV*IL", "impact": "negative", "reason": "x", "magnitude": 2},
            ],
        }
        impact = {"ticker": "ABCD", "impact": "positive", "layer": "direct", "reason": "", "magnitude": 3}

        msg = _build_telegram_message(
            ticker="ABCD", tracker_info=tracker_info, headline="H",
            article_url="", source="test", analysis=analysis, impact=impact,
            current_price=None,
        )

        assert "EV\\*IL" in msg
        assert "EV*IL" not in msg


# ══════════════════════════════════════════════════════════════════════════════
# 5. scheduler._escape_md — local helper sanity
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerEscapeMd:
    def test_escapes_all_special_chars(self):
        from scheduler import _escape_md
        raw = "a*b_c`d[e"
        escaped = _escape_md(raw)
        assert escaped == "a\\*b\\_c\\`d\\[e"

    def test_empty_string_passthrough(self):
        from scheduler import _escape_md
        assert _escape_md("") == ""

    def test_none_passthrough(self):
        from scheduler import _escape_md
        assert _escape_md(None) is None


# ══════════════════════════════════════════════════════════════════════════════
# 6. alert_monitor.check_dead_threads — market-day-aware window
# ══════════════════════════════════════════════════════════════════════════════

class TestAlertMonitorDeadThreadWindow:
    """Reproduces the Monday false-positive: last alert fired Friday, checked
    Monday morning. A fixed 48h wall-clock window always reports "dead" here
    even though the thread is healthy (weekend = no market activity)."""

    def _make_db(self, tmp_path):
        db_path = tmp_path / "alert_monitor_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE watchlist_alerts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker     TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                message    TEXT,
                sent_at    TEXT NOT NULL,
                score      REAL,
                price      REAL
            );
        """)
        conn.commit()
        return conn

    def test_friday_alert_not_flagged_dead_on_monday(self, tmp_path, monkeypatch):
        from src import alert_monitor

        conn = self._make_db(tmp_path)

        # Simulate "now" = Monday 09:30, last alert fired Friday ~16:00 (65.5h gap
        # in wall-clock time, well past the old fixed 48h cutoff).
        monday_now = datetime(2026, 6, 29, 9, 30)  # a Monday
        friday_fire = datetime(2026, 6, 26, 16, 0)  # the preceding Friday
        assert monday_now.weekday() == 0
        assert friday_fire.weekday() == 4

        for atype in alert_monitor.THREAD_TYPES:
            conn.execute(
                "INSERT INTO watchlist_alerts (ticker, alert_type, message, sent_at) VALUES (?,?,?,?)",
                ("ABCD", atype, "test", friday_fire.isoformat()),
            )
        conn.commit()

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return monday_now

        monkeypatch.setattr(alert_monitor, "datetime", _FixedDateTime)

        dead = alert_monitor.check_dead_threads(conn)
        conn.close()

        assert dead == [], f"expected no dead threads, got {dead}"

    def test_truly_stale_thread_still_flagged(self, tmp_path, monkeypatch):
        """A thread with no alerts in the last N market days (e.g. 5 calendar
        days including a weekend) should still be flagged dead."""
        from src import alert_monitor

        conn = self._make_db(tmp_path)

        now = datetime(2026, 7, 1, 9, 30)  # Wednesday
        stale_fire = datetime(2026, 6, 22, 9, 0)  # more than a week earlier

        for atype in alert_monitor.THREAD_TYPES:
            conn.execute(
                "INSERT INTO watchlist_alerts (ticker, alert_type, message, sent_at) VALUES (?,?,?,?)",
                ("ABCD", atype, "test", stale_fire.isoformat()),
            )
        conn.commit()

        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(alert_monitor, "datetime", _FixedDateTime)

        dead = alert_monitor.check_dead_threads(conn)
        conn.close()

        assert set(dead) == set(alert_monitor.THREAD_TYPES)
