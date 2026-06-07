"""
Lightweight TTL cache for yfinance calls.

Prevents redundant API hits when the same ticker is fetched multiple times
within a short window — especially important for:
  - price_alert_monitor  (every 5 min per ticker)
  - catalyst_scanner     (per-ticker loop, 50-500 tickers)
  - squeeze_scanner      (per-ticker loop)

Usage:
    from src.yf_cache import get_info, get_history

    info = get_info("AAPL")                        # cached 5 min
    hist = get_history("AAPL", period="1y")        # cached 30 min
    hist = get_history("AAPL", period="5d",
                       interval="15m", ttl=300)   # cached 5 min
"""

import time
import threading
import yfinance as yf
from typing import Optional
from loguru import logger

_lock   = threading.Lock()
_store: dict[str, tuple] = {}   # key → (value, expires_at)


def _key(*parts) -> str:
    return "|".join(str(p) for p in parts)


def _get(k: str):
    with _lock:
        entry = _store.get(k)
        if entry and time.time() < entry[1]:
            return entry[0], True
        return None, False


def _set(k: str, value, ttl: int):
    with _lock:
        _store[k] = (value, time.time() + ttl)


def _evict():
    """Remove expired entries — call occasionally to avoid unbounded growth."""
    now = time.time()
    with _lock:
        expired = [k for k, (_, exp) in _store.items() if now >= exp]
        for k in expired:
            del _store[k]


# ── Public API ────────────────────────────────────────────────────────────────

def get_info(ticker: str, ttl: int = 300) -> dict:
    """
    Return yf.Ticker(ticker).info, cached for `ttl` seconds (default 5 min).
    Returns {} on failure.
    """
    k = _key("info", ticker)
    cached, hit = _get(k)
    if hit:
        logger.debug(f"[yf_cache] info HIT  {ticker}")
        return cached

    try:
        data = yf.Ticker(ticker).info or {}
        _set(k, data, ttl)
        logger.debug(f"[yf_cache] info MISS {ticker} → cached {ttl}s")
        return data
    except Exception as e:
        logger.warning(f"[yf_cache] info fetch failed {ticker}: {e}")
        return {}


def get_history(ticker: str, period: str = "1y", interval: str = "1d",
                ttl: int = 1800):
    """
    Return yf.Ticker(ticker).history(period, interval), cached for `ttl` seconds.

    Defaults:
      period="1y", interval="1d"  → ttl=1800 (30 min)
      period="5d", interval="15m" → pass ttl=300 (5 min) explicitly
      period="60d"                → ttl=1800 default
    """
    k = _key("hist", ticker, period, interval)
    cached, hit = _get(k)
    if hit:
        logger.debug(f"[yf_cache] hist HIT  {ticker} {period}/{interval}")
        return cached

    try:
        data = yf.Ticker(ticker).history(period=period, interval=interval)
        _set(k, data, ttl)
        logger.debug(f"[yf_cache] hist MISS {ticker} {period}/{interval} → cached {ttl}s")
        return data
    except Exception as e:
        logger.warning(f"[yf_cache] hist fetch failed {ticker} {period}/{interval}: {e}")
        import pandas as pd
        return pd.DataFrame()


def get_price(ticker: str, ttl: int = 180) -> Optional[float]:
    """
    Return current price, cached for `ttl` seconds (default 3 min).
    Tries currentPrice → regularMarketPrice from info.
    """
    k = _key("price", ticker)
    cached, hit = _get(k)
    if hit:
        return cached

    info = get_info(ticker, ttl=ttl)
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if price and float(price) > 0:
        p = float(price)
        _set(k, p, ttl)
        return p
    return None


def invalidate(ticker: str):
    """Force-expire all cached entries for a ticker (e.g. after a scan completes)."""
    prefix = f"info|{ticker}"
    with _lock:
        keys = [k for k in _store if ticker in k]
        for k in keys:
            del _store[k]
    logger.debug(f"[yf_cache] invalidated all entries for {ticker}")
