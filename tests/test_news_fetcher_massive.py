"""
Test: fetch_massive_news() (src/news_fetcher.py) and its wiring into
get_ticker_news().

Background (2026-08-26): Massive/Polygon's /v2/reference/news was already
paid-for and used by src/gap_scanner.py::fetch_recent_news_catalyst for the
momentum/supertrend Telegram enrichment path only. This adds it as a general
source for get_ticker_news() (News Impact / Research pages), alongside
Google RSS / yfinance / Finnhub / Alpha Vantage / Marketaux — it carries real
per-ticker sentiment from the source instead of a keyword heuristic.

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_news_fetcher_massive.py -v
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src import news_fetcher as nf


def _article(title="Company announces big news", ticker="AAPL", sentiment="positive",
             published_utc="2026-08-26T10:00:00Z"):
    return {
        "title": title,
        "published_utc": published_utc,
        "article_url": "https://example.com/article",
        "publisher": {"name": "Example Wire"},
        "insights": [{"ticker": ticker, "sentiment": sentiment,
                      "sentiment_reasoning": "because reasons"}],
    }


def _resp(status_code=200, payload=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload or {}
    return r


class TestFetchMassiveNews:
    def test_no_api_key_returns_empty_without_a_request(self, monkeypatch):
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "")
        with patch.object(nf.requests, "get") as mock_get:
            assert nf.fetch_massive_news("AAPL") == []
        assert not mock_get.called

    def test_no_ticker_returns_empty_without_a_request(self, monkeypatch):
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "test-key")
        with patch.object(nf.requests, "get") as mock_get:
            assert nf.fetch_massive_news("") == []
        assert not mock_get.called

    def test_ticker_specific_sentiment_used_when_present(self, monkeypatch):
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "test-key")
        payload = {"results": [_article(ticker="AAPL", sentiment="positive")]}
        with patch.object(nf.requests, "get", return_value=_resp(200, payload)):
            result = nf.fetch_massive_news("AAPL")
        assert len(result) == 1
        assert result[0]["sentiment"] == "positive"
        assert result[0]["confidence"] == "high"
        assert result[0]["origin"] == "massive"
        assert result[0]["source"] == "Example Wire"

    def test_picks_this_tickers_sentiment_not_another_tickers(self, monkeypatch):
        """A multi-ticker article can be bullish for one name and neutral for
        another mentioned only in passing -- must read the right insight."""
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "test-key")
        article = _article(ticker="AAPL", sentiment="positive")
        article["insights"].append({"ticker": "MSFT", "sentiment": "negative"})
        payload = {"results": [article]}
        with patch.object(nf.requests, "get", return_value=_resp(200, payload)):
            result = nf.fetch_massive_news("MSFT")
        assert result[0]["sentiment"] == "negative"

    def test_falls_back_to_keyword_sentiment_when_no_insight_for_ticker(self, monkeypatch):
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "test-key")
        # Mild headline (no strong keyword hits) isolates that this came from
        # keyword_sentiment's neutral default, not a source-provided label.
        article = _article(title="Company holds annual shareholder meeting",
                            ticker="OTHERTICKER", sentiment="positive")
        payload = {"results": [article]}
        with patch.object(nf.requests, "get", return_value=_resp(200, payload)):
            result = nf.fetch_massive_news("AAPL")
        assert result[0]["sentiment"] == "neutral"
        assert result[0]["confidence"] == "low"

    def test_non_200_status_returns_empty_without_raising(self, monkeypatch):
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "test-key")
        with patch.object(nf.requests, "get", return_value=_resp(429, None)):
            assert nf.fetch_massive_news("AAPL") == []

    def test_request_exception_returns_empty_without_raising(self, monkeypatch):
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "test-key")
        with patch.object(nf.requests, "get", side_effect=RuntimeError("network down")):
            assert nf.fetch_massive_news("AAPL") == []

    def test_no_results_returns_empty(self, monkeypatch):
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "test-key")
        with patch.object(nf.requests, "get", return_value=_resp(200, {"results": []})):
            assert nf.fetch_massive_news("AAPL") == []


class TestGetTickerNewsWiresMassive:
    def test_massive_included_when_key_present(self, monkeypatch):
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "test-key")
        monkeypatch.setattr(nf, "MARKETAUX_KEY", "")
        monkeypatch.setattr(nf, "AV_KEY", "")
        payload = {"results": [_article(title="Massive-sourced headline", ticker="AAPL")]}
        with patch.object(nf, "fetch_google_news_rss", return_value=[]), \
             patch.object(nf, "fetch_yfinance_news", return_value=[]), \
             patch.object(nf, "fetch_finnhub_news", return_value=[]), \
             patch.object(nf.requests, "get", return_value=_resp(200, payload)):
            result = nf.get_ticker_news("AAPL", days=7)
        assert any(a["origin"] == "massive" for a in result)

    def test_massive_skipped_without_key(self, monkeypatch):
        monkeypatch.setattr(nf, "MASSIVE_API_KEY", "")
        monkeypatch.setattr(nf, "MARKETAUX_KEY", "")
        monkeypatch.setattr(nf, "AV_KEY", "")
        with patch.object(nf, "fetch_google_news_rss", return_value=[]), \
             patch.object(nf, "fetch_yfinance_news", return_value=[]), \
             patch.object(nf, "fetch_finnhub_news", return_value=[]), \
             patch.object(nf.requests, "get") as mock_get:
            result = nf.get_ticker_news("AAPL", days=7)
        assert not mock_get.called
        assert not any(a["origin"] == "massive" for a in result)
