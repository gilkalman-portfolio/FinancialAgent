"""
Score Cache - שומר ציונים ב-memory ל-1 שעה
מונע חישוב כפול כשמריצים כמה סריקות ביום
"""

import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

CACHE_TTL = timedelta(hours=1)
_cache: Dict[str, tuple] = {}   # {ticker: (result, timestamp)}
_lock = threading.Lock()


def _key(ticker: str, forecast_days: int = 30) -> str:
    return f"{ticker}:{forecast_days}"


def get(ticker: str, forecast_days: int = 30) -> Optional[Dict[str, Any]]:
    k = _key(ticker, forecast_days)
    with _lock:
        entry = _cache.get(k)
        if not entry:
            return None
        result, ts = entry
        if datetime.now() - ts > CACHE_TTL:
            del _cache[k]
            return None
        return result


def put(ticker: str, result: Dict[str, Any], forecast_days: int = 30):
    with _lock:
        _cache[_key(ticker, forecast_days)] = (result, datetime.now())


def clear():
    with _lock:
        _cache.clear()


def stats() -> dict:
    with _lock:
        return {"cached": len(_cache), "ttl_minutes": int(CACHE_TTL.total_seconds() / 60)}
