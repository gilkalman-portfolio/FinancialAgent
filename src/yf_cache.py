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

from src import massive_client

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
        if data:
            _set(k, data, ttl)
            logger.debug(f"[yf_cache] info MISS {ticker} → cached {ttl}s")
            return data
        err = "empty info"
    except Exception as e:
        err = str(e)
    # yfinance failed (typically a 429). Fall back to the paid Massive plan —
    # but ONLY for the fields it can answer faithfully (marketCap, shares,
    # name). Deliberately NO currentPrice/short-interest/float: get_price()
    # reads currentPrice from here, so a stale prior-close would feed false
    # price alerts, and absent keys behave exactly as they did on failure.
    alt = massive_client.get_overview(ticker)
    if alt:
        _set(k, alt, min(ttl, 300))  # short TTL: retry yfinance soon
        logger.info(f"[yf_cache] info via Massive fallback {ticker} (yfinance: {err[:60]})")
        return alt
    if err == "empty info":
        _set(k, {}, ttl)  # preserve pre-fallback behavior: empty results were cached
        return {}
    logger.warning(f"[yf_cache] info fetch failed {ticker}: {err}")
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

    import pandas as pd
    err = None
    try:
        data = yf.Ticker(ticker).history(period=period, interval=interval)
        if data is not None and not data.empty:
            _set(k, data, ttl)
            logger.debug(f"[yf_cache] hist MISS {ticker} {period}/{interval} → cached {ttl}s")
            return data
        err = "empty history"
    except Exception as e:
        err = str(e)
    if interval == "1d":
        alt = massive_client.get_daily_history(ticker, period)
        if not alt.empty:
            _set(k, alt, min(ttl, 300))
            logger.info(f"[yf_cache] hist via Massive fallback {ticker} {period} (yfinance: {err[:60]})")
            return alt
    if err == "empty history":
        empty = pd.DataFrame()
        _set(k, empty, ttl)  # preserve pre-fallback behavior: empty results were cached
        return empty
    logger.warning(f"[yf_cache] hist fetch failed {ticker} {period}/{interval}: {err}")
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
