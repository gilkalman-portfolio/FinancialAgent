"""
Insider Cluster Scanner — free, market-wide Form 4 cluster-buying detector.

Data-capture layer for the QuantConnect insider-cluster-buying research lead
(CLAUDE.md, project_quantconnect_cross_check memory) — measures the same
signal QC's paid Quiver Insider Trading dataset drove (2+ distinct insiders
buying the same sub-$2B, thin-coverage stock within a rolling window), using
only SEC EDGAR's free daily filing index. Records WATCH rows to
forward_signals for forward measurement; never places a trade — mirrors
forward_signals.record_watch_signals()'s data-capture-only contract for the
News-Catalyst Event-Study Measurement.

Pipeline, once per scheduled run (see scheduler.py::run_insider_cluster_scan):
  1. fetch_daily_form4_filings() — SEC's form.YYYYMMDD.idx, filtered to
     Form Type "4" (market-wide, ~500-900 filings/day, one HTTP request).
  2. Each filing's own XML (fetched via the daily index's file path, no
     extra per-ticker lookup needed) carries issuerTradingSymbol directly —
     regardless of which CIK (issuer's or the individual insider's) the
     filing happened to be indexed under, closing a real gap: SEC indexes
     a Form 4 under whichever CIK filed it, which is the REPORTING PERSON's
     own CIK for the majority of filings, not the issuer's — a naive
     ticker-map lookup on the daily index's CIK column alone would silently
     miss most filings.
  3. Every PURCHASE (transactionCode=='P', matching the P=buy convention
     already established in insider_tracker.py / sec_api_client.py) is
     recorded to insider_purchase_events (ticker, insider name, date) --
     unfiltered by market cap/liquidity, a complete raw record independent
     of the current strategy's universe definition.
  4. Only tickers with >= min_distinct_insiders distinct insiders in the
     trailing cluster_window_hours get a yfinance universe-filter check
     (sub-$2B market cap, min price, min dollar volume, no analyst
     coverage) -- deliberately deferred to this late, small-candidate-set
     step rather than gating collection, so raw capture stays complete and
     yfinance calls stay cheap (dozens/day, not hundreds).
  5. A ticker passing both the cluster and universe checks gets one
     forward_signals WATCH row, unless the same cluster was already logged
     within the last cluster_window_hours (dedup — an ongoing cluster
     shouldn't re-log every single day it stays true).
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv
from lxml import etree
from loguru import logger

from src.database import get_connection, retry_on_busy
from src.yf_cache import get_info as _yf_info

load_dotenv()  # must run before os.getenv() below — matches edgar_fcf.py /
                # insider_tracker.py's identical convention. Missing this
                # sends an empty-email User-Agent ("FinancialAgent ()"),
                # which SEC's automated-access policy is known to penalize
                # more aggressively than a real contact email (found live
                # 2026-08-30 — see the rate-limiter docstring above).
_email = os.getenv("SEC_USER_AGENT_EMAIL", "")
_HEADERS = {"User-Agent": f"FinancialAgent ({_email})"}
_thread_local = threading.local()


class _RateLimiter:
    """Shared, thread-safe throttle across ALL worker threads — SEC's fair-
    access policy caps automated requests at 10/sec TOTAL, not per-thread
    (https://www.sec.gov/os/webmaster-faq#developers). A naive
    ThreadPoolExecutor with no shared throttle can burst far past that in
    the first second (confirmed live 2026-08-30: an untouched 10-worker
    pool hitting ~811 filings got HTTP 429 on nearly everything, and even a
    single follow-up request minutes later was still 429'd — SEC's block
    outlasts the burst itself, not just a per-request limit)."""

    def __init__(self, max_per_second: float = 8.0):
        self._lock = threading.Lock()
        self._min_interval = 1.0 / max_per_second
        self._last_call = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()


_rate_limiter = _RateLimiter(max_per_second=8.0)

# Same fixed-width-with-internal-spaces daily index format used by every SEC
# form.YYYYMMDD.idx file — verified live 2026-08-30 against a real file
# (811 Form 4 rows, 0 parse failures out of 7,417 total lines). Form Type can
# itself contain a single space ("1-A POS"), so this can't be a naive
# whitespace split — it relies on the >=2-space gaps between columns.
_INDEX_LINE_RE = re.compile(
    r"^(?P<form>\S(?:.*?\S)?)\s{2,}(?P<company>\S(?:.*?\S)?)\s{2,}"
    r"(?P<cik>\d+)\s+(?P<date>\d{8})\s+(?P<file>\S+)\s*$"
)
_XML_BLOCK_RE = re.compile(r"<XML>(.*?)</XML>", re.DOTALL)

WATCH_SOURCE_TAG = "[insider_cluster]"


def _thread_session() -> requests.Session:
    """One pooled, keep-alive requests.Session per calling thread — mirrors
    gap_scanner.py/sec_api_client.py/insider_tracker.py's identical pattern.
    See CLAUDE.md Incident Archive, 2026-08-18 (shared-Session-under-
    concurrency crashed the scheduler process with a native heap fault)."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(_HEADERS)
        _thread_local.session = session
    return session


# ── Step 1: daily filing index ──────────────────────────────────────────────

def fetch_daily_form4_filings(day: date) -> list[dict]:
    """Fetch SEC's daily Form-Type index for `day` and return every Form 4
    row as {"cik": str, "company": str, "file": str (path under
    /Archives/edgar/, e.g. "edgar/data/1770787/0001610717-26-000393.txt")}.

    Returns [] on any failure (holiday/weekend with no index published,
    network error, unexpected format) — never raises, matching this
    codebase's soft-enrichment convention (see gap_scanner.fetch_recent_news_catalyst)."""
    quarter = (day.month - 1) // 3 + 1
    url = f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{quarter}/form.{day.strftime('%Y%m%d')}.idx"
    try:
        _rate_limiter.wait()
        resp = _thread_session().get(url, timeout=20)
        if resp.status_code != 200:
            logger.debug(f"[insider_cluster_scanner] daily index {day}: HTTP {resp.status_code}")
            return []
        lines = resp.text.splitlines()
    except Exception as e:
        logger.warning(f"[insider_cluster_scanner] daily index fetch failed for {day}: {e}")
        return []

    try:
        dash_idx = next(i for i, l in enumerate(lines) if l.startswith("---"))
    except StopIteration:
        logger.debug(f"[insider_cluster_scanner] daily index {day}: no header separator found")
        return []

    results = []
    for line in lines[dash_idx + 1:]:
        if not line.strip():
            continue
        m = _INDEX_LINE_RE.match(line)
        if not m or m.group("form") != "4":
            continue
        results.append({
            "cik": m.group("cik"),
            "company": m.group("company"),
            "file": m.group("file"),
        })
    return results


# ── Step 2-3: per-filing XML parse ──────────────────────────────────────────

def _parse_form4_filing(file_path: str) -> Optional[dict]:
    """Fetch one Form 4's full-submission text file and parse its embedded
    ownershipDocument XML. Returns {"ticker": str, "purchases": [{"insider",
    "shares", "price", "date"}]} or None on any failure. The issuer ticker
    comes from the filing's own <issuerTradingSymbol> — always correct
    regardless of which CIK (issuer's or the individual insider's) the
    daily index happened to file this row under."""
    url = f"https://www.sec.gov/Archives/edgar/{file_path}"
    try:
        _rate_limiter.wait()
        resp = _thread_session().get(url, timeout=15)
        if resp.status_code != 200:
            if resp.status_code == 429:
                logger.warning(f"[insider_cluster_scanner] HTTP 429 (rate limited) on {file_path}")
            return None
        m = _XML_BLOCK_RE.search(resp.text)
        if not m:
            return None
        root = etree.fromstring(m.group(1).strip().encode())
    except Exception as e:
        logger.debug(f"[insider_cluster_scanner] filing parse failed ({file_path}): {e}")
        return None

    ticker_nodes = root.xpath("//issuerTradingSymbol/text()")
    if not ticker_nodes or not ticker_nodes[0].strip():
        return None
    ticker = ticker_nodes[0].strip().upper()

    owner_nodes = root.xpath("//rptOwnerName/text()")
    owner = owner_nodes[0].strip() if owner_nodes else "Unknown"

    purchases = []
    for tx in root.xpath("//nonDerivativeTransaction"):
        try:
            code_nodes = tx.xpath(".//transactionCode/text()")
            if not code_nodes or code_nodes[0] != "P":  # P=open-market buy — same
                continue                                 # convention as insider_tracker.py
            shares_nodes = tx.xpath(".//transactionShares/value/text()")
            price_nodes = tx.xpath(".//transactionPricePerShare/value/text()")
            date_nodes = tx.xpath(".//transactionDate/value/text()")
            if not shares_nodes or not date_nodes:
                continue
            purchases.append({
                "insider": owner,
                "shares": float(shares_nodes[0]),
                "price": float(price_nodes[0]) if price_nodes else None,
                "date": date_nodes[0],
            })
        except Exception:
            continue

    if not purchases:
        return None
    return {"ticker": ticker, "purchases": purchases}


# ── Step 4: universe filter (only run against cluster candidates) ──────────

def _passes_universe_filter(ticker: str, max_market_cap: float, min_price: float,
                             min_dollar_volume: float) -> bool:
    """Sub-$2B / liquid / no-analyst-coverage filter — the exact universe
    definition validated in the QC research (market_cap<$2B, price>$1,
    dollar_volume>$300K, forward_pe None/<=0 as a "no analyst coverage"
    proxy, since free fundamental data has no literal analyst-count field).
    Fails closed (returns False) on any lookup error — a data-fetch failure
    should never let an unvetted ticker through."""
    try:
        info = _yf_info(ticker, ttl=3600)
        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        if price < min_price:
            return False
        market_cap = info.get("marketCap") or 0
        if market_cap <= 0 or market_cap >= max_market_cap:
            return False
        avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day") or 0
        if price * avg_volume < min_dollar_volume:
            return False
        forward_pe = info.get("forwardPE")
        if forward_pe is not None and forward_pe > 0:
            return False
        return True
    except Exception as e:
        logger.debug(f"[insider_cluster_scanner] universe filter failed for {ticker}: {e}")
        return False


# ── DB: raw purchase-event log + cluster query ──────────────────────────────

@retry_on_busy()
def _record_purchase_events(ticker: str, purchases: list[dict]) -> int:
    """INSERT OR IGNORE every purchase into insider_purchase_events — the
    UNIQUE(ticker, insider_name, transaction_date) constraint makes this
    naturally idempotent if the same day's index is ever re-processed."""
    inserted = 0
    now = datetime.now().isoformat()
    with get_connection() as conn:
        for p in purchases:
            cur = conn.execute(
                """INSERT OR IGNORE INTO insider_purchase_events
                   (ticker, insider_name, transaction_date, price, shares, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ticker, p["insider"], p["date"], p.get("price"), p.get("shares"), now),
            )
            if cur.rowcount:
                inserted += 1
    return inserted


def _distinct_insiders_in_window(ticker: str, window_hours: int) -> list[str]:
    cutoff = (datetime.now() - timedelta(hours=window_hours)).strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT insider_name FROM insider_purchase_events
               WHERE ticker = ? AND transaction_date >= ?""",
            (ticker, cutoff),
        ).fetchall()
    return [r[0] for r in rows]


def _last_cluster_watch_recorded_at(ticker: str) -> Optional[datetime]:
    """Dedup check — has this ticker already gotten an insider-cluster WATCH
    row recently? Uses catalyst_summary's fixed WATCH_SOURCE_TAG prefix
    rather than the ai_verdict column (already repurposed for sentiment
    direction by the news-catalyst WATCH source — see forward_signals.py —
    so a distinct dedup mechanism per WATCH source avoids the two colliding)."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT signal_ts FROM forward_signals
               WHERE ticker = ? AND signal_type = 'WATCH'
                 AND catalyst_summary LIKE ?
               ORDER BY signal_ts DESC LIMIT 1""",
            (ticker, f"{WATCH_SOURCE_TAG}%"),
        ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None


# ── Orchestrator ─────────────────────────────────────────────────────────────

def scan_insider_clusters(
    day: Optional[date] = None,
    max_market_cap: float = 2_000_000_000,
    min_price: float = 1.0,
    min_dollar_volume: float = 300_000,
    cluster_window_hours: int = 72,
    min_distinct_insiders: int = 2,
    max_workers: int = 10,
) -> dict:
    """Run one full scan cycle. Returns stats: {filings_checked, purchases_found,
    tickers_touched, cluster_candidates, watch_recorded, watch_deduped}."""
    from src.forward_signals import record_signal, SignalRecord

    day = day or date.today()
    stats = {
        "filings_checked": 0, "purchases_found": 0, "tickers_touched": 0,
        "cluster_candidates": 0, "watch_recorded": 0, "watch_deduped": 0,
    }

    filings = fetch_daily_form4_filings(day)
    stats["filings_checked"] = len(filings)
    if not filings:
        logger.info(f"[insider_cluster_scanner] {day}: no Form 4 filings found (holiday/weekend/fetch failure)")
        return stats

    # Phase 1: concurrent fetch+parse — no DB writes here (mirrors
    # watchlist_manager.py's score-then-write two-phase split).
    parsed: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for result in pool.map(lambda f: _parse_form4_filing(f["file"]), filings):
            if result:
                parsed.append(result)

    by_ticker: dict[str, list[dict]] = {}
    for r in parsed:
        by_ticker.setdefault(r["ticker"], []).extend(r["purchases"])
    stats["tickers_touched"] = len(by_ticker)
    stats["purchases_found"] = sum(len(v) for v in by_ticker.values())

    # Phase 2: sequential DB writes — raw capture, unfiltered by universe.
    for ticker, purchases in by_ticker.items():
        _record_purchase_events(ticker, purchases)

    # Phase 3: cluster check (cheap, DB-only) → universe filter (yfinance,
    # only for candidates that already clear the cluster bar).
    for ticker in by_ticker:
        distinct = _distinct_insiders_in_window(ticker, cluster_window_hours)
        if len(distinct) < min_distinct_insiders:
            continue
        stats["cluster_candidates"] += 1

        last_watch = _last_cluster_watch_recorded_at(ticker)
        if last_watch and (datetime.now() - last_watch) < timedelta(hours=cluster_window_hours):
            stats["watch_deduped"] += 1
            continue

        if not _passes_universe_filter(ticker, max_market_cap, min_price, min_dollar_volume):
            continue

        info = _yf_info(ticker, ttl=3600)
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            continue

        record_signal(SignalRecord(
            ticker=ticker,
            signal_type="WATCH",
            entry_price=float(price),
            catalyst_summary=f"{WATCH_SOURCE_TAG} {len(distinct)} distinct insiders in {cluster_window_hours}h: {sorted(distinct)}",
            ai_verdict=f"cluster_{len(distinct)}",
        ))
        stats["watch_recorded"] += 1
        logger.info(f"[insider_cluster_scanner] WATCH recorded: {ticker} — {len(distinct)} distinct insiders")

    logger.info(f"[insider_cluster_scanner] {day}: {stats}")
    return stats
