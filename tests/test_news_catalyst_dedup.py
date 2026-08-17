"""
Test: news_catalyst_monitor.py's headline dedup no longer permanently
blacklists a real catalyst on a transient failure.

Background (2026-08-17): news_seen_add(key, ...) used to be called for EVERY
headline scoring above catalyst_threshold immediately, before the LLM call
and the Telegram send even ran. Unlike every other cooldown/dedup mechanism
in this codebase (24h), this one has no expiry at all — so a headline that
scored well but then hit an LLM timeout, an LLM error, no significant
impact, an LLM-budget cap, or a failed Telegram send was gone forever, not
just for 24h. The fix defers marking a qualifying headline as "seen" until
the Telegram send is confirmed; sub-threshold headlines are still marked
immediately (that part was never buggy — no reason to ever re-score a
genuinely irrelevant headline).

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_news_catalyst_dedup.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.news_catalyst_monitor as ncm


def _article(headline="BigCo announces blowout earnings beat", ts=None):
    import time
    return {"headline": headline, "ts": ts or time.time(), "url": "http://x", "source": "test"}


@pytest.fixture(autouse=True)
def _stub_tracked_and_seen(monkeypatch):
    """Every test tracks exactly one ticker (FAKE) and starts with a clean
    'seen' set backed by a simple in-memory dict, so tests can assert on it
    directly instead of needing a real DB."""
    monkeypatch.setattr(ncm, "_get_tracked_tickers", lambda scope: {"FAKE": {}})
    seen = {}
    monkeypatch.setattr(ncm, "news_seen_contains", lambda key: key in seen)
    monkeypatch.setattr(ncm, "news_seen_add", lambda key, ticker, score: seen.__setitem__(key, score))
    monkeypatch.setattr(ncm, "watchlist_save_alert", lambda **kw: None)
    monkeypatch.setattr(ncm, "_get_current_price", lambda ticker: 42.0)
    return seen


_DEFAULT_IMPACT = {"impact": "bullish", "layer": 1}


def _run(articles, catalyst_score_value, telegram_sends, impact=_DEFAULT_IMPACT):
    """Run one check cycle with a single tracked ticker and a single article.
    telegram_sends: the return value TelegramNotifier().send_message() gives.
    impact: pass None explicitly to simulate "no significant impact found"."""
    mock_tg = MagicMock()
    mock_tg.send_message.return_value = telegram_sends
    analysis = {"tone": "confident", "guidance": "raised", "beat_miss": "beat", "score": 5}

    with patch.object(ncm, "get_ticker_news", return_value=articles), \
         patch.object(ncm, "catalyst_score", return_value=catalyst_score_value), \
         patch.object(ncm, "TelegramNotifier", return_value=mock_tg), \
         patch("src.news_impact_analyzer.run_full_analysis", return_value=analysis), \
         patch.object(ncm, "_impact_on_ticker", return_value=impact), \
         patch.object(ncm, "_build_telegram_message", return_value="msg"):
        alerts_sent = ncm.run_catalyst_check(catalyst_threshold=3, max_llm_calls=3)
    return alerts_sent, mock_tg


class TestBelowThresholdMarkedImmediately:
    def test_low_score_headline_marked_seen_right_away(self, _stub_tracked_and_seen):
        seen = _stub_tracked_and_seen
        articles = [_article()]
        _run(articles, catalyst_score_value=1, telegram_sends=True)  # below threshold=3
        key = ncm._headline_key(articles[0]["headline"], "FAKE")
        assert key in seen, "a genuinely low-signal headline should be marked seen immediately"


class TestAboveThresholdOnlyMarkedOnConfirmedSend:
    def test_successful_send_marks_seen(self, _stub_tracked_and_seen):
        seen = _stub_tracked_and_seen
        articles = [_article()]
        alerts_sent, mock_tg = _run(articles, catalyst_score_value=5, telegram_sends=True)

        key = ncm._headline_key(articles[0]["headline"], "FAKE")
        assert alerts_sent == 1
        assert key in seen, "expected the headline to be marked seen after a confirmed send"

    def test_failed_send_does_not_mark_seen(self, _stub_tracked_and_seen):
        """The core fix: a qualifying headline whose Telegram send fails must
        remain retry-able next cycle, not permanently blacklisted."""
        seen = _stub_tracked_and_seen
        articles = [_article()]
        alerts_sent, mock_tg = _run(articles, catalyst_score_value=5, telegram_sends=False)

        key = ncm._headline_key(articles[0]["headline"], "FAKE")
        assert alerts_sent == 0
        assert key not in seen, "expected the headline to remain unmarked — Telegram send failed"
        assert not ncm.news_seen_contains(key)

    def test_no_significant_impact_does_not_mark_seen(self, _stub_tracked_and_seen):
        """A qualifying headline that turns out to have no real impact on the
        tracked ticker must also stay retry-able — the LLM's read on it could
        differ next cycle with fresher context."""
        seen = _stub_tracked_and_seen
        articles = [_article()]
        alerts_sent, mock_tg = _run(articles, catalyst_score_value=5, telegram_sends=True, impact=None)

        key = ncm._headline_key(articles[0]["headline"], "FAKE")
        assert alerts_sent == 0
        assert key not in seen


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
