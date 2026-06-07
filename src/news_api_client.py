"""
Massive.com (formerly Polygon.io) News Client
Fetches stock-specific news articles via: GET /v2/reference/news?ticker=X&apiKey=Y
Base URL: https://api.massive.com
"""

import os
import requests
import time
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv('NEWS_API_KEY', '')
BASE_URL = 'https://api.massive.com'

_last_call = 0
_MIN_INTERVAL = 1.0


def _get(endpoint: str, params: dict) -> dict:
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = time.time()

    if not API_KEY:
        logger.warning("NEWS_API_KEY not set")
        return {}
    try:
        params['apiKey'] = API_KEY
        r = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Massive NewsAPI error: {e}")
        return {}


def get_stock_news(ticker: str, company_name: str = '', days: int = 7) -> List[Dict[str, Any]]:
    """
    Fetch news articles for a stock ticker from Massive.com.
    Endpoint: GET /v2/reference/news
    Returns list of dicts: headline, source, url, published_at, sentiment_hint
    """
    from_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    data = _get('v2/reference/news', {
        'ticker':        ticker,
        'published_utc.gte': from_date,
        'order':         'desc',
        'limit':         20,
        'sort':          'published_utc',
    })

    articles = data.get('results', [])
    if not articles:
        return []

    positive_kw = {'surge', 'soar', 'rally', 'gain', 'beat', 'breakout', 'growth', 'profit', 'record', 'upgrade'}
    negative_kw = {'drop', 'fall', 'plunge', 'decline', 'crash', 'miss', 'loss', 'debt', 'risk', 'concern', 'downgrade'}

    results = []
    for a in articles:
        text = ((a.get('title') or '') + ' ' + (a.get('description') or '')).lower()
        pos = sum(1 for w in positive_kw if w in text)
        neg = sum(1 for w in negative_kw if w in text)
        sentiment = 'bullish' if pos > neg else ('bearish' if neg > pos else 'neutral')

        results.append({
            'headline':     a.get('title', ''),
            'source':       a.get('publisher', {}).get('name', '') if isinstance(a.get('publisher'), dict) else '',
            'url':          a.get('article_url', ''),
            'published_at': a.get('published_utc', ''),
            'sentiment':    sentiment,
        })

    logger.info(f"Massive NewsAPI: {len(results)} articles for {ticker}")
    return results


def get_news_score(ticker: str, company_name: str = '', days: int = 7) -> Dict[str, Any]:
    """
    Get a news buzz score (0-5) for use in stock_scorer.
    Compatible with the existing news_score field.
    """
    articles = get_stock_news(ticker, company_name, days)
    count = len(articles)

    if count == 0:
        return {'score': 0, 'count': 0, 'sentiment': 'neutral', 'source': 'massive_news'}

    pos = sum(1 for a in articles if a['sentiment'] == 'bullish')
    neg = sum(1 for a in articles if a['sentiment'] == 'bearish')
    sentiment = 'bullish' if pos > neg else ('bearish' if neg > pos else 'neutral')

    if count >= 20:   score = 5
    elif count >= 10: score = 4
    elif count >= 5:  score = 3
    elif count >= 2:  score = 2
    else:             score = 1

    return {
        'score':     score,
        'count':     count,
        'sentiment': sentiment,
        'articles':  articles[:5],
        'source':    'massive_news',
    }
