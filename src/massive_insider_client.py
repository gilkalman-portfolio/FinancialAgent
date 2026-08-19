"""
Massive/Polygon Insider Client — SEC Form 4 (insider transactions)

Primary source for per-ticker and market-wide Form 4 lookups, ahead of
sec-api.io: sec-api.io's free tier caps at 100 requests/query and was
observed live hitting 429 ("exceeded the free query limit") during a normal
scan, while Massive/Polygon is an existing paid subscription (MASSIVE_API_KEY)
already used elsewhere in the project (gap_scanner.py, catalyst_scanner.py).
See CLAUDE.md Incident Archive 2026-08-19.

Output contract matches src.sec_api_client exactly (same dict shape) so
InsiderTracker can fall through between sources without special-casing.

Actual field names (verified against the live API 2026-08-19):
  results[].tickers[]                — first entry used as ticker
  results[].owner_name
  results[].is_officer / officer_title / is_director / is_ten_percent_owner
  results[].record_type              — "transaction" (real Form 4 activity)
                                        vs "holding" (a reported position,
                                        not a trade — must be excluded)
  results[].security_type            — "non_derivative" vs "derivative";
                                        only non_derivative matches the
                                        sec-api.io contract this mirrors
                                        (nonDerivativeTable only)
  results[].transaction_code         P=buy, S=sell (also M/A/F etc., filtered out)
  results[].transaction_shares
  results[].transaction_price_per_share
  results[].transaction_value        pre-computed by the API
  results[].transaction_date / period_of_report / filing_date

NOTE: this endpoint's own parameter docs say the ticker filter is named
`ticker` (singular). That's wrong — `ticker=AAPL` is silently ignored and
returns unfiltered market-wide results. `tickers=` (plural, matching the
8-K disclosures endpoint's convention) is what actually filters. Verified
empirically, not from the docs.
"""

import os
import threading
import requests
from datetime import datetime, timedelta
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_thread_local = threading.local()


def _api_key() -> str:
    return os.getenv("MASSIVE_API_KEY", "")


def _base_url() -> str:
    return os.getenv("MASSIVE_BASE_URL", "https://api.polygon.io")


def _thread_session() -> requests.Session:
    """One pooled, keep-alive requests.Session per calling thread — mirrors
    src/gap_scanner.py::_thread_session() / src/sec_api_client.py. Reachable
    from every score_stock() call once watchlist_manager scores tickers
    concurrently (5 workers) — a shared Session under concurrent access is
    the exact pattern that crashed the scheduler process with a native
    STATUS_HEAP_CORRUPTION fault. See CLAUDE.md Incident Archive 2026-08-18."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def _role(rec: dict) -> str:
    parts = []
    if rec.get("is_officer"):
        parts.append(rec.get("officer_title") or "Officer")
    if rec.get("is_director"):
        parts.append("Director")
    if rec.get("is_ten_percent_owner"):
        parts.append("10% Owner")
    return ", ".join(parts) or "Insider"


def _extract_transaction(rec: dict, filter_codes: set) -> dict | None:
    if rec.get("record_type") != "transaction":
        return None  # a reported holding, not an actual trade
    if rec.get("security_type") != "non_derivative":
        return None  # mirrors sec_api_client's nonDerivativeTable-only scope

    code = rec.get("transaction_code", "")
    if code not in filter_codes:
        return None

    shares = float(rec.get("transaction_shares") or 0)
    price  = float(rec.get("transaction_price_per_share") or 0)
    value  = rec.get("transaction_value")
    value  = float(value) if value is not None else shares * price

    tickers = rec.get("tickers") or []
    ticker  = tickers[0] if tickers else ""
    date    = rec.get("transaction_date") or rec.get("period_of_report") or rec.get("filing_date", "")

    return {
        "ticker":  ticker,
        "date":    date,
        "insider": rec.get("owner_name", "Unknown"),
        "role":    _role(rec),
        "type":    "BUY" if code == "P" else "SELL",
        "shares":  shares,
        "price":   price,
        "value":   value,
    }


def get_insider_transactions(ticker: str, days: int = 90) -> list[dict]:
    """Returns flat list of Form 4 BUY/SELL transactions for a ticker.
    Same output contract as src.sec_api_client.get_insider_transactions()."""
    api_key = _api_key()
    if not api_key:
        return []

    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = f"{_base_url()}/stocks/filings/vX/form-4"
    params = {
        "tickers": ticker.upper(),
        "filing_date.gte": since,
        "limit": 250,
        "apiKey": api_key,
    }

    results: list[dict] = []
    pages_left = 4
    try:
        while url and pages_left > 0:
            r = _thread_session().get(url, params=params, timeout=10)
            if r.status_code != 200:
                logger.debug(f"massive_insider get_insider_transactions({ticker}): HTTP {r.status_code}")
                break
            body = r.json()
            for rec in body.get("results", []) or []:
                tx = _extract_transaction(rec, {"P", "S"})
                if tx:
                    results.append(tx)
            url = body.get("next_url")
            params = {"apiKey": api_key} if url else None
            pages_left -= 1
    except Exception as e:
        logger.debug(f"massive_insider get_insider_transactions({ticker}): {e}")
        return []

    return results


def get_recent_insider_buyers(days: int = 7, min_value: float = 50_000, limit: int = 200) -> list[dict]:
    """Reverse lookup — open-market purchases (code P) across all tickers.
    Same output contract as src.sec_api_client.get_recent_insider_buyers()."""
    api_key = _api_key()
    if not api_key:
        return []

    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = f"{_base_url()}/stocks/filings/vX/form-4"
    params = {
        "filing_date.gte": since,
        "limit": 1000,
        "apiKey": api_key,
    }

    results: list[dict] = []
    pages_left = 5
    try:
        while url and len(results) < limit and pages_left > 0:
            r = _thread_session().get(url, params=params, timeout=15)
            if r.status_code != 200:
                logger.warning(f"massive_insider get_recent_insider_buyers: HTTP {r.status_code}")
                break
            body = r.json()
            for rec in body.get("results", []) or []:
                tx = _extract_transaction(rec, {"P"})
                if tx and tx["value"] >= min_value:
                    results.append(tx)
            url = body.get("next_url")
            params = {"apiKey": api_key} if url else None
            pages_left -= 1
    except Exception as e:
        logger.debug(f"massive_insider get_recent_insider_buyers: {e}")

    results.sort(key=lambda x: x["value"], reverse=True)
    return results[:limit]
