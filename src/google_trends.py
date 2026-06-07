"""
Google Trends - Search interest scoring (no API key required)
Uses pytrends to measure search velocity for a ticker.
Returns score 0-10 based on recent interest vs 7-day baseline.
"""

import time
import threading
from typing import Optional
from loguru import logger

_pytrends = None
_lock = threading.Lock()

_trends_cache: dict = {}
_TRENDS_TTL = 3600  # 1 hour


def _get_pytrends():
    global _pytrends
    if _pytrends is not None:
        return _pytrends
    try:
        from pytrends.request import TrendReq
        _pytrends = TrendReq(hl='en-US', tz=360, timeout=(10, 25))
        logger.info("Google Trends initialized")
    except Exception as e:
        logger.warning(f"Google Trends init failed: {e}")
        _pytrends = None
    return _pytrends


def trends_score(ticker: str) -> dict:
    """
    Returns Google Trends interest score 0-10 and raw interest value.
    - interest: 0-100 (Google's scale, relative to peak in timeframe)
    - spike: True if latest interest > 2x average
    """
    cached = _trends_cache.get(ticker)
    if cached and (time.time() - cached["_ts"]) < _TRENDS_TTL:
        return cached

    pt = _get_pytrends()
    if pt is None:
        return {'interest': 0, 'avg_interest': 0, 'spike': False, 'trends_score': 0}

    try:
        with _lock:
            pt.build_payload([ticker], timeframe='now 7-d', geo='US')
            time.sleep(0.5)
            df = pt.interest_over_time()

        if df.empty or ticker not in df.columns:
            return {'interest': 0, 'avg_interest': 0, 'spike': False, 'trends_score': 0}

        latest = int(df[ticker].iloc[-1])
        avg    = float(df[ticker].mean())
        spike  = (avg > 0) and (latest >= avg * 2)

        # score 0-10
        if latest >= 80 or spike:
            score = 10
        elif latest >= 60:
            score = 7
        elif latest >= 40:
            score = 5
        elif latest >= 20:
            score = 3
        elif latest >= 5:
            score = 1
        else:
            score = 0

        result = {
            'interest':     latest,
            'avg_interest': round(avg, 1),
            'spike':        spike,
            'trends_score': score,
        }
        result["_ts"] = time.time()
        _trends_cache[ticker] = result
        return result

    except Exception as e:
        logger.debug(f"Google Trends failed for {ticker}: {e}")
        return {'interest': 0, 'avg_interest': 0, 'spike': False, 'trends_score': 0}
