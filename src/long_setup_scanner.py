"""
Long Setup Scanner
Identifies stocks likely to move UP in the next 1–5 days.

Score (0–100):
  RSI zone 40–65     0–20  momentum building, not overbought
  MACD crossover     0–25  bullish cross within last 3 days
  Volume surge       0–20  5d avg > 1.5x 30d avg (accumulation)
  MA alignment       0–20  price > MA20 > MA50
  Momentum 5d        0–15  positive 5-day price change
"""

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime
from loguru import logger
from typing import Optional

SP100_SUBSET = {
    "AAPL","MSFT","AMZN","GOOGL","META","NVDA","TSLA","BRK-B","JPM","JNJ",
    "V","UNH","XOM","PG","MA","HD","CVX","MRK","ABBV","PEP","KO","AVGO",
    "COST","TMO","WMT","MCD","ACN","LIN","ABT","DHR","TXN","NEE","PM","RTX",
    "BMY","ORCL","QCOM","HON","UPS","IBM","GE","CAT","SBUX","LOW","INTC",
    "AMD","SPGI","AXP","BA","GS","MMM","CRM","ISRG","BLK","DE","SYK","ADP",
    "GILD","T","VZ","MDLZ","AMGN","C","WFC","BAC","MS","SCHW",
    "SPY","QQQ","IWM","DIA","XLK","XLF","XLE","XLV","XLI","XLY",
}


# ── Scoring helpers ────────────────────────────────────────────────────────────

def _rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    val   = 100 - (100 / (1 + rs))
    return float(val.iloc[-1]) if not val.empty else 50.0


def _macd_crossover_days(closes: pd.Series) -> Optional[int]:
    """Returns how many days ago MACD crossed above signal, or None if no recent cross."""
    if len(closes) < 35:
        return None
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    diff  = macd - signal
    for i in range(1, min(6, len(diff))):
        if diff.iloc[-i] > 0 and diff.iloc[-i-1] <= 0:
            return i
    return None


def _rsi_pts(rsi: float) -> float:
    if 50 <= rsi <= 65: return 20.0
    if 40 <= rsi < 50:  return 14.0
    if 65 < rsi <= 70:  return 10.0
    if 35 <= rsi < 40:  return 6.0
    return 0.0


def _macd_pts(cross_days: Optional[int]) -> float:
    if cross_days is None: return 0.0
    if cross_days == 1:    return 25.0
    if cross_days == 2:    return 20.0
    if cross_days == 3:    return 15.0
    if cross_days <= 5:    return 8.0
    return 0.0


def _volume_pts(vol_5d: float, vol_30d: float) -> float:
    if vol_30d <= 0: return 0.0
    ratio = vol_5d / vol_30d
    if ratio >= 3.0: return 20.0
    if ratio >= 2.0: return 16.0
    if ratio >= 1.5: return 10.0
    if ratio >= 1.2: return 5.0
    return 0.0


def _ma_pts(price: float, ma20: float, ma50: float) -> float:
    pts = 0.0
    if price > ma20:  pts += 10.0
    if ma20  > ma50:  pts += 10.0
    return pts


def _momentum_pts(pct_5d: float) -> float:
    if pct_5d >= 5.0:  return 15.0
    if pct_5d >= 3.0:  return 11.0
    if pct_5d >= 1.0:  return 7.0
    if pct_5d >= 0.0:  return 3.0
    return 0.0


# ── Single ticker scoring ──────────────────────────────────────────────────────

def _score_ticker(ticker: str, closes: pd.Series, volumes: pd.Series) -> Optional[dict]:
    if len(closes) < 55:
        return None

    price  = float(closes.iloc[-1])
    p5     = float(closes.iloc[-6]) if len(closes) >= 6 else price
    pct_5d = (price - p5) / p5 * 100 if p5 > 0 else 0.0

    ma20 = float(closes.tail(20).mean())
    ma50 = float(closes.tail(50).mean())
    rsi  = _rsi(closes.tail(30))
    cross_days = _macd_crossover_days(closes)

    vol_5d  = float(volumes.tail(5).mean()) if len(volumes) >= 5 else 0.0
    vol_30d = float(volumes.tail(30).mean()) if len(volumes) >= 30 else 0.0

    pts_rsi  = _rsi_pts(rsi)
    pts_macd = _macd_pts(cross_days)
    pts_vol  = _volume_pts(vol_5d, vol_30d)
    pts_ma   = _ma_pts(price, ma20, ma50)
    pts_mom  = _momentum_pts(pct_5d)
    score    = pts_rsi + pts_macd + pts_vol + pts_ma + pts_mom

    return {
        "ticker":       ticker,
        "score":        round(score, 1),
        "price":        round(price, 2),
        "rsi":          round(rsi, 1),
        "pct_5d":       round(pct_5d, 2),
        "vol_ratio":    round(vol_5d / vol_30d if vol_30d > 0 else 0.0, 2),
        "macd_cross_days": cross_days,
        "ma20":         round(ma20, 2),
        "ma50":         round(ma50, 2),
        "above_ma20":   price > ma20,
        "ma20_gt_ma50": ma20 > ma50,
        "pts_rsi":      pts_rsi,
        "pts_macd":     pts_macd,
        "pts_vol":      pts_vol,
        "pts_ma":       pts_ma,
        "pts_mom":      pts_mom,
        "scanned_at":   datetime.now().isoformat(),
    }


# ── Main scanner ───────────────────────────────────────────────────────────────

def scan_long_setups(
    universe: Optional[list] = None,
    min_score: float = 50.0,
    top_n: int = 10,
) -> list[dict]:
    """
    Scan universe for bullish long setups. Returns top_n results sorted by score.

    Args:
        universe:  list of tickers (defaults to SP100_SUBSET)
        min_score: minimum long score to include (default 50)
        top_n:     maximum results to return (default 10)
    """
    tickers = universe or list(SP100_SUBSET)
    logger.info(f"Long setup scan: {len(tickers)} tickers, min_score={min_score}")

    try:
        raw = yf.download(
            tickers,
            period="6mo",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error(f"Long setup scan download failed: {e}")
        return []

    if raw.empty:
        logger.warning("Long setup scan: empty data")
        return []

    if isinstance(raw.columns, pd.MultiIndex):
        closes  = raw["Close"]
        volumes = raw["Volume"]
    else:
        closes  = raw[["Close"]].rename(columns={"Close": tickers[0]})
        volumes = raw[["Volume"]].rename(columns={"Volume": tickers[0]})

    results = []
    for ticker in tickers:
        if ticker not in closes.columns:
            continue
        try:
            c = closes[ticker].dropna()
            v = volumes[ticker].dropna() if ticker in volumes.columns else pd.Series(dtype=float)
            r = _score_ticker(ticker, c, v)
            if r and r["score"] >= min_score:
                results.append(r)
        except Exception as e:
            logger.debug(f"Long setup score {ticker}: {e}")

    results.sort(key=lambda x: x["score"], reverse=True)
    results = results[:top_n]

    logger.info(f"Long setup scan: {len(results)} setups found above {min_score}")
    return results
