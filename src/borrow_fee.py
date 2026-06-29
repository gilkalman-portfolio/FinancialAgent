"""
Borrow Fee Scraper
Sources tried in order:
  1. Finviz quote page — scrapes "Short Float" and "Short Ratio" (always available, no JS)
  2. Stockanalysis.com — secondary fallback
  3. None — caller applies -20pt penalty

Note: Finviz doesn't expose annualised borrow rate directly.
We approximate it from Short Float % using a calibrated scale:
  SI% of Float → Estimated Borrow Rate
  < 5%  → ~1%
  5-10% → ~3%
  10-20%→ ~8%
  20-30%→ ~20%
  30-50%→ ~40%
  50-75%→ ~80%
  >75%  → ~150%+

This is a recognised industry approximation: stocks with high SI% have high
demand to borrow, which drives up the borrow rate. Not perfect, but directionally
correct and avoids showing N/A on everything.

Cache: 2 hours per ticker.
"""
import re
import threading
import time
import requests
from typing import Optional
from loguru import logger

_cache: dict[str, tuple[Optional[float], float]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL       = 2 * 3600   # successful result
_CACHE_TTL_ERROR = 5 * 60     # failed fetch (rate-limited/blocked) — retry sooner

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

# SI% → approximate borrow rate mapping
_SI_TO_BORROW = [
    (75, 150.0),
    (50,  80.0),
    (30,  40.0),
    (20,  20.0),
    (10,   8.0),
    (5,    3.0),
    (0,    1.0),
]


def _si_to_borrow_rate(si_pct: float) -> float:
    """Approximate annualised borrow rate from SI% of Float."""
    for threshold, rate in _SI_TO_BORROW:
        if si_pct >= threshold:
            return rate
    return 1.0


def get_borrow_fee(ticker: str) -> Optional[float]:
    """
    Returns estimated annualised borrow fee as a percentage.
    Returns None only if we can't get any data at all.
    """
    ticker = ticker.upper().strip()

    with _cache_lock:
        if ticker in _cache:
            fee, ts = _cache[ticker]
            if time.time() - ts < _CACHE_TTL:
                return fee

    fee = _fetch_finviz(ticker)
    if fee is None:
        fee = _fetch_stockanalysis(ticker)

    ttl = _CACHE_TTL if fee is not None else _CACHE_TTL_ERROR
    with _cache_lock:
        _cache[ticker] = (fee, time.time() + ttl - _CACHE_TTL)  # store adjusted ts so TTL check works
    logger.debug(f"Borrow fee {ticker}: {fee} (cache ttl={ttl//60}min)")
    return fee


def _fetch_finviz(ticker: str) -> Optional[float]:
    """
    Scrape Finviz quote page for Short Float %.
    Then convert to estimated borrow rate.
    URL: https://finviz.com/quote.ashx?t=TICKER
    The data appears in a table with rows like:
      <td class="snapshot-td2">Short Float</td>
      <td class="snapshot-td2-cp">25.43%</td>
    """
    try:
        url  = f"https://finviz.com/quote.ashx?t={ticker}&p=d"
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            logger.debug(f"Finviz {ticker}: HTTP {resp.status_code}")
            return None

        html = resp.text

        # Find "Short Float" value
        m = re.search(
            r'Short\s+Float\s*</td>\s*<td[^>]*>\s*([\d.]+)%',
            html, re.I
        )
        if not m:
            # Alternative pattern used in different Finviz layouts
            m = re.search(
                r'Short Float.*?>([\d.]+)%',
                html, re.I | re.S
            )
        if not m:
            logger.debug(f"Finviz {ticker}: Short Float not found in page")
            return None

        si_pct = float(m.group(1))
        rate   = _si_to_borrow_rate(si_pct)
        logger.debug(f"Finviz {ticker}: SI={si_pct:.1f}% → estimated borrow={rate:.1f}%")
        return rate

    except Exception as e:
        logger.debug(f"Finviz scrape {ticker}: {e}")
        return None


def _fetch_stockanalysis(ticker: str) -> Optional[float]:
    """
    Secondary fallback: stockanalysis.com/stocks/{ticker}/
    Looks for short interest % in the statistics table.
    """
    try:
        url  = f"https://stockanalysis.com/stocks/{ticker.lower()}/"
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            return None

        # Look for "Short Interest %" or "Short % of Float"
        m = re.search(
            r'Short.{0,30}Float.{0,50}>([\d.]+)\s*%',
            resp.text, re.I | re.S
        )
        if m:
            si_pct = float(m.group(1))
            return _si_to_borrow_rate(si_pct)

    except Exception as e:
        logger.debug(f"StockAnalysis scrape {ticker}: {e}")
    return None


def clear_cache():
    _cache.clear()
