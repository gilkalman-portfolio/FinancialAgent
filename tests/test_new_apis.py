"""
Tests for Alpha Vantage and Massive.com (NEWS_API_KEY) clients.
Run: pytest tests/test_new_apis.py -v
"""
import pytest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.alpha_vantage_client import get_price_fallback, get_overview_fallback
from src.news_api_client import get_stock_news, get_news_score

TEST_TICKER = 'AAPL'


# ── Alpha Vantage ──────────────────────────────────────────────────────────────

class TestAlphaVantage:

    def test_api_key_loaded(self):
        from dotenv import load_dotenv
        load_dotenv()
        key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
        assert key != '', "ALPHA_VANTAGE_API_KEY not found in .env"

    def test_get_price_fallback_returns_dict(self):
        result = get_price_fallback(TEST_TICKER)
        assert result is not None, "get_price_fallback returned None — quota may be exhausted (25/day)"
        assert isinstance(result, dict)

    def test_get_price_fallback_has_required_fields(self):
        result = get_price_fallback(TEST_TICKER)
        if result is None:
            pytest.skip("Alpha Vantage rate limit hit (25 req/day)")
        for field in ['price', 'volume', 'change_pct', 'source']:
            assert field in result, f"Missing field: {field}"
        assert result['source'] == 'alpha_vantage'

    def test_get_price_fallback_price_positive(self):
        result = get_price_fallback(TEST_TICKER)
        if result is None:
            pytest.skip("Alpha Vantage rate limit hit")
        assert result['price'] > 0, "Price should be > 0"

    def test_get_overview_fallback_returns_dict(self):
        result = get_overview_fallback(TEST_TICKER)
        if result is None:
            pytest.skip("Alpha Vantage rate limit hit or no data")
        assert isinstance(result, dict)
        assert result.get('source') == 'alpha_vantage'

    def test_get_price_fallback_invalid_ticker(self):
        result = get_price_fallback('INVALIDTICKER999')
        # Should return None or empty dict, not raise
        assert result is None or isinstance(result, dict)


# ── Massive.com News API ───────────────────────────────────────────────────────

class TestMassiveNewsAPI:

    def test_api_key_loaded(self):
        from dotenv import load_dotenv
        load_dotenv()
        key = os.getenv('NEWS_API_KEY', '')
        assert key != '', "NEWS_API_KEY not found in .env"

    def test_get_stock_news_returns_list(self):
        result = get_stock_news(TEST_TICKER, days=7)
        assert isinstance(result, list)

    def test_get_stock_news_article_structure(self):
        result = get_stock_news(TEST_TICKER, days=7)
        if not result:
            pytest.skip("No articles returned — check API key or quota")
        article = result[0]
        for field in ['headline', 'source', 'url', 'published_at', 'sentiment']:
            assert field in article, f"Missing field: {field}"

    def test_get_stock_news_sentiment_values(self):
        result = get_stock_news(TEST_TICKER, days=7)
        for article in result:
            assert article['sentiment'] in ('bullish', 'bearish', 'neutral')

    def test_get_news_score_structure(self):
        result = get_news_score(TEST_TICKER, days=7)
        assert isinstance(result, dict)
        for field in ['score', 'count', 'sentiment', 'source']:
            assert field in result, f"Missing field: {field}"

    def test_get_news_score_range(self):
        result = get_news_score(TEST_TICKER, days=7)
        assert 0 <= result['score'] <= 5, f"Score out of range: {result['score']}"

    def test_get_news_score_source(self):
        result = get_news_score(TEST_TICKER, days=7)
        assert result['source'] == 'massive_news'

    def test_get_news_score_empty_ticker(self):
        result = get_news_score('INVALIDTICKER999XYZ', days=7)
        assert result['score'] == 0
        assert result['count'] == 0


# ── Integration: scorer uses fallback ─────────────────────────────────────────

class TestScorerIntegration:

    def test_alpha_vantage_imported_in_scorer(self):
        from src import stock_scorer
        import inspect
        source = inspect.getsource(stock_scorer)
        assert 'get_price_fallback' in source

    def test_newsapi_imported_in_scorer(self):
        from src import stock_scorer
        import inspect
        source = inspect.getsource(stock_scorer)
        assert 'get_newsapi_score' in source
