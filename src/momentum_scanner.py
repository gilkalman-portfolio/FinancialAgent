"""
Momentum Scanner — 5-factor momentum score (0–100)
Vectorized pandas ops + yf.download batch — ~0.2s per ticker at scale

Components:
  Price ROC 20d        0–25   (P_today - P_20d) / P_20d * 100
  Relative Strength    0–20   ROC_stock / ROC_SPY
  MA Stack             0–25   price > MA20 > MA50 > MA200
  RSI Zone 50–70       0–15
  Volume Surge 5d/30d  0–15
"""

import pandas as pd
import numpy as np
import yfinance as yf
from typing import Optional
from loguru import logger


def _rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_series = 100 - (100 / (1 + rs))
    return float(rsi_series.iloc[-1]) if not rsi_series.empty else 50.0


def _price_roc_pts(roc: float) -> float:
    """Linear 0–25 pts; full score at +20% ROC."""
    return max(0.0, min(25.0, roc * 1.25))


def _rs_pts(roc_stock: float, roc_spy: float) -> float:
    """Relative Strength vs SPY → 0–20 pts."""
    if roc_spy <= 0:
        return 20.0 if roc_stock > 0 else 0.0
    rs = roc_stock / roc_spy
    if rs >= 2.0: return 20.0
    if rs >= 1.5: return 16.0
    if rs >= 1.0: return 12.0
    if rs >= 0.5: return 6.0
    return 0.0


def _ma_stack_pts(price: float, ma20: float, ma50: float, ma200: float) -> float:
    """Bullish MA alignment → 0–25 pts."""
    pts = 0.0
    if price > ma200: pts += 10.0
    if ma20  > ma50:  pts += 8.0
    if ma50  > ma200: pts += 7.0
    return pts


def _rsi_zone_pts(rsi: float) -> float:
    """Momentum zone 50–70 (not overbought) → 0–15 pts."""
    if 50 <= rsi <= 70: return 15.0
    if 45 <= rsi < 50:  return 8.0
    if 70 < rsi <= 75:  return 8.0
    return 0.0


def _volume_surge_pts(vol_5d: float, vol_30d: float) -> float:
    """5d avg volume vs 30d avg → 0–15 pts."""
    if vol_30d <= 0:
        return 0.0
    ratio = vol_5d / vol_30d
    if ratio >= 2.5: return 15.0
    if ratio >= 2.0: return 12.0
    if ratio >= 1.5: return 8.0
    if ratio >= 1.2: return 4.0
    return 0.0


def _price_change_5d(closes: pd.Series) -> float:
    if len(closes) < 6:
        return 0.0
    p5 = float(closes.iloc[-6])
    if p5 <= 0:
        return 0.0
    return (float(closes.iloc[-1]) - p5) / p5 * 100


def _is_breakout(closes: pd.Series, lookback: int) -> bool:
    """True if today's price is within 3% of the N-day high (breakout zone)."""
    if len(closes) < lookback:
        return False
    window_high = float(closes.tail(lookback).max())
    if window_high <= 0:
        return False
    return float(closes.iloc[-1]) >= window_high * 0.97


def _score_series(
    closes: pd.Series,
    volumes: pd.Series,
    spy_roc_20d: float,
    breakout_lookback_days: int = 20,
) -> Optional[dict]:
    """Score a single ticker from its price/volume series. Returns None if data insufficient."""
    if len(closes) < 205:
        return None

    p_today = float(closes.iloc[-1])
    p_20d   = float(closes.iloc[-21])
    if p_20d <= 0 or p_today <= 0:
        return None

    roc   = (p_today - p_20d) / p_20d * 100
    ma20  = float(closes.tail(20).mean())
    ma50  = float(closes.tail(50).mean())
    ma200 = float(closes.tail(200).mean())
    rsi   = _rsi(closes.tail(30))

    if len(volumes) < 30:
        return None
    vol_5d  = float(volumes.tail(5).mean())
    vol_30d = float(volumes.tail(30).mean())

    roc_pts = _price_roc_pts(roc)
    rs_pts  = _rs_pts(roc, spy_roc_20d)
    ma_pts  = _ma_stack_pts(p_today, ma20, ma50, ma200)
    rsi_pts = _rsi_zone_pts(rsi)
    vol_pts = _volume_surge_pts(vol_5d, vol_30d)
    score   = roc_pts + rs_pts + ma_pts + rsi_pts + vol_pts

    return {
        "price":           round(p_today, 2),
        "roc_20d":         round(roc, 2),
        "price_change_5d": round(_price_change_5d(closes), 2),
        "is_breakout":     _is_breakout(closes, breakout_lookback_days),
        "ma20":            round(ma20, 2),
        "ma50":            round(ma50, 2),
        "ma200":           round(ma200, 2),
        "rsi":             round(rsi, 1),
        "vol_ratio":       round(vol_5d / vol_30d if vol_30d > 0 else 0.0, 2),
        "score":           round(score, 1),
        "pts_roc":         round(roc_pts, 1),
        "pts_rs":          round(rs_pts, 1),
        "pts_ma":          round(ma_pts, 1),
        "pts_rsi":         round(rsi_pts, 1),
        "pts_vol":         round(vol_pts, 1),
    }


def scan_momentum(
    tickers: list,
    min_score: float = 70.0,
    breakout_lookback_days: int = 20,
) -> list:
    """
    Batch-download OHLCV for all tickers + SPY, compute momentum scores.
    Returns list of result dicts sorted by score desc, only those >= min_score.
    """
    if not tickers:
        return []

    all_tickers = list(dict.fromkeys(tickers + ["SPY"]))
    logger.info(f"Momentum scan: downloading {len(all_tickers)} tickers ({len(tickers)} + SPY)")

    try:
        raw = yf.download(
            all_tickers,
            period="1y",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error(f"Momentum scan download failed: {e}")
        return []

    if raw.empty:
        logger.warning("Momentum scan: empty data returned")
        return []

    # Normalise single vs multi-ticker column structure
    if isinstance(raw.columns, pd.MultiIndex):
        closes  = raw["Close"]
        volumes = raw["Volume"]
    else:
        closes  = raw[["Close"]].rename(columns={"Close": all_tickers[0]})
        volumes = raw[["Volume"]].rename(columns={"Volume": all_tickers[0]})

    # SPY 20d ROC as benchmark
    spy_roc_20d = 0.0
    if "SPY" in closes.columns:
        spy_close = closes["SPY"].dropna()
        if len(spy_close) >= 21:
            spy_roc_20d = float(
                (spy_close.iloc[-1] - spy_close.iloc[-21]) / spy_close.iloc[-21] * 100
            )
        else:
            logger.warning(f"Momentum scan: SPY has only {len(spy_close)} bars — RS scores will use 0% benchmark")
    else:
        logger.warning("Momentum scan: SPY data missing from download — RS scores will use 0% benchmark")

    results = []
    for ticker in tickers:
        if ticker not in closes.columns:
            continue
        try:
            c = closes[ticker].dropna()
            v = volumes[ticker].dropna() if ticker in volumes.columns else pd.Series(dtype=float)
            data = _score_series(c, v, spy_roc_20d, breakout_lookback_days)
            if data and data["score"] >= min_score:
                results.append({"ticker": ticker, **data})
        except Exception as e:
            logger.debug(f"Momentum score {ticker}: {e}")

    results.sort(key=lambda x: x["score"], reverse=True)
    logger.info(
        f"Momentum scan complete: {len(results)}/{len(tickers)} above {min_score:.0f} | "
        f"SPY ROC 20d={spy_roc_20d:+.1f}%"
    )
    return results
