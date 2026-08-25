"""
Test: fetch_recent_news_catalyst() (src/gap_scanner.py) and its wiring into
auto_watchlist_agent.py's momentum/supertrend Telegram summaries.

Background (2026-08-25): the full-universe technical scanners (momentum_scanner,
supertrend) run every 30 min across the whole scan universe but a hit carries
no "why" -- a user flagged that a raw gap/flip is retrospective (you already
missed the move) and asked whether Massive/Polygon's already-paid-for
`/v2/reference/news` endpoint (second-precision published_utc + per-ticker
sentiment, unlike the existing SEC 8-K enrichment which is date-only) could
close that gap without adding cost (Benzinga's real-time feed was evaluated
and rejected -- 403 on the current plan tier, would require a paid upgrade).

Wired into auto_watchlist_agent.py::run() for momentum/supertrend only
(squeeze/catalyst already carry their own context) and only for the final,
already-filtered `added` list -- never the raw scan results -- to keep the
extra API calls cheap. See CLAUDE.md Incident Archive, 2026-08-25.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_news_catalyst_enrichment.py -v
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


# ── fetch_recent_news_catalyst (src/gap_scanner.py) ─────────────────────────

def _article(published_utc: str, ticker: str = "FOO", sentiment: str = "positive",
             title: str = "Company announces big news"):
    return {
        "title": title,
        "published_utc": published_utc,
        "article_url": "https://example.com/article",
        "publisher": {"name": "Example Wire"},
        "insights": [{"ticker": ticker, "sentiment": sentiment,
                      "sentiment_reasoning": "because reasons"}],
    }


class TestFetchRecentNewsCatalyst:
    def test_no_api_key_returns_none_without_a_request(self, monkeypatch):
        from src import gap_scanner
        monkeypatch.setattr(gap_scanner, "MASSIVE_API_KEY", "")
        with patch.object(gap_scanner, "_massive_get") as mock_get:
            assert gap_scanner.fetch_recent_news_catalyst("FOO") is None
        assert not mock_get.called

    def test_fresh_article_returns_ticker_specific_sentiment(self, monkeypatch):
        from src import gap_scanner
        monkeypatch.setattr(gap_scanner, "MASSIVE_API_KEY", "test-key")
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {"results": [_article(recent, ticker="FOO", sentiment="positive")]}
        with patch.object(gap_scanner, "_massive_get", return_value=(200, payload)):
            result = gap_scanner.fetch_recent_news_catalyst("FOO")

        assert result is not None
        assert result["title"] == "Company announces big news"
        assert result["sentiment"] == "positive"

    def test_picks_this_tickers_sentiment_not_another_tickers(self, monkeypatch):
        """A multi-ticker article can be bullish for one name and neutral for
        another mentioned only in passing -- must read the right insight."""
        from src import gap_scanner
        monkeypatch.setattr(gap_scanner, "MASSIVE_API_KEY", "test-key")
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        article = _article(recent, ticker="FOO", sentiment="positive")
        article["insights"].append({"ticker": "BAR", "sentiment": "negative"})
        with patch.object(gap_scanner, "_massive_get", return_value=(200, {"results": [article]})):
            result = gap_scanner.fetch_recent_news_catalyst("BAR")
        assert result["sentiment"] == "negative"

    def test_stale_article_outside_max_age_is_ignored(self, monkeypatch):
        from src import gap_scanner
        monkeypatch.setattr(gap_scanner, "MASSIVE_API_KEY", "test-key")
        stale = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {"results": [_article(stale)]}
        with patch.object(gap_scanner, "_massive_get", return_value=(200, payload)):
            assert gap_scanner.fetch_recent_news_catalyst("FOO", max_age_hours=24.0) is None

    def test_no_results_returns_none(self, monkeypatch):
        from src import gap_scanner
        monkeypatch.setattr(gap_scanner, "MASSIVE_API_KEY", "test-key")
        with patch.object(gap_scanner, "_massive_get", return_value=(200, {"results": []})):
            assert gap_scanner.fetch_recent_news_catalyst("FOO") is None

    def test_non_200_status_returns_none_without_raising(self, monkeypatch):
        from src import gap_scanner
        monkeypatch.setattr(gap_scanner, "MASSIVE_API_KEY", "test-key")
        with patch.object(gap_scanner, "_massive_get", return_value=(429, None)):
            assert gap_scanner.fetch_recent_news_catalyst("FOO") is None

    def test_malformed_timestamp_is_skipped_not_raised(self, monkeypatch):
        from src import gap_scanner
        monkeypatch.setattr(gap_scanner, "MASSIVE_API_KEY", "test-key")
        bad = _article("not-a-real-timestamp")
        with patch.object(gap_scanner, "_massive_get", return_value=(200, {"results": [bad]})):
            assert gap_scanner.fetch_recent_news_catalyst("FOO") is None


# ── _build_telegram_line / _news_catalyst_suffix (src/auto_watchlist_agent.py) ─

class TestTelegramLineNewsSuffix:
    def test_no_news_leaves_line_unchanged(self):
        from src.auto_watchlist_agent import _build_telegram_line
        r = {"ticker": "FOO", "score": 80, "roc_20d": 12.0, "vol_ratio": 2.0, "rsi": 60}
        line = _build_telegram_line(r, "momentum", news=None)
        assert "\U0001F4F0" not in line

    def test_news_present_appends_sentiment_and_age(self):
        from src.auto_watchlist_agent import _build_telegram_line
        r = {"ticker": "FOO", "score": 80, "roc_20d": 12.0, "vol_ratio": 2.0, "rsi": 60}
        news = {
            "title": "FOO wins major contract",
            "published_utc": datetime.now(timezone.utc) - timedelta(minutes=30),
            "sentiment": "positive",
        }
        line = _build_telegram_line(r, "momentum", news=news)
        assert "FOO wins major contract" in line
        assert "[positive]" in line
        assert "30m ago" in line

    def test_news_without_sentiment_omits_the_bracket_tag(self):
        from src.auto_watchlist_agent import _build_telegram_line
        r = {"ticker": "FOO", "price": 50.0, "level": 46.0}
        news = {
            "title": "FOO mentioned in roundup",
            "published_utc": datetime.now(timezone.utc) - timedelta(minutes=5),
            "sentiment": None,
        }
        line = _build_telegram_line(r, "supertrend", news=news)
        assert "FOO mentioned in roundup" in line
        assert "[" not in line.split("\U0001F4F0")[1].split("(")[0]


# ── End-to-end via auto_watchlist_agent.run() ───────────────────────────────

@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="news_enrich_")
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


class _StubTg:
    def __init__(self):
        self.sent = None

    def send_message(self, msg):
        self.sent = msg
        return True


def _base_cfg(source: str) -> dict:
    return {
        "telegram": True,
        "auto_watchlist": {
            "enabled": True,
            "sources": {source: {"enabled": True, "price_floor": 5.0}},
            "deduplication": {"cooldown_minutes": 1440},
            "watchlist_policy": {"max_items_per_source": 10, "max_items_total": 30,
                                  "require_liquidity_check": False},
        },
    }


class TestRunWiresEnrichment:
    def test_momentum_add_calls_enrichment_and_includes_it_in_telegram(self, temp_db, monkeypatch):
        from src.auto_watchlist_agent import run as aw_run

        stub = _StubTg()
        monkeypatch.setattr("src.auto_watchlist_agent.TelegramNotifier", lambda: stub)

        fake_news = {
            "title": "MOMCO beats earnings",
            "published_utc": datetime.now(timezone.utc) - timedelta(minutes=10),
            "sentiment": "positive",
        }
        with patch("src.gap_scanner.fetch_recent_news_catalyst", return_value=fake_news) as mock_fetch:
            results = [{"ticker": "MOMCO", "score": 80, "roc_20d": 15.0, "vol_ratio": 3.0, "rsi": 65}]
            added = aw_run(results, "momentum", _base_cfg("momentum"))

        assert [r["ticker"] for r in added] == ["MOMCO"]
        mock_fetch.assert_called_once_with("MOMCO")
        assert "MOMCO beats earnings" in stub.sent
        assert "[positive]" in stub.sent

    def test_squeeze_add_never_calls_enrichment(self, temp_db, monkeypatch):
        """Cost control: only momentum/supertrend get the extra API call."""
        from src.auto_watchlist_agent import run as aw_run

        stub = _StubTg()
        monkeypatch.setattr("src.auto_watchlist_agent.TelegramNotifier", lambda: stub)

        with patch("src.gap_scanner.fetch_recent_news_catalyst") as mock_fetch:
            results = [{"ticker": "SQZCO", "score": 75, "si_pct": 25.0, "dtc": 8.0, "rvol": 4.0}]
            added = aw_run(results, "squeeze", _base_cfg("squeeze"))

        assert [r["ticker"] for r in added] == ["SQZCO"]
        assert not mock_fetch.called

    def test_enrichment_failure_does_not_block_add_or_telegram(self, temp_db, monkeypatch):
        """A news-lookup exception must be swallowed -- the watchlist add and
        the Telegram send are independently valid regardless of enrichment."""
        from src.auto_watchlist_agent import run as aw_run
        from src.database import watchlist_get_all

        stub = _StubTg()
        monkeypatch.setattr("src.auto_watchlist_agent.TelegramNotifier", lambda: stub)

        with patch("src.gap_scanner.fetch_recent_news_catalyst",
                   side_effect=RuntimeError("Massive API down")):
            results = [{"ticker": "FAILCO", "score": 80, "roc_20d": 10.0, "vol_ratio": 2.0, "rsi": 55}]
            added = aw_run(results, "momentum", _base_cfg("momentum"))

        assert [r["ticker"] for r in added] == ["FAILCO"]
        assert any(w["ticker"] == "FAILCO" for w in watchlist_get_all())
        assert "FAILCO" in stub.sent
