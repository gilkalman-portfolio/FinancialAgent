"""
News Fetcher — מודול מרכזי לשליפת חדשות
========================================
מקור יחיד לכל החדשות באפליקציה. כל מודול אחר יקרא מכאן בלבד.

מקורות (לפי עדיפות):
  1. Google News RSS  — ללא API key, real-time, כיסוי מלא
  2. yfinance         — ללא API key, ticker-specific, delay ~15-30 דק'
  3. Finnhub          — API key, 60 req/min, איכות טובה
  4. Alpha Vantage    — API key, 25 req/day (!), כולל sentiment מחושב
  5. Marketaux        — API key (חינמי, 100 req/day), כולל sentiment

שימוש:
    from src.news_fetcher import get_ticker_news, get_market_news
"""

import os
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

FINNHUB_KEY   = os.getenv("FINNHUB_API_KEY", "")
AV_KEY        = os.getenv("ALPHA_VANTAGE_API_KEY", "")
MARKETAUX_KEY = os.getenv("MARKETAUX_API_KEY", "")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SENTIMENT_COLOR = {
    "Bullish":          "#16a34a",
    "Somewhat-Bullish": "#4ade80",
    "Neutral":          "#6b7280",
    "Somewhat-Bearish": "#f97316",
    "Bearish":          "#dc2626",
    "positive":         "#16a34a",
    "negative":         "#dc2626",
    "neutral":          "#6b7280",
}

# ── Sentiment keywords (shared across app) ─────────────────────────────────────

POSITIVE_KW = {
    "surge": 3, "soar": 3, "jump": 3, "rally": 3, "beat": 3, "record": 3,
    "breakout": 3, "strong": 2, "gain": 2, "upgrade": 2, "bullish": 2,
    "growth": 2, "profit": 2, "rise": 1, "up": 1, "high": 1, "exceed": 1,
    "outperform": 2, "boost": 2, "recovery": 2, "positive": 1,
}

NEGATIVE_KW = {
    "plunge": 3, "crash": 3, "miss": 3, "loss": 3, "bankrupt": 3, "fraud": 3,
    "collapse": 3, "recession": 3, "wipe": 2, "decline": 2, "fall": 2,
    "drop": 2, "downgrade": 2, "bearish": 2, "risk": 2, "layoff": 2,
    "default": 2, "warn": 2,
    "down": 1, "low": 1, "weak": 1, "concern": 1, "below": 1, "cut": 1,
}


def keyword_sentiment(text: str) -> Dict:
    """Keyword-based sentiment. Returns sentiment, score (-1..1), confidence."""
    t = text.lower()
    pos = sum(w for kw, w in POSITIVE_KW.items() if kw in t)
    neg = sum(w for kw, w in NEGATIVE_KW.items() if kw in t)
    total = pos + neg
    if total == 0:
        return {"sentiment": "neutral", "label": "Neutral", "score": 0.0, "confidence": "low", "raw": 0}
    score = (pos - neg) / total
    confidence = "high" if total >= 6 else "medium" if total >= 3 else "low"
    if score >= 0.3:    sentiment, label = "positive", "Bullish"
    elif score <= -0.3: sentiment, label = "negative", "Bearish"
    else:               sentiment, label = "neutral",  "Neutral"
    return {"sentiment": sentiment, "label": label, "score": round(score, 2),
            "confidence": confidence, "raw": pos - neg}


def _parse_ts(pub_str: str) -> float:
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S%z", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(pub_str.strip(), fmt).timestamp()
        except Exception:
            pass
    return 0.0


# ── Source 1: Google News RSS ──────────────────────────────────────────────────

def fetch_google_news_rss(query: str, limit: int = 20) -> List[Dict]:
    """ללא API key, real-time, כיסוי מלא."""
    url = (
        f"https://news.google.com/rss/search"
        f"?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        r = requests.get(url, headers=_HEADERS, timeout=8)
        if r.status_code != 200:
            logger.debug(f"[NewsFetcher] Google RSS '{query}': status {r.status_code}")
            return []
        root = ET.fromstring(r.content)
        results = []
        for item in root.findall(".//item")[:limit]:
            title  = item.findtext("title", "").strip()
            link   = item.findtext("link", "").strip()
            pub    = item.findtext("pubDate", "").strip()
            src_el = item.find("source")
            source = src_el.text if src_el is not None else ""
            if not title:
                continue
            ts = _parse_ts(pub)
            s  = keyword_sentiment(title)
            results.append({
                "headline":   title,
                "url":        link,
                "source":     source,
                "published":  datetime.fromtimestamp(ts).strftime("%b %d %H:%M") if ts else pub[:16],
                "ts":         ts,
                "sentiment":  s["sentiment"],
                "label":      s["label"],
                "score":      s["score"],
                "confidence": s["confidence"],
                "origin":     "google_rss",
            })
        logger.debug(f"[NewsFetcher] Google RSS '{query}': {len(results)} articles")
        return results
    except Exception as e:
        logger.warning(f"[NewsFetcher] Google RSS '{query}' failed: {e}")
        return []


# ── Source 2: yfinance ─────────────────────────────────────────────────────────

def fetch_yfinance_news(ticker: str, limit: int = 20) -> List[Dict]:
    try:
        import yfinance as yf
        items = yf.Ticker(ticker).news or []
        results = []
        for a in items[:limit]:
            content = a.get("content", {})
            title   = content.get("title", "") or a.get("title", "")
            summary = content.get("summary", "") or ""
            url     = (content.get("canonicalUrl", {}) or {}).get("url", "") or a.get("link", "")
            source  = (content.get("provider", {}) or {}).get("displayName", "") or a.get("publisher", "")
            pub_raw = content.get("pubDate", "") or ""
            ts      = _parse_ts(pub_raw) if pub_raw else 0.0
            if not title:
                continue
            s = keyword_sentiment(f"{title} {summary}")
            results.append({
                "headline":   title,
                "url":        url,
                "source":     source,
                "published":  datetime.fromtimestamp(ts).strftime("%b %d %H:%M") if ts else "",
                "ts":         ts,
                "sentiment":  s["sentiment"],
                "label":      s["label"],
                "score":      s["score"],
                "confidence": s["confidence"],
                "origin":     "yfinance",
            })
        logger.debug(f"[NewsFetcher] yfinance '{ticker}': {len(results)} articles")
        return results
    except Exception as e:
        logger.debug(f"[NewsFetcher] yfinance '{ticker}' failed: {e}")
        return []


# ── Source 3: Finnhub ──────────────────────────────────────────────────────────

def fetch_finnhub_news(ticker: str = "", days: int = 7, limit: int = 20) -> List[Dict]:
    if not FINNHUB_KEY:
        return []
    try:
        today   = datetime.now().strftime("%Y-%m-%d")
        from_dt = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        if ticker:
            url = (f"https://finnhub.io/api/v1/company-news"
                   f"?symbol={ticker}&from={from_dt}&to={today}&token={FINNHUB_KEY}")
        else:
            url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=6)
        if r.status_code != 200:
            return []
        results = []
        for a in r.json()[:limit]:
            ts    = float(a.get("datetime", 0) or 0)
            title = a.get("headline", "") or a.get("title", "")
            s = keyword_sentiment(title)
            results.append({
                "headline":   title,
                "url":        a.get("url", ""),
                "source":     a.get("source", ""),
                "published":  datetime.fromtimestamp(ts).strftime("%b %d %H:%M") if ts else "",
                "ts":         ts,
                "sentiment":  s["sentiment"],
                "label":      s["label"],
                "score":      s["score"],
                "confidence": s["confidence"],
                "origin":     "finnhub",
            })
        logger.debug(f"[NewsFetcher] Finnhub '{ticker or 'general'}': {len(results)} articles")
        return results
    except Exception as e:
        logger.debug(f"[NewsFetcher] Finnhub '{ticker}' failed: {e}")
        return []


# ── Source 4: Alpha Vantage (25 req/day — use sparingly) ──────────────────────

def fetch_alpha_vantage_news(
    ticker: str = "", topics: str = "", days: int = 7, limit: int = 20,
) -> List[Dict]:
    if not AV_KEY:
        return []
    try:
        params: Dict = {"function": "NEWS_SENTIMENT", "limit": limit, "apikey": AV_KEY, "sort": "LATEST"}
        if ticker:  params["tickers"]   = ticker
        if topics:  params["topics"]    = topics
        if days:    params["time_from"] = (datetime.now() - timedelta(days=days)).strftime("%Y%m%dT0000")
        r = requests.get("https://www.alphavantage.co/query", params=params, timeout=8)
        if r.status_code != 200:
            return []
        results = []
        for a in r.json().get("feed", []):
            pub_raw  = a.get("time_published", "")
            ts       = _parse_ts(pub_raw)
            av_label = a.get("overall_sentiment_label", "")
            av_score = float(a.get("overall_sentiment_score", 0) or 0)
            if "Bullish" in av_label:   sentiment, label = "positive", av_label
            elif "Bearish" in av_label: sentiment, label = "negative", av_label
            else:                       sentiment, label = "neutral",  "Neutral"
            results.append({
                "headline":   a.get("title", ""),
                "url":        a.get("url", ""),
                "source":     a.get("source", ""),
                "published":  datetime.fromtimestamp(ts).strftime("%b %d %H:%M") if ts else "",
                "ts":         ts,
                "sentiment":  sentiment,
                "label":      label,
                "score":      av_score if sentiment == "positive" else -abs(av_score),
                "confidence": "high",
                "tickers":    [t["ticker"] for t in a.get("ticker_sentiment", [])[:3]],
                "origin":     "alpha_vantage",
            })
        logger.debug(f"[NewsFetcher] AlphaVantage '{ticker or topics}': {len(results)} articles")
        return results
    except Exception as e:
        logger.debug(f"[NewsFetcher] AlphaVantage failed: {e}")
        return []


# ── Source 5: Marketaux (100 req/day free) ────────────────────────────────────

def fetch_marketaux_news(ticker: str = "", limit: int = 10) -> List[Dict]:
    """Free 100 req/day. Register: https://www.marketaux.com/"""
    if not MARKETAUX_KEY:
        return []
    try:
        params: Dict = {"api_token": MARKETAUX_KEY, "language": "en",
                        "limit": limit, "sort": "published_at"}
        if ticker:
            params["symbols"] = ticker
        r = requests.get("https://api.marketaux.com/v1/news/all", params=params, timeout=8)
        if r.status_code != 200:
            logger.debug(f"[NewsFetcher] Marketaux status {r.status_code}")
            return []
        results = []
        for a in r.json().get("data", []):
            pub_raw  = a.get("published_at", "")
            ts       = _parse_ts(pub_raw) if pub_raw else 0.0
            entities = a.get("entities", [])
            ticker_sentiments = {
                e.get("symbol", ""): float(e.get("sentiment_score", 0) or 0)
                for e in entities if e.get("symbol")
            }
            if ticker and ticker in ticker_sentiments:
                raw = ticker_sentiments[ticker]
                if raw > 0.15:    sentiment, label = "positive", "Bullish"
                elif raw < -0.15: sentiment, label = "negative", "Bearish"
                else:             sentiment, label = "neutral",  "Neutral"
                score = raw
            else:
                s = keyword_sentiment(a.get("title", ""))
                sentiment, label, score = s["sentiment"], s["label"], s["score"]
            results.append({
                "headline":          a.get("title", ""),
                "url":               a.get("url", ""),
                "source":            a.get("source", ""),
                "published":         datetime.fromtimestamp(ts).strftime("%b %d %H:%M") if ts else "",
                "ts":                ts,
                "sentiment":         sentiment,
                "label":             label,
                "score":             score,
                "confidence":        "high" if ticker in ticker_sentiments else "medium",
                "ticker_sentiments": ticker_sentiments,
                "origin":            "marketaux",
            })
        logger.debug(f"[NewsFetcher] Marketaux '{ticker}': {len(results)} articles")
        return results
    except Exception as e:
        logger.debug(f"[NewsFetcher] Marketaux failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED: get_ticker_news
# ══════════════════════════════════════════════════════════════════════════════

def get_ticker_news(ticker: str, days: int = 7, limit: int = 30) -> List[Dict]:
    """
    חדשות מאוחדות לטיקר מכל המקורות. Dedup + sort by sentiment strength.
    """
    sources = []
    sources.extend(fetch_google_news_rss(f"{ticker} stock", limit=15))
    sources.extend(fetch_yfinance_news(ticker, limit=15))
    sources.extend(fetch_finnhub_news(ticker, days=days, limit=15))
    if MARKETAUX_KEY:
        sources.extend(fetch_marketaux_news(ticker, limit=10))
    if len(sources) < 5 and AV_KEY:
        sources.extend(fetch_alpha_vantage_news(ticker=ticker, days=days, limit=10))

    seen: set = set()
    merged = []
    for a in sources:
        key = a.get("headline", "")[:60].lower().strip()
        if key and key not in seen:
            seen.add(key)
            merged.append(a)

    cutoff = (datetime.now() - timedelta(days=days)).timestamp()
    # ts=0 means unknown timestamp — keep only if days > 1 (lenient), drop for tight windows
    merged = [a for a in merged if a.get("ts", 0) >= cutoff or (a.get("ts", 0) == 0 and days > 1)]
    merged.sort(key=lambda x: (abs(x.get("score", 0)), x.get("ts", 0)), reverse=True)
    logger.info(f"[NewsFetcher] get_ticker_news('{ticker}'): {len(merged)} (raw={len(sources)})")
    return merged[:limit]


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED: get_market_news
# ══════════════════════════════════════════════════════════════════════════════

def get_market_news(limit: int = 40) -> List[Dict]:
    """חדשות שוק כלליות — לדף Market ו-Digest."""
    sources = []
    for query in ["stock market", "S&P 500", "Federal Reserve", "earnings"]:
        sources.extend(fetch_google_news_rss(query, limit=10))
    if AV_KEY:
        sources.extend(fetch_alpha_vantage_news(
            topics="earnings,ipo,mergers_and_acquisitions,financial_markets,economy_macro",
            limit=20,
        ))
    if len(sources) < 10:
        sources.extend(fetch_finnhub_news(limit=limit))

    seen: set = set()
    merged = []
    for a in sources:
        key = a.get("headline", "")[:60].lower().strip()
        if key and key not in seen:
            seen.add(key)
            merged.append(a)

    merged.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return merged[:limit]


# ══════════════════════════════════════════════════════════════════════════════
# CATALYST FILTER — fast pre-filter before LLM (used by news_catalyst_monitor)
# ══════════════════════════════════════════════════════════════════════════════

HIGH_IMPACT_KEYWORDS = {
    # Negative
    "downgrade": 3, "miss": 3, "loss": 3, "bankrupt": 3, "fraud": 3,
    "recall": 3, "lawsuit": 3, "investigation": 3, "sec ": 3, "layoff": 3,
    "guidance cut": 3, "revenue miss": 3, "earnings miss": 3, "short seller": 3,
    "plunge": 3, "crash": 3, "halt": 3, "delisted": 3,
    # Positive
    "upgrade": 3, "beat": 3, "record": 3, "breakthrough": 3, "approval": 3,
    "fda": 3, "acquisition": 3, "merger": 3, "buyout": 3, "partnership": 3,
    "contract": 3, "guidance raise": 3, "earnings beat": 3, "revenue beat": 3,
    "ipo": 2, "spinoff": 2, "buyback": 2, "dividend": 2, "insider buy": 2,
}


def catalyst_score(headline: str) -> int:
    """ציון catalyst 0-15+. ≥3 = שווה ל-LLM. ≥6 = catalyst חזק."""
    h = headline.lower()
    return sum(w for kw, w in HIGH_IMPACT_KEYWORDS.items() if kw in h)


NEWS_CATALYST_KEYWORDS = [
    "fda", "approval", "approved", "clearance",
    "merger", "acquisition", "buyout", "takeover",
    "deal", "agreement", "contract",
    "partnership", "collaboration",
    "raises", "funding", "investment", "round",
    "settlement", "verdict",
]


def detect_news_catalyst(headlines: list) -> list:
    """Return list of unique matched catalyst keywords (title-cased), ordered by first appearance."""
    found = []
    seen  = set()
    for h in headlines:
        hl = h.lower()
        for kw in NEWS_CATALYST_KEYWORDS:
            if kw in hl and kw not in seen:
                found.append(kw.title())
                seen.add(kw)
    return found
