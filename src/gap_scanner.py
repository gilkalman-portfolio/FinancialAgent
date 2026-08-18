"""
Gap Scanner — two independent, informational-only scanners.

Feeds the Premarket Gap Alert and Opening Print Alert Telegram jobs
(scheduler.py::run_premarket_gap_alert / run_opening_print_alert). Neither is
called from auto_watchlist_agent.py, order_manager.py, or ibkr_worker.py —
this module exists to notify, not to trade or auto-add to the watchlist.

Background: a momentum alert for NMAX reached the user ~6h after the move,
because a move that completes in minutes is invisible to the 30-min momentum
monitor. That's why this module has two functions, not one: a real premarket
gap needs premarket *volume* as confirmation (scan_premarket_gaps), while an
NMAX-shaped event needs a post-open comparison of the 09:30 print vs. price a
few minutes later (scan_opening_prints). See CLAUDE.md Incident Archive,
2026-08-15.

scan_premarket_gaps() was rewritten 2026-08-16 to use Massive/Polygon
(MASSIVE_API_KEY, per-ticker REST) instead of yfinance — yfinance's premarket
volume field was found to always report exactly 0 in production (confirmed
across 6 independent real historical cases), meaning the volume-confirmation
filter could never pass and this alert never actually fired. Alpaca's free
tier and EODHD's free tier were also evaluated and both fail this use case
(zero extended-hours bars / blocked entirely). See CLAUDE.md Incident
Archive, 2026-08-16, for the full comparison. scan_opening_prints() is
unchanged — it uses yfinance regular-session data, separately verified
working, so there's no reason to add paid-API cost there.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timezone
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from loguru import logger

_ET = ZoneInfo("America/New_York")

MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
MASSIVE_BASE_URL = os.getenv("MASSIVE_BASE_URL", "https://api.polygon.io")
_DEFAULT_MASSIVE_MAX_WORKERS = 10


def _split_fields(raw: pd.DataFrame, tickers: list, fields: tuple):
    """Normalise yf.download's single- vs multi-ticker column shape.
    Returns one DataFrame per requested field, columns = tickers."""
    if isinstance(raw.columns, pd.MultiIndex):
        return tuple(raw[f] for f in fields)
    t = tickers[0]
    return tuple(raw[[f]].rename(columns={f: t}) for f in fields)


def _download_with_retry(tickers: list, **kwargs) -> pd.DataFrame:
    """One retry with a short backoff. These scanners run once/day, so an
    unretried rate-limit failure loses the entire day's check with no
    next-cycle recovery — unlike the 30-min momentum/supertrend threads,
    which simply get another attempt in 30 minutes."""
    last_err = None
    for attempt in (1, 2):
        try:
            return yf.download(tickers, **kwargs)
        except Exception as e:
            last_err = e
            if attempt == 2:
                raise
            logger.warning(f"gap_scanner download failed (attempt {attempt}): {e} — retrying in 5s")
            time.sleep(5)
    raise last_err  # unreachable, satisfies linters


def _massive_get(url: str, params: dict) -> Tuple[int, Optional[dict]]:
    """Single seam every Massive/Polygon call routes through. Never raises —
    returns (0, None) on any network/timeout/decode failure so callers can
    treat it uniformly with real HTTP failures."""
    try:
        r = requests.get(url, params=params, timeout=8)
        return r.status_code, (r.json() if r.status_code == 200 else None)
    except Exception as e:
        logger.debug(f"Massive API request failed: {url}: {e}")
        return 0, None


def _fetch_prior_close(ticker: str) -> Optional[float]:
    """Prior regular-session close via Massive/Polygon's 'previous day bar'
    endpoint — the standard gap-% reference, not the first premarket print
    (which can already be hours stale relative to the true prior close)."""
    status, data = _massive_get(
        f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{ticker}/prev",
        {"adjusted": "true", "apiKey": MASSIVE_API_KEY},
    )
    if status in (401, 403):
        logger.warning(f"Premarket gap scan: {ticker} prior-close fetch got {status} — check MASSIVE_API_KEY")
        return None
    if status == 429:
        logger.warning(f"Premarket gap scan: {ticker} prior-close fetch rate-limited (429)")
        return None
    if status != 200 or not data:
        return None
    results = data.get("results") or []
    if not results:
        return None
    close = results[0].get("c")
    return float(close) if close else None


def _fetch_premarket_bars(ticker: str, date_str: str) -> Optional[List[dict]]:
    """Today's 5-minute bars, filtered to the genuine 04:00-09:30 ET premarket
    window. Returns None on fetch failure, but [] (distinct!) when the call
    succeeds and there simply were no premarket bars — that's a real "no
    premarket activity" result (e.g. WEST on 2026-07-27), not an error, and
    must not be conflated with one.

    Timezone handling: Massive/Polygon returns each bar's `t` as Unix
    MILLISECONDS in UTC — not ET, not seconds. Every bar must be converted
    via tz=timezone.utc THEN .astimezone(_ET) before any boundary check;
    comparing the raw UTC hour directly against ET boundary constants would
    silently admit overnight bars or drop legitimate premarket bars — the
    exact class of data-correctness bug this whole rewrite exists to fix.
    """
    status, data = _massive_get(
        f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{ticker}/range/5/minute/{date_str}/{date_str}",
        {"adjusted": "true", "sort": "asc", "limit": 500, "apiKey": MASSIVE_API_KEY},
    )
    if status in (401, 403):
        logger.warning(f"Premarket gap scan: {ticker} bars fetch got {status} — check MASSIVE_API_KEY")
        return None
    if status == 429:
        logger.warning(f"Premarket gap scan: {ticker} bars fetch rate-limited (429)")
        return None
    if status != 200 or not data:
        return None

    bars = data.get("results") or []
    premarket_bars = []
    for bar in bars:
        ts_et = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).astimezone(_ET)
        if dtime(4, 0) <= ts_et.time() < dtime(9, 30):
            premarket_bars.append(bar)
    premarket_bars.sort(key=lambda b: b["t"])
    return premarket_bars


def _fetch_one_ticker_premarket(ticker: str, date_str: str) -> Optional[dict]:
    """ThreadPoolExecutor worker unit — mirrors catalyst_scanner.py's
    fetch_sec_8k_events._fetch_one shape: wrapped in its own try/except,
    never raises, never aborts the batch on a single ticker's failure."""
    try:
        prior_close = _fetch_prior_close(ticker)
        if prior_close is None or prior_close <= 0:
            return None

        bars = _fetch_premarket_bars(ticker, date_str)
        if not bars:  # None (fetch failed) or [] (genuinely no premarket activity)
            return None

        latest_price = float(bars[-1]["c"])
        premarket_dollar_volume = sum(b["c"] * b["v"] for b in bars)
        premarket_volume = sum(b["v"] for b in bars)

        return {
            "ticker": ticker,
            "price": latest_price,
            "prior_close": prior_close,
            "premarket_dollar_volume": premarket_dollar_volume,
            "premarket_volume": premarket_volume,
        }
    except Exception as e:
        logger.debug(f"Premarket gap scan {ticker}: {e}")
        return None


def _preflight_check_api_key(sample_ticker: str = "SPY") -> bool:
    """One cheap synchronous call before fanning out ~2x N requests. Returns
    False only on a confirmed 401/403 (bad/revoked key) — fail-open on every
    other outcome (a single ticker's timeout or a transient 5xx shouldn't
    abort the whole day's scan). A bad key fails on ticker #1 exactly as
    reliably as ticker #500, so this is sufficient without a more complex
    mid-scan circuit breaker."""
    status, _ = _massive_get(
        f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{sample_ticker}/prev",
        {"adjusted": "true", "apiKey": MASSIVE_API_KEY},
    )
    if status in (401, 403):
        logger.error(f"Premarket gap scan: MASSIVE_API_KEY preflight failed ({status}) — check .env")
        return False
    return True


def scan_premarket_gaps(
    tickers: list,
    gap_pct_min: float = 8.0,
    min_premarket_dollar_volume: float = 200_000.0,
    massive_max_workers: int = _DEFAULT_MASSIVE_MAX_WORKERS,
) -> List[Dict]:
    """
    Scan for tickers with a real premarket gap: a price move vs. the prior
    regular-session close that is backed by actual premarket volume, via
    Massive/Polygon per-ticker REST calls (MASSIVE_API_KEY).

    The volume requirement is deliberate — it's what correctly excludes a
    quiet, no-print, no-volume premarket tape even when the nominal price
    differs from the prior close.
    """
    if not tickers:
        return []

    if not MASSIVE_API_KEY:
        logger.error(
            "Premarket gap scan: MASSIVE_API_KEY not set — skipping "
            f"(would otherwise attempt {2 * len(tickers)} doomed requests)"
        )
        return []

    if not _preflight_check_api_key():
        return []

    date_str = datetime.now(_ET).strftime("%Y-%m-%d")

    with ThreadPoolExecutor(max_workers=massive_max_workers) as pool:
        fetched = list(pool.map(lambda t: _fetch_one_ticker_premarket(t, date_str), tickers))

    fail_count = sum(1 for r in fetched if r is None)

    results = []
    for r in fetched:
        if r is None:
            continue
        prior_close = r["prior_close"]
        gap_pct = (r["price"] - prior_close) / prior_close * 100
        if gap_pct >= gap_pct_min and r["premarket_dollar_volume"] >= min_premarket_dollar_volume:
            results.append({
                "ticker": r["ticker"],
                "price": round(r["price"], 4),
                "prior_close": round(prior_close, 4),
                "gap_pct": round(gap_pct, 2),
                "premarket_dollar_volume": round(r["premarket_dollar_volume"], 2),
                "premarket_volume": round(r["premarket_volume"], 2),
            })

    results.sort(key=lambda x: x["gap_pct"], reverse=True)
    logger.info(
        f"Premarket gap scan complete: {len(results)}/{len(tickers)} qualify "
        f"(gap>={gap_pct_min}% | $vol>={min_premarket_dollar_volume:,.0f} | "
        f"{fail_count} fetch failures)"
    )
    return results


def scan_opening_prints(
    tickers: list,
    pct_move_min: float = 10.0,
    min_dollar_volume: float = 500_000.0,
) -> List[Dict]:
    """
    Batch-scan comparing each ticker's regular-session open print (09:30 ET)
    to its price a few minutes later, backed by a dollar-volume filter.

    Meant to be called ~5-10 min after the open. This is the check that would
    have caught NMAX on 2026-08-14: it had zero premarket signal but moved
    heavily (245K/69K/67K/57K shares in its first four 1-min bars) within
    minutes of the bell.
    """
    if not tickers:
        return []

    try:
        # Regular session only, no prepost. Cheap by construction — called a
        # few minutes after the open, so each ticker only has a handful of
        # rows at call time.
        raw = _download_with_retry(
            tickers, period="1d", interval="1m",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception as e:
        logger.error(f"Opening print scan: download failed: {e}")
        return []
    if raw is None or raw.empty:
        logger.warning("Opening print scan: empty data returned")
        return []

    opens, closes, volumes = _split_fields(raw, tickers, ("Open", "Close", "Volume"))

    results = []
    for ticker in tickers:
        if ticker not in closes.columns:
            continue
        try:
            c = closes[ticker].dropna()
            if c.empty:
                continue
            o = opens[ticker].dropna() if ticker in opens.columns else pd.Series(dtype=float)
            if o.empty:
                continue

            open_print = float(o.iloc[0])
            if open_print <= 0:
                continue
            latest_close = float(c.iloc[-1])
            move_pct = (latest_close - open_print) / open_print * 100

            v = (volumes[ticker].reindex(c.index).fillna(0.0)
                 if ticker in volumes.columns else pd.Series(0.0, index=c.index))
            dollar_volume = float((c * v).sum())

            if move_pct >= pct_move_min and dollar_volume >= min_dollar_volume:
                results.append({
                    "ticker": ticker,
                    "price": round(latest_close, 4),
                    "open_print": round(open_print, 4),
                    "move_pct": round(move_pct, 2),
                    "dollar_volume": round(dollar_volume, 2),
                    "volume": round(float(v.sum()), 2),
                })
        except Exception as e:
            logger.debug(f"Opening print scan {ticker}: {e}")

    results.sort(key=lambda x: x["move_pct"], reverse=True)
    logger.info(
        f"Opening print scan complete: {len(results)}/{len(tickers)} qualify "
        f"(move>={pct_move_min}% | $vol>={min_dollar_volume:,.0f})"
    )
    return results
