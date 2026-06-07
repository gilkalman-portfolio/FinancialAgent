"""
Market Feed - Market indices, futures, breaking news, earnings calendar, macro events.
"""

import os, requests
from datetime import datetime, timedelta
from typing import List, Dict
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
AV_KEY      = os.getenv("ALPHA_VANTAGE_API_KEY", "")

SENTIMENT_COLOR = {
    "Bullish":          "#16a34a",
    "Somewhat-Bullish": "#4ade80",
    "Neutral":          "#6b7280",
    "Somewhat-Bearish": "#f97316",
    "Bearish":          "#dc2626",
}

# ── Market Indices ─────────────────────────────────────────────────────────────

MARKET_SYMBOLS = [
    ("S&P 500",      "^GSPC",    "index"),
    ("Nasdaq 100",   "^NDX",     "index"),
    ("Dow Jones",    "^DJI",     "index"),
    ("Russell 2000", "^RUT",     "index"),
    ("VIX",          "^VIX",     "vix"),
    ("Oil WTI",      "CL=F",     "commodity"),
    ("Gold",         "GC=F",     "commodity"),
    ("10Y Bond",     "^TNX",     "bond"),
    ("EUR/USD",      "EURUSD=X", "fx"),
    ("Bitcoin",      "BTC-USD",  "crypto"),
]

FUTURES_SYMBOLS = [
    ("S&P Futures",     "ES=F"),
    ("Nasdaq Futures",  "NQ=F"),
    ("Dow Futures",     "YM=F"),
    ("Gold Futures",    "GC=F"),
    ("Oil Futures",     "CL=F"),
    ("VIX Futures",     "VXc1"),
]


def _get_cboe_quote(sym: str) -> dict | None:
    """Real-time quote from CBOE for indices (free, no API key)."""
    cboe_map = {
        "^VIX":  "_VIX",
        "^GSPC": "_SPX",
        "^NDX":  "_NDX",
        "^DJI":  "_DJI",
        "^RUT":  "_RUT",
    }
    cboe_sym = cboe_map.get(sym)
    if not cboe_sym:
        return None
    try:
        r = requests.get(
            f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/{cboe_sym}.json",
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            d = r.json().get("data", {})
            price = d.get("current_price")
            prev  = d.get("prev_day_close")
            chg_pct = d.get("price_change_percent", 0) or 0
            if price and price > 0:
                return {"price": float(price), "prev": float(prev) if prev else float(price), "chg_pct": float(chg_pct)}
    except Exception:
        pass
    return None


def _scrape_investing(url: str) -> dict | None:
    """Scrape price/change from investing.com via subprocess (avoids event loop conflict with Streamlit)."""
    import subprocess, sys, json, re
    script = f"""
import re, json
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    page.goto("{url}", wait_until="domcontentloaded", timeout=15000)
    page.wait_for_timeout(2000)
    price_el  = page.query_selector('[data-test="instrument-price-last"]')
    change_el = page.query_selector('[data-test="instrument-price-change-percent"]')
    price_txt  = price_el.inner_text()  if price_el  else ""
    change_txt = change_el.inner_text() if change_el else ""
    browser.close()
    if price_txt:
        price = float(price_txt.replace(",", ""))
        m = re.search(r'([+-]?\\d+\\.?\\d*)', change_txt)
        chg = float(m.group(1)) if m else 0.0
        print(json.dumps({{"price": price, "change": chg, "up": chg >= 0}}))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=20
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception as e:
        logger.debug(f"Playwright scrape {url}: {e}")
    return None


def get_market_indices() -> List[Dict]:
    CBOE_SYMS = {"^VIX", "^GSPC", "^NDX", "^DJI", "^RUT"}
    INVESTING_URLS = {
        "VXc1": "https://www.investing.com/indices/us-spx-vix-futures",
    }
    try:
        import yfinance as yf
        results = []
        for name, sym, cat in MARKET_SYMBOLS:
            try:
                if sym in INVESTING_URLS:
                    data = _scrape_investing(INVESTING_URLS[sym])
                    if data:
                        results.append({"name": name, "symbol": sym, "category": cat, **data})
                        continue
                elif sym in CBOE_SYMS:
                    fh = _get_cboe_quote(sym)
                    if fh:
                        price = fh["price"]
                        chg   = fh.get("chg_pct") or ((price - fh["prev"]) / fh["prev"] * 100 if fh["prev"] else 0)
                        results.append({"name": name, "symbol": sym, "category": cat,
                                        "price": price, "change": chg, "up": chg >= 0})
                        continue
                # yfinance fallback
                t = yf.Ticker(sym)
                h_intra = t.history(period="1d", interval="5m")
                h_daily = t.history(period="10d", interval="1d")
                if h_daily.empty:
                    continue
                daily_closes = h_daily["Close"].dropna()
                if len(daily_closes) < 2:
                    continue
                if not h_intra.empty:
                    price = float(h_intra["Close"].dropna().iloc[-1])
                    prev  = float(daily_closes.iloc[-2])
                else:
                    price = float(daily_closes.iloc[-1])
                    prev  = float(daily_closes.iloc[-2])
                chg = (price - prev) / prev * 100
                results.append({"name": name, "symbol": sym, "category": cat,
                                "price": price, "change": chg, "up": chg >= 0})
            except Exception:
                continue
        return results
    except Exception as e:
        logger.warning(f"Market indices failed: {e}")
        return []


def get_futures() -> List[Dict]:
    """Returns pre-market / after-hours futures data."""
    INVESTING_URLS = {
        "VXc1": "https://www.investing.com/indices/us-spx-vix-futures",
    }
    try:
        import yfinance as yf
        results = []
        for name, sym in FUTURES_SYMBOLS:
            try:
                if sym in INVESTING_URLS:
                    data = _scrape_investing(INVESTING_URLS[sym])
                    if data:
                        results.append({"name": name, "symbol": sym, **data})
                        continue
                t = yf.Ticker(sym)
                h_intra = t.history(period="1d", interval="5m")
                h_daily = t.history(period="10d", interval="1d")
                if h_daily.empty:
                    continue
                daily_closes = h_daily["Close"].dropna()
                if len(daily_closes) < 2:
                    continue
                if not h_intra.empty:
                    price = float(h_intra["Close"].dropna().iloc[-1])
                    prev  = float(daily_closes.iloc[-2])
                else:
                    price = float(daily_closes.iloc[-1])
                    prev  = float(daily_closes.iloc[-2])
                chg = (price - prev) / prev * 100
                results.append({"name": name, "symbol": sym,
                                "price": price, "change": chg, "up": chg >= 0})
            except Exception:
                continue
        return results
    except Exception as e:
        logger.warning(f"Futures fetch failed: {e}")
        return []


def get_vix_level(vix_price: float) -> Dict:
    """Classify VIX into a named level with color and description."""
    if vix_price < 15:
        return {"level": "Calm",    "color": "#16a34a", "desc": "Low volatility — complacency zone"}
    if vix_price < 20:
        return {"level": "Normal",  "color": "#4ade80", "desc": "Normal market conditions"}
    if vix_price < 30:
        return {"level": "Caution", "color": "#d97706", "desc": "Elevated volatility — stay alert"}
    if vix_price < 40:
        return {"level": "Fear",    "color": "#dc2626", "desc": "High fear — potential opportunity"}
    return     {"level": "Panic",   "color": "#7c3aed", "desc": "Extreme panic — rare event"}


# ── Sentiment helper ───────────────────────────────────────────────────────────

def _keyword_sentiment(headline: str) -> str:
    """Delegate to centralized news_fetcher sentiment."""
    from src.news_fetcher import keyword_sentiment
    return keyword_sentiment(headline)["label"]

# ── Breaking News ──────────────────────────────────────────────────────────────

def get_market_news(limit: int = 40) -> List[Dict]:
    """Delegate to centralized news_fetcher."""
    from src.news_fetcher import get_market_news as _fetch
    articles = _fetch(limit=limit)
    # Normalize to market_feed format (adds 'color' field)
    for a in articles:
        label = a.get("label", a.get("sentiment", "Neutral"))
        a["color"] = SENTIMENT_COLOR.get(label, SENTIMENT_COLOR.get(a.get("sentiment", ""), "#6b7280"))
        a["tickers"] = a.get("tickers", [])
        a["sentiment"] = label   # normalize to label: page_market.py filter/mood use this field
    return articles


def get_market_mood(articles: List[Dict]) -> Dict:
    if not articles:
        return {"label": "Neutral", "bullish": 0, "bearish": 0, "neutral": 0, "score": 50}
    bullish = sum(1 for a in articles if "Bullish" in a["sentiment"])
    bearish = sum(1 for a in articles if "Bearish" in a["sentiment"])
    neutral = len(articles) - bullish - bearish
    total   = len(articles)
    score   = int((bullish / total) * 100)
    if score >= 60:   label = "Bullish"
    elif score >= 45: label = "Neutral"
    else:             label = "Bearish"
    return {"label": label, "bullish": bullish, "bearish": bearish, "neutral": neutral, "score": score}


# ── Economic Calendar ──────────────────────────────────────────────────────────

MACRO_SCHEDULE = [
    (1, 12, "CPI Release *",        "high"),
    (3, 18, "Fed Interest Rate *",   "high"),
    (4, 12, "NFP / Jobs Report *",   "high"),
    (2, 14, "FOMC Minutes *",        "high"),
    (0, 14, "ISM Manufacturing *",   "medium"),
    (3, 14, "GDP Estimate *",        "medium"),
    (1, 14, "PPI Release *",         "medium"),
    (2, 14, "Retail Sales *",        "medium"),
    (3, 12, "Jobless Claims *",      "medium"),
    (4, 14, "Consumer Sentiment *",  "medium"),
]

def get_upcoming_macro(days_ahead: int = 7) -> List[Dict]:
    events = []
    now = datetime.utcnow()
    for day_offset in range(days_ahead + 1):
        dt = now + timedelta(days=day_offset)
        wd = dt.weekday()
        for (weekday, hour, name, impact) in MACRO_SCHEDULE:
            if weekday == wd:
                event_dt = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
                if event_dt > now:
                    events.append({
                        "name":   name,
                        "date":   event_dt.strftime("%a %b %d"),
                        "time":   event_dt.strftime("%H:%M UTC"),
                        "impact": impact,
                        "color":  "#dc2626" if impact == "high" else "#d97706",
                        "ts":     event_dt.timestamp(),
                    })
    events.sort(key=lambda x: x["ts"])
    events.append({
        "name":   "* approximate — verify dates",
        "date":   "",
        "time":   "",
        "impact": "info",
    })
    return events


def get_earnings_calendar(days_ahead: int = 7) -> List[Dict]:
    events = []
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept":     "application/json",
        "Origin":     "https://www.nasdaq.com",
        "Referer":    "https://www.nasdaq.com/",
    }
    for day_offset in range(days_ahead + 1):
        dt = datetime.now() + timedelta(days=day_offset)
        if dt.weekday() >= 5:
            continue
        date_str = dt.strftime("%Y-%m-%d")
        try:
            r = requests.get(
                f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}",
                headers=headers, timeout=8
            )
            if r.status_code != 200:
                continue
            rows = r.json().get("data", {}).get("rows") or []
            for row in rows:
                time_label   = row.get("time", "")
                time_display = "Pre-market" if "pre" in time_label else "After-hours" if "after" in time_label else "During"
                eps  = row.get("epsForecast", "")
                mcap = row.get("marketCap", "")
                try:
                    mcap_val     = int(mcap.replace("$","").replace(",",""))
                    mcap_display = f"${mcap_val/1e9:.1f}B" if mcap_val >= 1e9 else f"${mcap_val/1e6:.0f}M"
                except Exception:
                    mcap_display = ""
                events.append({
                    "symbol":   row.get("symbol", ""),
                    "name":     row.get("name", ""),
                    "date":     dt.strftime("%a %b %d"),
                    "time":     time_display,
                    "estimate": eps,
                    "mcap":     mcap_display,
                    "mcap_val": mcap_val if mcap else None,
                    "ts":       dt.timestamp(),
                })
        except Exception as e:
            logger.warning(f"Nasdaq earnings {date_str}: {e}")
    events.sort(key=lambda x: (x["ts"], -1 if "Pre" in x["time"] else 1))
    return events
