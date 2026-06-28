"""
EDGAR FCF Provider — fetches multi-year Free Cash Flow from SEC EDGAR XBRL API.

Source:   https://data.sec.gov/api/xbrl/companyfacts/CIK{padded}.json
Free, no API key required. Rate limit: 10 req/sec (SEC policy).

FCF = NetCashProvidedByUsedInOperatingActivities
       - PaymentsToAcquirePropertyPlantAndEquipment

Returns median of last 4 annual (10-K) values to smooth one-time swings.
Falls back gracefully to None so callers can use yfinance as backup.
"""

import os
import time
import statistics
import requests
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_email   = os.getenv("SEC_USER_AGENT_EMAIL", "agent@example.com")
_HEADERS = {"User-Agent": f"FinancialAgent ({_email})"}

# Ticker → CIK map: loaded once per day from SEC
_TICKER_CIK: dict       = {}
_CIK_LOADED_AT: Optional[datetime] = None
_CIK_TTL = timedelta(days=1)

# Per-ticker facts cache: 24h TTL — shared by all helpers to avoid duplicate SEC requests
_FACTS_CACHE: dict = {}   # ticker -> (loaded_at: datetime, facts: dict)
_FACTS_TTL = timedelta(hours=24)


def _fetch_facts(ticker: str) -> Optional[dict]:
    """Fetch and cache full EDGAR facts dict for a ticker (one HTTP request per ticker per day)."""
    now = datetime.now()
    if ticker in _FACTS_CACHE:
        cached_at, facts = _FACTS_CACHE[ticker]
        if now - cached_at < _FACTS_TTL:
            return facts
    cik = _get_cik(ticker)
    if not cik:
        return None
    try:
        time.sleep(0.12)   # stay comfortably under 10 req/sec (SEC policy)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        facts = r.json().get("facts", {})
        _FACTS_CACHE[ticker] = (now, facts)
        return facts
    except Exception as exc:
        logger.debug(f"[EDGAR] {ticker}: {exc}")
        return None

_OCF_TAG   = "NetCashProvidedByUsedInOperatingActivities"
_CAPEX_TAG = "PaymentsToAcquirePropertyPlantAndEquipment"

# ── CIK lookup ────────────────────────────────────────────────────────────────

def _load_cik_map() -> dict:
    global _TICKER_CIK, _CIK_LOADED_AT
    now = datetime.now()
    if _CIK_LOADED_AT and (now - _CIK_LOADED_AT) < _CIK_TTL:
        return _TICKER_CIK
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_HEADERS, timeout=15
        )
        r.raise_for_status()
        data = r.json()
        _TICKER_CIK = {
            v["ticker"].upper(): str(v["cik_str"]).zfill(10)
            for v in data.values()
        }
        _CIK_LOADED_AT = now
        logger.debug(f"[EDGAR FCF] CIK map: {len(_TICKER_CIK)} tickers loaded")
    except Exception as exc:
        logger.warning(f"[EDGAR FCF] CIK map fetch failed: {exc}")
    return _TICKER_CIK


def _get_cik(ticker: str) -> Optional[str]:
    return _load_cik_map().get(ticker.upper())


# ── XBRL concept extraction ───────────────────────────────────────────────────

def _annual_vals(facts: dict, concept: str) -> list[float]:
    """Return annual 10-K FY values for a US-GAAP concept, newest-first, deduped."""
    try:
        entries = (
            facts.get("us-gaap", {})
                 .get(concept, {})
                 .get("units", {})
                 .get("USD", [])
        )
        # Only full-year 10-K rows
        annual = [e for e in entries if e.get("form") == "10-K" and e.get("fp") == "FY"]
        annual.sort(key=lambda e: e["end"], reverse=True)
        seen, out = set(), []
        for e in annual:
            if e["end"] not in seen:
                seen.add(e["end"])
                out.append(float(e["val"]))
        return out
    except Exception:
        return []


# ── Public API ────────────────────────────────────────────────────────────────

def get_edgar_fcf_series(ticker: str, years: int = 4) -> Optional[list[float]]:
    """
    Return up to `years` annual FCF values (newest-first) from EDGAR.
    FCF = OCF - CapEx. Uses shared _FACTS_CACHE to avoid duplicate SEC requests.
    """
    facts = _fetch_facts(ticker)
    if facts is None:
        return None

    ocf_vals   = _annual_vals(facts, _OCF_TAG)
    capex_vals = _annual_vals(facts, _CAPEX_TAG)

    if not ocf_vals or not capex_vals:
        logger.debug(f"[EDGAR FCF] {ticker}: missing OCF or CapEx concept")
        return None

    n    = min(len(ocf_vals), len(capex_vals), years)
    vals = [ocf_vals[i] - capex_vals[i] for i in range(n)]

    logger.debug(f"[EDGAR FCF] {ticker}: {n}yr FCF = {[f'{v/1e6:.0f}M' for v in vals]}")
    return vals


def get_edgar_fcf_median(ticker: str) -> Optional[float]:
    """
    Single representative FCF for DCF input: median of last 4 positive annual values.
    Median is more robust than TTM alone — smooths one-off items (asset sales,
    restructuring charges) that can distort any single year's OCF.
    Returns None if EDGAR unavailable or company has no positive FCF years.
    """
    vals = get_edgar_fcf_series(ticker, years=4)
    if not vals:
        return None
    positive = [v for v in vals if v > 0]
    if not positive:
        return None
    return statistics.median(positive)


# ── Fundamental helpers ───────────────────────────────────────────────────────

def get_revenue_cagr(ticker: str, years: int = 5) -> Optional[float]:
    """
    5-year annual revenue CAGR from EDGAR 10-K filings.
    More stable than yfinance revenueGrowth (1yr only, noisy).
    Returns decimal (e.g. 0.08 = 8%), or None on failure.
    """
    facts = _fetch_facts(ticker)
    if facts is None:
        return None

    # Try primary concept, fall back to Revenues
    vals = _annual_vals(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
    if len(vals) < 2:
        vals = _annual_vals(facts, "Revenues")
    if len(vals) < 2:
        return None

    n = min(len(vals), years)
    # vals is newest-first; vals[0] = most recent, vals[n-1] = oldest in window
    try:
        cagr = (vals[0] / vals[n - 1]) ** (1 / (n - 1)) - 1
        return cagr
    except (ZeroDivisionError, ValueError):
        return None


def get_interest_coverage(ticker: str) -> Optional[float]:
    """
    Interest Coverage Ratio = EBIT / InterestExpense (most recent annual 10-K).
    ICR > 5: strong, 2-5: adequate, < 2: risky.
    Returns float or None.
    """
    facts = _fetch_facts(ticker)
    if facts is None:
        return None

    ebit_vals = _annual_vals(facts, "OperatingIncomeLoss")
    interest_vals = _annual_vals(facts, "InterestExpense")

    if not ebit_vals or not interest_vals:
        return None

    ebit = ebit_vals[0]
    interest = interest_vals[0]

    if interest == 0:
        return None

    icr = abs(ebit) / abs(interest)
    return min(max(icr, 0.0), 100.0)


def get_current_ratio(ticker: str) -> Optional[float]:
    """
    Current Ratio = AssetsCurrent / LiabilitiesCurrent (most recent annual 10-K).
    > 1.5: healthy, 1.0-1.5: adequate, < 1.0: risky.
    Returns float or None.
    """
    facts = _fetch_facts(ticker)
    if facts is None:
        return None

    assets_vals = _annual_vals(facts, "AssetsCurrent")
    liab_vals = _annual_vals(facts, "LiabilitiesCurrent")

    if not assets_vals or not liab_vals:
        return None

    current_assets = assets_vals[0]
    current_liabilities = liab_vals[0]

    if current_liabilities == 0:
        return None

    return current_assets / current_liabilities


def get_eps_yoy_growth(ticker: str, quarters: int = 4) -> Optional[float]:
    """
    Average YoY EPS growth over last N quarters from EDGAR 10-Q filings.
    Used as proxy for earnings quality when Finnhub is unavailable.
    Returns decimal (e.g. 0.20 = 20% avg YoY growth), or None.
    """
    from datetime import date as date_type

    facts = _fetch_facts(ticker)
    if facts is None:
        return None

    try:
        entries = (
            facts.get("us-gaap", {})
                 .get("EarningsPerShareDiluted", {})
                 .get("units", {})
                 .get("USD/shares", [])
        )
    except Exception:
        return None

    # Filter to 10-Q only, pure quarterly (60–105 days)
    quarterly = []
    for e in entries:
        if e.get("form") != "10-Q":
            continue
        try:
            start = date_type.fromisoformat(e["start"])
            end = date_type.fromisoformat(e["end"])
            days = (end - start).days
            if 60 <= days <= 105:
                quarterly.append(e)
        except (KeyError, ValueError):
            continue

    if not quarterly:
        return None

    # Sort newest-first, deduplicate by end date
    quarterly.sort(key=lambda e: e["end"], reverse=True)
    seen, deduped = set(), []
    for e in quarterly:
        if e["end"] not in seen:
            seen.add(e["end"])
            deduped.append(e)

    # Need at least quarters + 4 entries to compute YoY
    if len(deduped) < quarters + 4:
        return None

    recent = deduped[:quarters]         # most recent N quarters
    year_ago = deduped[4: quarters + 4] # same quarters one year prior

    yoy_values = []
    for now_e, ago_e in zip(recent, year_ago):
        try:
            eps_now = float(now_e["val"])
            eps_ago = float(ago_e["val"])
            if eps_ago == 0 or eps_ago < 0:
                continue
            yoy_values.append((eps_now - eps_ago) / abs(eps_ago))
        except (KeyError, ValueError, ZeroDivisionError):
            continue

    if not yoy_values:
        return None

    return sum(yoy_values) / len(yoy_values)
