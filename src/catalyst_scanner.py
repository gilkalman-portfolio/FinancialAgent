"""
Catalyst Scanner
Finds small/mid-cap stocks with upcoming catalysts that have high explosion potential.

Catalyst types supported:
  - Earnings     (Nasdaq API)
  - Analyst      (Finnhub upgrade/downgrade in last N days)
  - SEC 8-K      (EDGAR recent material filings in last N days)
  - FDA/PDUFA    (BioPharma Catalyst public calendar — no API key required)

Ticker sources supported:
  - Nasdaq earnings calendar (all reporting stocks)
  - FDA/PDUFA calendar (all biotech/pharma stocks with upcoming action dates)
  - Watchlist (from DB)
  - Manual list (user-supplied tickers)
  - Index / sector (via index_loader — Russell 2000 + Health Care recommended for biotech)

Explosion Score (0-100):
  Urgency              0-30  (how close the event is)
  SI% Fuel             0-25  (short interest = trapped sellers)
  Float Amplifier      0-20  (low float = bigger price swings)
  Volume Building      0-10  (accumulation before event)
  Insider Buying       0-10  (insiders buying before catalyst)
  Momentum             0-5   (price trend)
  Unusual Options      0-8   (unusual call activity or bullish PCR via yfinance)

PDUFA cache: data/pdufa_cache.json (TTL: 6 hours)
"""

import os
import requests
import yfinance as yf
from src.yf_cache import get_info as _yf_info, get_history as _yf_hist
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional, Callable, List, Dict
from loguru import logger

from src.market_feed import get_earnings_calendar
from src.news_fetcher import fetch_yfinance_news, detect_news_catalyst


# ── Component scorers (pure — no I/O) ────────────────────────────────────────

def _urgency_pts(days: int) -> float:
    """Closer event = more urgency (0-30)."""
    if days <= 0:  return 30
    if days == 1:  return 27
    if days == 2:  return 22
    if days == 3:  return 17
    if days <= 5:  return 12
    if days <= 7:  return 8
    return 4


def _si_pts(si_pct: float) -> float:
    """Short interest fuel — trapped shorts amplify upside moves (0-25)."""
    if si_pct >= 20:  return 25
    if si_pct >= 15:  return 18
    if si_pct >= 10:  return 11
    if si_pct >= 5:   return 5
    return 0


def _float_pts(float_m: Optional[float]) -> float:
    """Low float = bigger % swings on same dollar volume (0-20)."""
    if float_m is None:   return 3
    if float_m <= 5:      return 20
    if float_m <= 15:     return 16
    if float_m <= 40:     return 11
    if float_m <= 100:    return 6
    return 2


def _volume_pts(vol_ratio: float) -> float:
    """Recent volume vs 30d avg — accumulation signal (0-10)."""
    if vol_ratio >= 3.0:  return 10
    if vol_ratio >= 2.0:  return 7
    if vol_ratio >= 1.5:  return 4
    if vol_ratio >= 1.2:  return 2
    return 0


def _momentum_pts(pct_5d: Optional[float]) -> float:
    """5-day price momentum — not going down already (0-5)."""
    if pct_5d is None:  return 0
    if pct_5d >= 5:     return 5
    if pct_5d >= 2:     return 3
    if pct_5d >= 0:     return 1
    return 0


def explosion_score(
    days_to_event: int,
    si_pct: float,
    float_m: Optional[float],
    vol_ratio: float,
    has_insider: bool,
    pct_5d: Optional[float] = None,
    unusual_options_pts: float = 0,
) -> float:
    """Composite explosion potential score (0-100). Pure function — testable without I/O."""
    pts = (
        _urgency_pts(days_to_event)
        + _si_pts(si_pct)
        + _float_pts(float_m)
        + _volume_pts(vol_ratio)
        + (10 if has_insider else 0)
        + _momentum_pts(pct_5d)
        + unusual_options_pts
    )
    return min(100.0, round(pts, 1))


def score_label(score: float) -> tuple[str, str]:
    """(label, color) for explosion score."""
    if score >= 70:  return "HIGH",   "#7c3aed"
    if score >= 50:  return "MEDIUM", "#dc2626"
    if score >= 30:  return "LOW",    "#d97706"
    return "WATCH",  "#6b7280"


# ── Technical snapshot ────────────────────────────────────────────────────────

def _ta_snapshot(hist: pd.DataFrame) -> Dict:
    """
    Lightweight technical analysis from price history.
    Returns RSI, MACD signal, MA trend, BB position.
    Does NOT require the full TechnicalIndicators class.
    """
    result = {"rsi": None, "macd": "N/A", "ma_trend": "N/A", "rsi_signal": ""}
    if hist is None or len(hist) < 20:
        return result

    close = hist["Close"]

    # RSI (14)
    try:
        delta  = close.diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, 1e-9)
        rsi_s  = 100 - (100 / (1 + rs))
        rsi    = float(rsi_s.iloc[-1])
        result["rsi"] = round(rsi, 1)
        if rsi < 30:      result["rsi_signal"] = "Oversold"
        elif rsi > 70:    result["rsi_signal"] = "Overbought"
        elif rsi > 55:    result["rsi_signal"] = "Bullish"
        else:             result["rsi_signal"] = "Neutral"
    except Exception as e:
        logger.debug(f"[ta_snapshot] RSI failed: {e}")

    # MACD (12/26/9)
    try:
        ema12   = close.ewm(span=12, adjust=False).mean()
        ema26   = close.ewm(span=26, adjust=False).mean()
        macd    = ema12 - ema26
        signal  = macd.ewm(span=9, adjust=False).mean()
        result["macd"] = "Bullish" if macd.iloc[-1] > signal.iloc[-1] else "Bearish"
    except Exception as e:
        logger.debug(f"[ta_snapshot] MACD failed: {e}")

    # MA Trend (20/50/200)
    try:
        p = float(close.iloc[-1])
        sma20  = float(close.rolling(20).mean().iloc[-1])  if len(close) >= 20  else None
        sma50  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else None
        sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
        above = sum(1 for s in [sma20, sma50, sma200] if s and p > s)
        result["ma_trend"] = ["Downtrend", "Weak", "Mixed", "Uptrend"][above]
    except Exception as e:
        logger.debug(f"[ta_snapshot] MA trend failed: {e}")

    return result


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_ticker_data(ticker: str) -> Optional[Dict]:
    """
    Fetch price, SI%, float, volume ratio, momentum, and TA snapshot from yfinance.
    Returns None on failure (ticker delisted, no data, etc.).
    """
    try:
        info = _yf_info(ticker, ttl=900)  # 15-min cache — SI/float stable within scan

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            return None

        market_cap   = info.get("marketCap")
        float_shares = info.get("floatShares")
        float_m      = float_shares / 1e6 if float_shares else None
        si_pct       = (info.get("shortPercentOfFloat") or 0) * 100
        dtc          = info.get("shortRatio") or 0  # Days to Cover (short interest / avg daily vol)

        hist = _yf_hist(ticker, period="1y", ttl=1800)  # 30-min cache — 1y history stable
        vol_ratio = 1.0
        pct_5d    = None

        if len(hist) >= 6:
            vol_5     = hist["Volume"].iloc[-5:].mean()
            vol_30    = hist["Volume"].iloc[-30:].mean() if len(hist) >= 30 else hist["Volume"].mean()
            vol_ratio = vol_5 / vol_30 if vol_30 > 0 else 1.0
            close_now = hist["Close"].iloc[-1]
            close_5d  = hist["Close"].iloc[-6]
            pct_5d    = (close_now - close_5d) / close_5d * 100 if close_5d else None

        ta = _ta_snapshot(hist)

        return {
            "price":      float(price),
            "market_cap": market_cap,
            "float_m":    float_m,
            "si_pct":     si_pct,
            "dtc":        dtc,
            "vol_ratio":  vol_ratio,
            "pct_5d":     pct_5d,
            "name":       info.get("shortName") or info.get("longName") or ticker,
            "sector":     info.get("sector", ""),
            "rsi":        ta["rsi"],
            "rsi_signal": ta["rsi_signal"],
            "macd":       ta["macd"],
            "ma_trend":   ta["ma_trend"],
        }
    except Exception as e:
        logger.debug(f"catalyst._fetch_ticker_data({ticker}): {e}")
        return None


def _get_insider_signal(ticker: str) -> Optional[Dict]:
    """
    Returns insider activity dict or None if no data / check disabled.
    Dict keys: buys, sells, net, clustered, value_bought
    """
    try:
        from src.insider_tracker import InsiderTracker
        r = InsiderTracker().calculate_conviction_score(ticker)
        if r.total_purchases_90d == 0 and r.total_sales_90d == 0:
            return None
        return {
            "buys":      r.total_purchases_90d,
            "sells":     r.total_sales_90d,
            "net":       r.net_buying_90d,
            "clustered": r.clustered_buying,
            "value":     r.total_value_bought,
        }
    except Exception as e:
        logger.warning(f"[insider] {ticker}: {e}")
        return None


# ── Unusual options scorer ────────────────────────────────────────────────────

def _unusual_options_pts(ticker: str) -> tuple[float, bool]:
    """
    Score unusual call activity for a ticker using the existing options_flow module.
    Returns (pts, has_unusual_calls).
    +8 pts if unusual CALL contracts detected (vol/OI ≥ 3x or absolute vol ≥ 5000).
    +4 pts if put/call ratio < 0.7 (bullish options sentiment) with no unusual calls.
    Returns (0, False) on any failure — options data is unavailable for many small caps.
    """
    try:
        from src.options_flow import get_options_summary
        data = get_options_summary(ticker, max_expirations=3)
        if not data:
            return 0.0, False

        unusual_calls = [u for u in data.get("unusual", []) if u["side"] == "CALL"]
        if unusual_calls:
            return 8.0, True

        pcr_vol = data.get("pcr_vol")
        if pcr_vol is not None and pcr_vol < 0.7:
            return 4.0, False

        return 0.0, False
    except Exception as e:
        logger.debug(f"_unusual_options_pts({ticker}): {e}")
        return 0.0, False


# ── PDUFA / FDA catalyst fetcher ──────────────────────────────────────────────

_PDUFA_CACHE_PATH = None   # lazy-init in fetch_pdufa_events


def _scrape_biopharma_catalyst() -> List[Dict]:
    """
    Scrape BioPharma Catalyst FDA calendar (public, no API key).
    Returns [] gracefully on any error.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 not installed — run: pip install beautifulsoup4")
        return []

    try:
        url = "https://www.biopharmacatalyst.com/calendars/fda-calendar"
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code != 200:
            logger.warning(f"biopharmacatalyst: HTTP {r.status_code}")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        events: List[Dict] = []

        # Try the main data table — rows have: Ticker | Company | Catalyst | Date | Notes
        rows = (soup.select("table tbody tr")
                or soup.select("tbody tr")
                or soup.select("tr"))

        DATE_FMTS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            ticker_raw = cells[0].get_text(strip=True).upper()
            if not ticker_raw or len(ticker_raw) > 7 or not ticker_raw.replace(".", "").isalpha():
                continue

            cat_type = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            date_raw = cells[3].get_text(strip=True) if len(cells) > 3 else ""

            ts = None
            date_disp = date_raw
            for fmt in DATE_FMTS:
                try:
                    dt = datetime.strptime(date_raw, fmt)
                    ts = dt.timestamp()
                    date_disp = dt.strftime("%a %b %d")
                    break
                except Exception:
                    continue
            if ts is None:
                continue

            cat_upper = cat_type.upper()
            if "PDUFA" in cat_upper:
                label = "FDA PDUFA"
            elif "ADCOM" in cat_upper or "ADVISORY" in cat_upper:
                label = "FDA AdCom"
            elif "PHASE" in cat_upper:
                label = f"Phase Trial ({cat_type})"
            else:
                label = f"FDA ({cat_type})" if cat_type else "FDA Catalyst"

            events.append({
                "ticker":   ticker_raw,
                "type":     label,
                "date":     date_disp,
                "time":     "",
                "detail":   f"{cat_type} — regulatory action",
                "ts":       ts,
                "mcap_val": None,
            })

        logger.info(f"biopharmacatalyst: scraped {len(events)} FDA events")
        return events

    except Exception as e:
        logger.warning(f"biopharmacatalyst scrape failed: {e}")
        return []


def _filter_pdufa_by_window(events: List[Dict], days_ahead: int,
                             tickers_filter: Optional[set]) -> List[Dict]:
    """Keep only events within the date window and matching tickers_filter (if set)."""
    now    = datetime.now()
    cutoff = now + timedelta(days=days_ahead)
    out    = []
    for ev in events:
        try:
            event_dt = datetime.fromtimestamp(ev["ts"])
            if event_dt < now or event_dt > cutoff:
                continue
        except Exception:
            continue
        if tickers_filter and ev["ticker"] not in tickers_filter:
            continue
        out.append(ev)
    return out


def fetch_pdufa_events(days_ahead: int = 30,
                       tickers_filter: Optional[set] = None) -> List[Dict]:
    """
    Return upcoming FDA PDUFA / AdCom events.
    Source: BioPharma Catalyst public calendar (no API key needed).
    Cache: 6 hours in data/pdufa_cache.json.

    If tickers_filter is provided, only return events for those tickers.
    If tickers_filter is None, return all events within days_ahead window.
    """
    import json
    from pathlib import Path

    cache_path = Path(__file__).parent.parent / "data" / "pdufa_cache.json"
    CACHE_TTL_SECS = 6 * 3600

    # Try cache first
    try:
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(cached.get("cached_at", "2000-01-01"))
            if (datetime.now() - cached_at).total_seconds() < CACHE_TTL_SECS:
                logger.debug(f"pdufa_events: cache hit ({len(cached.get('events', []))} events)")
                return _filter_pdufa_by_window(cached.get("events", []), days_ahead, tickers_filter)
    except Exception:
        pass

    events = _scrape_biopharma_catalyst()

    # Save cache
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"cached_at": datetime.now().isoformat(), "events": events}),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug(f"pdufa_cache write: {e}")

    return _filter_pdufa_by_window(events, days_ahead, tickers_filter)


# ── Catalyst event fetchers ───────────────────────────────────────────────────

def fetch_earnings_events(days_ahead: int = 7, tickers_filter: Optional[set] = None) -> List[Dict]:
    """
    Pull upcoming earnings from Nasdaq API.
    If tickers_filter is provided, only return events for those tickers.
    """
    raw = get_earnings_calendar(days_ahead)
    events = []
    for ev in raw:
        sym = (ev.get("symbol") or "").upper().strip()
        if not sym:
            continue
        if tickers_filter and sym not in tickers_filter:
            continue
        events.append({
            "ticker":     sym,
            "type":       "Earnings",
            "date":       ev["date"],
            "time":       ev.get("time", ""),
            "detail":     f"EPS est. {ev['estimate']}" if ev.get("estimate") else "Earnings report",
            "ts":         ev["ts"],
            "mcap_val":   ev.get("mcap_val"),
        })
    return events


def fetch_analyst_events(tickers: List[str], days: int = 7) -> List[Dict]:
    """
    Pull recent analyst upgrades/downgrades from Finnhub.
    Returns events only for tickers with an upgrade in the last `days` days.
    """
    api_key = os.getenv("FINNHUB_API_KEY", "")
    if not api_key:
        logger.debug("FINNHUB_API_KEY not set — skipping analyst events")
        return []

    cutoff = datetime.now() - timedelta(days=days)
    events = []

    for ticker in tickers:
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/stock/upgrade-downgrade",
                params={"symbol": ticker, "token": api_key},
                timeout=5,
            )
            if r.status_code != 200:
                continue
            rows = r.json() or []
            for row in rows:
                try:
                    dt = datetime.strptime(row.get("gradeDate", ""), "%Y-%m-%d")
                except Exception:
                    continue
                if dt < cutoff:
                    continue
                action = row.get("action", "").lower()
                # Only include upgrades and initiations (bullish signals)
                if action not in ("upgrade", "init", "reiterated"):
                    continue
                from_grade = row.get("fromGrade", "")
                to_grade   = row.get("toGrade", "")
                firm       = row.get("company", "")
                detail     = f"{firm}: {from_grade} → {to_grade}" if from_grade else f"{firm}: {to_grade}"
                events.append({
                    "ticker":   ticker,
                    "type":     "Analyst Upgrade",
                    "date":     dt.strftime("%a %b %d"),
                    "time":     "",
                    "detail":   detail,
                    "ts":       dt.timestamp(),
                    "mcap_val": None,
                })
                break   # one event per ticker is enough
        except Exception as e:
            logger.debug(f"analyst_events({ticker}): {e}")

    return events


def fetch_sec_8k_events(tickers: List[str], days: int = 7) -> List[Dict]:
    """
    Check SEC EDGAR for recent 8-K filings (material events) for each ticker.
    Uses the free EDGAR full-text search API — requests run in parallel (8 workers).
    Only returns tickers that filed an 8-K in the last `days` days.
    """
    email   = os.getenv("SEC_USER_AGENT_EMAIL", "agent@example.com")
    headers = {"User-Agent": f"FinancialAgent ({email})"}
    cutoff  = datetime.now() - timedelta(days=days)
    start   = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    end     = datetime.now().strftime("%Y-%m-%d")

    def _fetch_one(ticker: str) -> Optional[Dict]:
        try:
            r = requests.get(
                f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
                f"&dateRange=custom&startdt={start}&enddt={end}&forms=8-K",
                headers=headers,
                timeout=8,
            )
            if r.status_code != 200:
                return None
            hits = r.json().get("hits", {}).get("hits", [])
            for hit in hits[:5]:
                src             = hit.get("_source", {})
                tickers_filed   = [t.upper() for t in src.get("entity_name", "").split(",")]
                filing_date_str = src.get("file_date", "")
                try:
                    filing_dt = datetime.strptime(filing_date_str, "%Y-%m-%d")
                except Exception:
                    continue
                if filing_dt < cutoff:
                    continue
                form_type     = src.get("form_type", "8-K")
                display_names = src.get("display_names", [""])[0]
                if ticker.upper() in display_names.upper() or ticker.upper() in str(tickers_filed).upper():
                    return {
                        "ticker":   ticker,
                        "type":     "SEC 8-K",
                        "date":     filing_dt.strftime("%a %b %d"),
                        "time":     "",
                        "detail":   f"{form_type} filing — material event",
                        "ts":       filing_dt.timestamp(),
                        "mcap_val": None,
                    }
        except Exception as e:
            logger.debug(f"sec_8k_events({ticker}): {e}")
        return None

    events: List[Dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for ev in pool.map(_fetch_one, tickers):
            if ev:
                events.append(ev)
    return events


# ── Ticker source resolvers ───────────────────────────────────────────────────

def resolve_tickers(
    source: str,
    manual_tickers: Optional[List[str]] = None,
    index_name: Optional[str] = None,
    sector: Optional[str] = None,
    max_index_stocks: int = 100,
) -> Optional[List[str]]:
    """
    Resolve the ticker list based on the chosen source.
    Returns None for 'calendar' mode (tickers come from the earnings calendar itself).
    """
    if source == "calendar":
        return None

    if source == "manual":
        return [t.strip().upper() for t in (manual_tickers or []) if t.strip()]

    if source == "watchlist":
        try:
            from src.database import watchlist_get_all, portfolio_get_all
            wl = [r["ticker"] for r in watchlist_get_all()]
            pt = [r["ticker"] for r in portfolio_get_all()]
            return list(dict.fromkeys(wl + pt))
        except Exception as e:
            logger.warning(f"resolve_tickers watchlist: {e}")
            return []

    if source == "index":
        try:
            from src.index_loader import get_tickers_by_sector
            return get_tickers_by_sector(index_name or "iShares Russell 2000", sector or "All", max_index_stocks)
        except Exception as e:
            logger.warning(f"resolve_tickers index: {e}")
            return []

    return None


# ── Main scanner ──────────────────────────────────────────────────────────────

def scan_catalysts(
    days_ahead: int = 7,
    max_market_cap_b: float = 10.0,
    min_si_pct: float = 0.0,
    min_explosion_score: float = 15.0,
    check_insider: bool = False,
    catalyst_types: Optional[List[str]] = None,
    tickers: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    phase_cb: Optional[Callable[[str], None]] = None,
    watchlist_mode: bool = False,
) -> List[Dict]:
    """
    Find stocks with upcoming catalysts and high explosion potential.

    Args:
        days_ahead:           Calendar days to look ahead (for earnings and analyst/8-K recency)
        max_market_cap_b:     Filter out companies above this market cap ($B)
        min_si_pct:           Minimum short interest % of float
        min_explosion_score:  Drop candidates below this score
        check_insider:        Query InsiderTracker (slow without SEC_API_KEY)
        catalyst_types:       List of types to scan: ["earnings", "analyst", "sec_8k"]
        tickers:              Explicit ticker list. None = use earnings calendar.
        progress_cb:          Optional callback(current, total, ticker)

    Returns:
        List of candidate dicts sorted by explosion_score descending.
    """
    if catalyst_types is None:
        catalyst_types = ["earnings"]
    catalyst_types = [c.lower() for c in catalyst_types]

    # ── Build event list ──────────────────────────────────────────────────────
    def _phase(msg: str) -> None:
        if phase_cb:
            try:
                phase_cb(msg)
            except Exception:
                pass

    events_by_ticker: Dict[str, Dict] = {}

    if "earnings" in catalyst_types:
        _phase("מאתר דיווחי earnings מ-Nasdaq…")
        tickers_filter = set(tickers) if tickers else None
        for ev in fetch_earnings_events(days_ahead, tickers_filter):
            sym = ev["ticker"]
            # Keep earliest / highest-priority event per ticker
            if sym not in events_by_ticker:
                events_by_ticker[sym] = ev

    if tickers and ("analyst" in catalyst_types):
        _phase(f"מאתר שדרוגי אנליסטים ({len(tickers)} מניות)…")
        for ev in fetch_analyst_events(tickers, days=days_ahead):
            sym = ev["ticker"]
            if sym not in events_by_ticker:
                events_by_ticker[sym] = ev

    if tickers and ("sec_8k" in catalyst_types):
        _phase(f"סורק הגשות SEC 8-K ({len(tickers)} מניות במקביל)…")
        for ev in fetch_sec_8k_events(tickers, days=days_ahead):
            sym = ev["ticker"]
            if sym not in events_by_ticker:
                events_by_ticker[sym] = ev

    if "pdufa" in catalyst_types:
        _phase("מאתר תאריכי PDUFA/AdCom מ-BioPharma Catalyst…")
        tickers_filter_pdufa = set(tickers) if tickers else None
        for ev in fetch_pdufa_events(days_ahead, tickers_filter_pdufa):
            sym = ev["ticker"]
            if sym not in events_by_ticker:
                events_by_ticker[sym] = ev

    # If ticker list provided but no earnings found, still include all tickers
    # for analyst/8-K modes — they may have no upcoming earnings but still have events
    # Only add placeholders in watchlist mode — shows all watchlist tickers even
    # without a dated catalyst. For Index/Sector or Manual scans, drop no-catalyst tickers.
    if watchlist_mode and tickers:
        for t in tickers:
            if t not in events_by_ticker:
                events_by_ticker[t] = {
                    "ticker":   t,
                    "type":     "Watchlist",
                    "date":     "—",
                    "time":     "",
                    "detail":   "ללא קטליזטור קרוב",
                    "ts":       datetime.now().timestamp(),
                    "mcap_val": None,
                }

    if not events_by_ticker:
        logger.warning("catalyst_scanner: no events found")
        return []

    # ── Pre-filter by calendar market cap ─────────────────────────────────────
    filtered_events = {}
    for sym, ev in events_by_ticker.items():
        mcap_val = ev.get("mcap_val")
        if mcap_val and mcap_val > max_market_cap_b * 1e9:
            continue
        filtered_events[sym] = ev

    candidates: List[Dict] = []
    total = len(filtered_events)

    for i, (ticker, ev) in enumerate(filtered_events.items()):
        if progress_cb:
            try:
                progress_cb(i + 1, total, ticker)
            except Exception:
                pass

        data = _fetch_ticker_data(ticker)
        if data is None:
            continue

        # Market cap filter (yfinance authoritative)
        mcap = data["market_cap"]
        if mcap and mcap > max_market_cap_b * 1e9:
            continue

        si_pct = data["si_pct"]
        if si_pct < min_si_pct:
            continue

        # Days to event
        try:
            event_dt      = datetime.fromtimestamp(ev["ts"])
            days_to_event = max(0, (event_dt.date() - datetime.now().date()).days)
        except Exception:
            days_to_event = 1

        insider_detail = _get_insider_signal(ticker) if check_insider else None
        has_insider    = bool(insider_detail and insider_detail["net"] > 0)

        try:
            _news_items   = fetch_yfinance_news(ticker, limit=8)
            _headlines    = [n.get("headline", "") for n in _news_items]
            news_catalyst = detect_news_catalyst(_headlines)
            if news_catalyst:
                logger.debug(f"[catalyst] {ticker} news catalysts: {news_catalyst}")
        except Exception as e:
            logger.warning(f"[catalyst] {ticker} news fetch failed: {e}")
            news_catalyst = []

        opt_pts, has_unusual_calls = _unusual_options_pts(ticker)

        score = explosion_score(
            days_to_event=days_to_event,
            si_pct=si_pct,
            float_m=data["float_m"],
            vol_ratio=data["vol_ratio"],
            has_insider=has_insider,
            pct_5d=data["pct_5d"],
            unusual_options_pts=opt_pts,
        )

        if score < min_explosion_score:
            continue

        mcap_disp  = "N/A"
        if mcap:
            mcap_disp = f"${mcap/1e9:.1f}B" if mcap >= 1e9 else f"${mcap/1e6:.0f}M"
        float_disp = f"{data['float_m']:.1f}M" if data["float_m"] else "N/A"

        label, color = score_label(score)

        candidates.append({
            "ticker":          ticker,
            "name":            data["name"],
            "sector":          data["sector"],
            # Catalyst info
            "catalyst":        ev["type"],
            "catalyst_date":   ev["date"],
            "catalyst_time":   ev.get("time", ""),
            "catalyst_detail": ev.get("detail", ""),
            "days_to_event":   days_to_event,
            # Price / size
            "price":           data["price"],
            "market_cap":      mcap,
            "market_cap_disp": mcap_disp,
            "float_m":         data["float_m"],
            "float_disp":      float_disp,
            # Squeeze fuel
            "si_pct":          round(si_pct, 1),
            "vol_ratio":       round(data["vol_ratio"], 2),
            "pct_5d":          data["pct_5d"],
            "has_insider":     has_insider,
            "insider_detail":  insider_detail,
            # Technical snapshot
            "rsi":             data["rsi"],
            "rsi_signal":      data["rsi_signal"],
            "macd":            data["macd"],
            "ma_trend":        data["ma_trend"],
            # Score
            "explosion_score":    score,
            "label":              label,
            "label_color":        color,
            # News catalyst
            "news_catalyst":      news_catalyst,
            # Unusual options
            "unusual_options_pts":    opt_pts,
            "has_unusual_calls":      has_unusual_calls,
        })

    candidates.sort(key=lambda x: x["explosion_score"], reverse=True)
    logger.info(f"catalyst_scanner: {len(candidates)} candidates from {total} events")
    return candidates
