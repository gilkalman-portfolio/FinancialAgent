"""
Index Loader - טוען רשימות מניות ממדדים שונים
מקורות: iShares API (חינם), יורד ישירות ללא CSV

נתמך:
  Russell 2000  - IWM
  S&P 500       - IVV
  Nasdaq 100    - QQQ
  Dow Jones     - DIA
  S&P 400 Mid   - IJH
  Russell 1000  - IWB
"""

import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger
import json

CACHE_DIR  = Path(__file__).parent.parent / "data" / "index_cache"
CACHE_TTL  = timedelta(days=30)  # רענן כל 30 ימים (S&P 500 / Russell לא משתנים הרבה)

INDICES = {
    "Russell 2000":   {"url": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?tab=all&fileType=json"},
    "S&P 500":        {"url": "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?tab=all&fileType=json"},
    "Nasdaq 100":     {"url": "https://www.ishares.com/us/products/244008/ishares-nasdaq-100-etf/1467271812596.ajax?tab=all&fileType=json"},
    "S&P 400 MidCap": {"url": "https://www.ishares.com/us/products/239763/ishares-sp-midcap-400-etf/1467271812596.ajax?tab=all&fileType=json"},
    "Russell 1000":   {"url": "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax?tab=all&fileType=json"},
    "Dow Jones 30":   {"url": None},  # 30 מניות - hardcoded
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _cache_path(index_name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = index_name.replace(" ", "_").replace("/", "_")
    return CACHE_DIR / f"{safe}.json"


def _load_cache(index_name: str) -> Optional[dict]:
    p = _cache_path(index_name)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        if datetime.now() - cached_at > CACHE_TTL:
            return None
        return data
    except Exception:
        return None


def _load_cache_stale(index_name: str) -> Optional[dict]:
    """Return cached data regardless of TTL — last-resort fallback when all live sources fail."""
    p = _cache_path(index_name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _save_cache(index_name: str, tickers: list, sectors: dict):
    p = _cache_path(index_name)
    p.write_text(json.dumps({
        "cached_at": datetime.now().isoformat(),
        "tickers": tickers,
        "sectors": sectors,
    }, indent=2))


def _fetch_ishares(url: str) -> Optional[pd.DataFrame]:
    """מוריד holdings מ-iShares API"""
    try:
        import json as _json
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        raw = r.content.decode('utf-8-sig')
        if raw.lstrip().startswith('<'):
            logger.warning("iShares returned HTML instead of JSON — API may have changed")
            return None
        data = _json.loads(raw)
        rows = data.get("aaData", [])
        if not rows:
            return None
        df = pd.DataFrame(rows)
        # עמודות iShares: 0=ticker, 1=name, 2=sector, 3=asset class
        df = df.iloc[:, [0, 1, 2]].copy()
        df.columns = ["ticker", "name", "sector"]
        df = df[df["ticker"].str.len() <= 5]
        df = df[df["ticker"] != "-"]
        df = df[df["ticker"].str.match(r'^[A-Z]+$', na=False)]
        df = df.dropna(subset=["ticker"])
        return df
    except Exception as e:
        logger.warning(f"iShares fetch failed: {e}")
        return None


def _fetch_sp500_wikipedia() -> Optional[pd.DataFrame]:
    """S&P 500 מוויקיפדיה — fallback כשiShares לא זמין"""
    try:
        import io
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None
        tables = pd.read_html(io.StringIO(r.text))
        if not tables:
            logger.error("Wikipedia S&P 500: no tables found in page — site structure may have changed")
            return None
        expected_cols = {"Symbol", "Security", "GICS Sector"}
        if not expected_cols.issubset(set(tables[0].columns)):
            logger.error(
                f"Wikipedia S&P 500: expected columns {expected_cols} not found "
                f"(got {list(tables[0].columns)[:5]}) — site structure may have changed"
            )
            return None
        df = tables[0][["Symbol", "Security", "GICS Sector"]].copy()
        df.columns = ["ticker", "name", "sector"]
        # BRK.B → BRK-B (yfinance format)
        df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)
        df = df[df["ticker"].str.match(r"^[A-Z\-]+$", na=False)]
        df = df.dropna(subset=["ticker"])
        return df
    except Exception as e:
        logger.warning(f"Wikipedia S&P 500 fetch failed: {e}")
        return None


def _fetch_russell_csv() -> Optional[pd.DataFrame]:
    """Russell 2000 מ-CSV מקומי (fallback)"""
    csv_path = Path(__file__).parent.parent / "russell_holdings.csv"
    if not csv_path.exists():
        return None
    for skip in [10, 9, 11, 0, 1, 2]:
        try:
            df = pd.read_csv(csv_path, skiprows=skip, encoding="utf-8", on_bad_lines="skip")
            tc = next((c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower()), None)
            sc = next((c for c in df.columns if "sector" in c.lower()), None)
            nm = next((c for c in df.columns if "name" in c.lower() and "file" not in c.lower()), None)
            if tc and sc:
                out = df[[tc, nm or tc, sc]].copy()
                out.columns = ["ticker", "name", "sector"]
                out = out.dropna(subset=["ticker", "sector"])
                out = out[out["ticker"].str.len() <= 5]
                return out
        except Exception:
            continue
    return None


def get_index(index_name: str, force_refresh: bool = False) -> Optional[pd.DataFrame]:
    """
    מחזיר DataFrame עם עמודות: ticker, name, sector
    משתמש ב-cache אם זמין ועדכני.
    """
    if not force_refresh:
        cached = _load_cache(index_name)
        if cached:
            df = pd.DataFrame([
                {"ticker": t, "name": "", "sector": cached["sectors"].get(t, "Unknown")}
                for t in cached["tickers"]
            ])
            logger.info(f"{index_name}: loaded {len(df)} tickers from cache")
            return df

    cfg = INDICES.get(index_name)
    if not cfg:
        logger.warning(f"Unknown index: {index_name}")
        return None

    # Dow Jones 30 - hardcoded
    if index_name == "Dow Jones 30":
        dj30 = ["AAPL","AMGN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","DOW",
                "GS","HD","HON","IBM","INTC","JNJ","JPM","KO","MCD","MMM",
                "MRK","MSFT","NKE","PG","TRV","UNH","V","VZ","WBA","WMT"]
        df = pd.DataFrame([{"ticker": t, "name": t, "sector": "Mixed"} for t in dj30])
        sectors = {t: "Mixed" for t in dj30}
        _save_cache(index_name, dj30, sectors)
        return df

    logger.info(f"Fetching {index_name} from iShares...")
    df = _fetch_ishares(cfg["url"]) if cfg.get("url") else None

    # fallbacks
    if df is None and index_name == "Russell 2000":
        logger.info("Falling back to local russell_holdings.csv")
        df = _fetch_russell_csv()

    if df is None and index_name == "S&P 500":
        logger.info("Falling back to Wikipedia S&P 500 list")
        df = _fetch_sp500_wikipedia()

    if df is None or df.empty:
        logger.error(f"Could not load {index_name} from any live source")
        # Serve stale cache rather than returning None (empty scan universe is worse than stale data)
        stale = _load_cache_stale(index_name)
        if stale and stale.get("tickers"):
            logger.warning(
                f"{index_name}: serving stale cache (cached_at={stale.get('cached_at', 'unknown')}) "
                f"— all live sources failed"
            )
            return pd.DataFrame([
                {"ticker": t, "name": "", "sector": stale["sectors"].get(t, "Unknown")}
                for t in stale["tickers"]
            ])
        return None

    # שמור cache
    sectors = dict(zip(df["ticker"], df["sector"]))
    _save_cache(index_name, df["ticker"].tolist(), sectors)
    logger.info(f"{index_name}: loaded {len(df)} tickers, cached for {CACHE_TTL.days} days")
    return df


def list_indices() -> list:
    return list(INDICES.keys())


def get_sectors(index_name: str) -> list:
    """מחזיר רשימת סקטורים ייחודיים"""
    df = get_index(index_name)
    if df is None:
        return []
    return sorted(df["sector"].dropna().unique().tolist())


def get_tickers_by_sector(index_name: str, sector: str, max_stocks: int = None) -> list:
    """מחזיר טיקרים לסקטור מסוים"""
    df = get_index(index_name)
    if df is None:
        return []
    mask    = df["sector"].str.lower().str.contains(sector.lower(), na=False)
    tickers = df[mask]["ticker"].tolist()
    if max_stocks:
        tickers = tickers[:max_stocks]
    return tickers


def refresh_all():
    """רענן את כל המדדים"""
    for name in INDICES:
        get_index(name, force_refresh=True)
