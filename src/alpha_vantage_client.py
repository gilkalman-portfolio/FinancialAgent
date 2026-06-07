"""
Alpha Vantage Client - Fallback data source for yfinance failures.
Free tier: 25 requests/day — used ONLY as fallback, not primary.
"""

import os
import requests
import time as _time
import time
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv('ALPHA_VANTAGE_API_KEY', '')
BASE_URL = 'https://www.alphavantage.co/query'

# Rate limiting: 5 req/min on free tier
_last_call = 0
_MIN_INTERVAL = 12.5  # seconds between calls

# Daily quota tracking (free tier: 25 req/day)
_daily_calls = {"date": "", "count": 0}
_MAX_DAILY_CALLS = 23  # leave buffer before hard limit


def _check_quota():
    today = _time.strftime("%Y-%m-%d")
    if _daily_calls["date"] != today:
        _daily_calls["date"] = today
        _daily_calls["count"] = 0
    _daily_calls["count"] += 1
    if _daily_calls["count"] > _MAX_DAILY_CALLS:
        logger.warning(f"[alpha_vantage] daily quota nearly exhausted ({_daily_calls['count']}/{_MAX_DAILY_CALLS+2})")


def _get(params: dict) -> dict:
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = time.time()

    _check_quota()

    if not API_KEY:
        return {}
    try:
        params['apikey'] = API_KEY
        r = requests.get(BASE_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'Note' in data or 'Information' in data:
            logger.warning("Alpha Vantage rate limit hit")
            return {}
        return data
    except Exception as e:
        logger.error(f"Alpha Vantage error: {e}")
        return {}


def get_price_fallback(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Get latest price + basic metrics as fallback when yfinance fails.
    Returns dict with: price, open, high, low, volume, change_pct
    """
    data = _get({'function': 'GLOBAL_QUOTE', 'symbol': ticker})
    quote = data.get('Global Quote', {})
    if not quote:
        return None

    try:
        return {
            'price':      float(quote.get('05. price', 0)),
            'open':       float(quote.get('02. open', 0)),
            'high':       float(quote.get('03. high', 0)),
            'low':        float(quote.get('04. low', 0)),
            'volume':     int(quote.get('06. volume', 0)),
            'change_pct': float(quote.get('10. change percent', '0%').replace('%', '')),
            'prev_close': float(quote.get('08. previous close', 0)),
            'source':     'alpha_vantage',
        }
    except Exception as e:
        logger.debug(f"Alpha Vantage parse error for {ticker}: {e}")
        return None


def get_overview_fallback(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Get company fundamentals as fallback.
    Returns dict with: pe_ratio, eps, market_cap, sector, industry
    """
    data = _get({'function': 'OVERVIEW', 'symbol': ticker})
    if not data or 'Symbol' not in data:
        return None

    try:
        def _f(key): return float(data.get(key) or 0) or None

        return {
            'pe_ratio':    _f('PERatio'),
            'eps':         _f('EPS'),
            'market_cap':  _f('MarketCapitalization'),
            'sector':      data.get('Sector'),
            'industry':    data.get('Industry'),
            'description': data.get('Description', ''),
            'ex_dividend': data.get('ExDividendDate'),
            'earnings_date': data.get('NextEarningsDate'),
            'source':      'alpha_vantage',
        }
    except Exception as e:
        logger.debug(f"Alpha Vantage overview error for {ticker}: {e}")
        return None
