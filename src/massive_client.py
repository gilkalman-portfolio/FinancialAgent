"""Thin Massive/Polygon REST helpers used as a FALLBACK behind yfinance.

yfinance is free but rate-limits ("Too Many Requests") under scan load; the
paid Massive plan (MASSIVE_API_KEY, already used by gap_scanner/news_fetcher/
insider+8-K clients) has no such per-call quota. This module only covers what
Massive can answer faithfully:

  * get_overview(ticker)      -> market cap / shares / name   (NOT short
    interest, float, or a live price — callers must treat those as missing)
  * get_daily_history(ticker) -> adjusted daily OHLCV DataFrame shaped like
    yfinance's `.history()` (Open/High/Low/Close/Volume, NY-tz date index)

Every function returns None / empty on any failure — never raises, never
fabricates a value.
"""
import os
import threading
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import requests
from loguru import logger

_BASE_URL = os.getenv("MASSIVE_BASE_URL", "https://api.polygon.io")
_local = threading.local()

# yfinance `period` strings -> calendar days to look back
_PERIOD_DAYS = {"5d": 7, "1mo": 31, "3mo": 93, "6mo": 186, "1y": 366, "2y": 731, "5y": 1827}


def _api_key() -> str:
    return os.getenv("MASSIVE_API_KEY", "")


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        _local.session = s
    return s


def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    key = _api_key()
    if not key:
        return None
    q = dict(params or {})
    q["apiKey"] = key
    try:
        r = _session().get(f"{_BASE_URL}{path}", params=q, timeout=8)
        if r.status_code != 200:
            logger.debug(f"[massive] {path} -> HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        logger.debug(f"[massive] {path} failed: {e}")
        return None


def get_overview(ticker: str) -> Optional[dict]:
    """yfinance-`info`-shaped subset: marketCap, sharesOutstanding, longName,
    shortName, plus `_source='massive'`. None if unavailable."""
    data = _get(f"/v3/reference/tickers/{ticker.upper()}")
    res = (data or {}).get("results")
    if not res:
        return None
    out = {"_source": "massive"}
    if res.get("market_cap"):
        out["marketCap"] = float(res["market_cap"])
    shares = res.get("weighted_shares_outstanding") or res.get("share_class_shares_outstanding")
    if shares:
        out["sharesOutstanding"] = float(shares)
    if res.get("name"):
        out["longName"] = out["shortName"] = res["name"]
    return out if len(out) > 1 else None


def get_daily_history(ticker: str, period: str = "1y") -> pd.DataFrame:
    """Adjusted daily bars, columns Open/High/Low/Close/Volume. Empty frame if
    the period is unsupported or the request fails."""
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return pd.DataFrame()
    end = date.today()
    start = end - timedelta(days=days)
    data = _get(
        f"/v2/aggs/ticker/{ticker.upper()}/range/1/day/{start}/{end}",
        {"adjusted": "true", "sort": "asc", "limit": 50000},
    )
    rows = (data or {}).get("results") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    idx = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York").dt.normalize()
    out = pd.DataFrame(
        {"Open": df["o"], "High": df["h"], "Low": df["l"], "Close": df["c"], "Volume": df["v"]}
    )
    out.index = pd.DatetimeIndex(idx, name="Date")
    return out


def _bars_for(ticker: str, period: str) -> Optional[pd.DataFrame]:
    """Daily bars with a tz-NAIVE date index (matches yf.download's shape).
    One retry on HTTP 429/transport failure; None if nothing usable."""
    for _ in range(2):
        df = get_daily_history(ticker, period)
        if not df.empty:
            df = df.copy()
            df.index = df.index.tz_localize(None)
            return df
    return None


def download_daily(tickers: list, period: str = "1y", max_workers: int = 10) -> pd.DataFrame:
    """Massive-backed equivalent of `yf.download(tickers, period=..., auto_adjust=True)`.

    Returns a DataFrame with (field, ticker) MultiIndex columns — ALWAYS
    MultiIndex, even for one ticker — covering only the tickers Massive
    actually answered for. Tickers it could not serve are simply absent
    (caller decides the fallback; see src/daily_download.py). Empty frame if
    the key is missing, period is unsupported, or nothing came back.
    """
    from concurrent.futures import ThreadPoolExecutor

    if not tickers or not _api_key() or period not in _PERIOD_DAYS:
        return pd.DataFrame()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        frames = list(pool.map(lambda t: _bars_for(t, period), tickers))
    got = {t: f for t, f in zip(tickers, frames) if f is not None}
    if not got:
        return pd.DataFrame()
    fields = ["Open", "High", "Low", "Close", "Volume"]
    wide = {fld: pd.DataFrame({t: f[fld] for t, f in got.items()}) for fld in fields}
    return pd.concat(wide, axis=1).sort_index()
