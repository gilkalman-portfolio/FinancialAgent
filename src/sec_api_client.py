"""
SEC API Client — sec-api.io wrapper
Endpoint: Form 3/4/5 - Insider Trading Disclosures

Actual field names (verified from live response):
  filing.issuer.tradingSymbol
  filing.reportingOwner.name
  filing.reportingOwner.relationship.{isDirector, isOfficer, officerTitle, ...}
  filing.nonDerivativeTable.transactions[].coding.code         ← P=buy, S=sell, M=vesting
  filing.nonDerivativeTable.transactions[].amounts.shares
  filing.nonDerivativeTable.transactions[].amounts.pricePerShare
  filing.periodOfReport
"""

import os
import time
import requests
from datetime import datetime, timedelta
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_API_KEY  = os.getenv("SEC_API_KEY", "")
_BASE_URL = "https://api.sec-api.io"

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


def _post(endpoint: str, payload: dict) -> dict | None:
    if not _API_KEY:
        logger.warning("SEC_API_KEY not set — sec_api_client disabled")
        return None
    resp = None
    try:
        resp = _session.post(
            f"{_BASE_URL}/{endpoint}",
            params={"token": _API_KEY},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        body = resp.text[:500] if resp is not None else "no response"
        logger.warning(f"sec-api.io error ({endpoint}): {e} | body: {body}")
        return None


def _extract_transactions(filing: dict, filter_codes: set = None) -> list[dict]:
    """
    Parse a single Form 4 filing into a flat list of transactions.
    filter_codes: set of transaction codes to keep e.g. {"P", "S"}. None = all.
    Verified field names from live API response.
    """
    filter_codes = filter_codes or {"P", "S"}
    results = []

    ticker  = filing.get("issuer", {}).get("tradingSymbol", "")
    date    = filing.get("periodOfReport", "")

    # reportingOwner can be a dict OR a list (multiple owners in one filing)
    raw_owner = filing.get("reportingOwner", {})
    owner = raw_owner[0] if isinstance(raw_owner, list) else raw_owner
    insider = owner.get("name", "Unknown")

    rel = owner.get("relationship", {})
    role_parts = []
    if rel.get("isOfficer"):         role_parts.append(rel.get("officerTitle", "Officer"))
    if rel.get("isDirector"):        role_parts.append("Director")
    if rel.get("isTenPercentOwner"): role_parts.append("10% Owner")
    role = ", ".join(role_parts) or "Insider"

    nd_table = filing.get("nonDerivativeTable", {})
    txns     = nd_table.get("transactions", [])
    if isinstance(txns, dict):
        txns = [txns]

    for tx in txns:
        try:
            code = tx.get("coding", {}).get("code", "")  # ← "code" not "transactionCode"
            if code not in filter_codes:
                continue

            amounts = tx.get("amounts", {})
            shares  = float(amounts.get("shares", 0) or 0)               # ← "shares" not "transactionShares"
            price   = float(amounts.get("pricePerShare", 0) or 0)        # ← "pricePerShare" not "transactionPricePerShare"
            value   = shares * price

            results.append({
                "ticker":  ticker,
                "date":    date,
                "insider": insider,
                "role":    role,
                "type":    "BUY" if code == "P" else "SELL",
                "shares":  shares,
                "price":   price,
                "value":   value,
            })
        except Exception as e:
            logger.debug(f"sec_api parse tx error: {e}")
            continue

    return results


def get_insider_transactions(ticker: str, days: int = 90) -> list[dict]:
    """
    Returns flat list of Form 4 BUY/SELL transactions for a ticker.
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    payload = {
        "query": {
            "query_string": {
                "query": f'issuer.tradingSymbol:"{ticker}" AND periodOfReport:[{since} TO *] AND documentType:"4"'
            }
        },
        "from": 0,
        "size": 50,
        "sort": [{"periodOfReport": {"order": "desc"}}],
    }

    data = _post("insider-trading", payload)
    if not data:
        return []

    results = []
    for filing in data.get("transactions", []):
        results.extend(_extract_transactions(filing, {"P", "S"}))

    return results


def get_recent_insider_buyers(days: int = 7, min_value: float = 50_000, limit: int = 200) -> list[dict]:
    """
    Reverse lookup — open-market purchases (code P) across all tickers.
    Paginates in chunks of 50 (API max) until limit is reached.
    """
    since   = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    results = []
    page    = 0
    page_size = 50  # API hard limit

    while len(results) < limit:
        payload = {
            "query": {
                "query_string": {
                    # Use filedAt instead of periodOfReport — catches filings faster
                    "query": f'filedAt:[{since} TO *] AND documentType:"4"'
                }
            },
            "from": page * page_size,
            "size": page_size,
            "sort": [{"filedAt": {"order": "desc"}}],
        }

        data = _post("insider-trading", payload)
        if not data:
            logger.warning(f"get_recent_insider_buyers: page {page} returned None — stopping")
            break

        filings = data.get("transactions", [])
        if not filings:
            break

        for filing in filings:
            for tx in _extract_transactions(filing, {"P"}):
                if tx["value"] >= min_value:
                    results.append(tx)

        logger.info(f"get_recent_insider_buyers: page {page} — {len(filings)} filings, {len(results)} P-purchases so far")

        if len(filings) < page_size:
            break  # last page

        page += 1
        time.sleep(0.3)  # avoid 429 rate limit

    results.sort(key=lambda x: x["value"], reverse=True)
    logger.info(f"get_recent_insider_buyers: total {len(results)} purchases >= ${min_value:,.0f}")
    return results
