"""
Supertrend Indicator — Python implementation identical to the TradingView Pine Script v4 original.

Algorithm:
  hl2    = (High + Low) / 2
  ATR    = rolling mean of True Range over `period` bars
  upper  = hl2 + multiplier * ATR   (upper band / resistance)
  lower  = hl2 - multiplier * ATR   (lower band / support)

  Bands are adjusted iteratively (carry-forward logic, same as up1/dn1 in Pine Script):
    final_upper[i] = min(upper[i], final_upper[i-1]) if close[i-1] <= final_upper[i-1]
    final_lower[i] = max(lower[i], final_lower[i-1]) if close[i-1] >= final_lower[i-1]

  Trend:
    trend = 1  (bullish) initially
    → flips to -1 when close < final_lower
    → flips to  1 when close > final_upper

  Signal (last two bars):
    BUY  : trend[-1] == 1  and trend[-2] == -1
    SELL : trend[-1] == -1 and trend[-2] ==  1
"""

import pandas as pd
import numpy as np
import yfinance as yf
from typing import Optional
from loguru import logger


def supertrend(
    hist: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
    lookback: int = 1,
) -> dict:
    """
    Calculate Supertrend for a yfinance OHLCV DataFrame.

    Args:
        hist:       DataFrame with columns High, Low, Close (from yf.Ticker.history())
        period:     ATR lookback period (default 10, same as Pine Script defval)
        multiplier: ATR multiplier (default 3.0, same as Pine Script defval)

    Returns:
        {
            "direction": "Bullish" | "Bearish",
            "signal":    "BUY" | "SELL" | None,   # None = no flip on last bar
            "level":     float,                    # current support/resistance line value
        }
        Returns {"direction": "N/A", "signal": None, "level": None} on insufficient data.
    """
    _empty = {"direction": "N/A", "signal": None, "level": None}

    if hist is None or len(hist) < period + 2:
        logger.debug(f"[supertrend] insufficient data ({len(hist) if hist is not None else 0} bars, need {period + 2})")
        return _empty

    try:
        high  = hist["High"]
        low   = hist["Low"]
        close = hist["Close"]

        # ── True Range & ATR ─────────────────────────────────────────────────
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

        # ── Raw bands ────────────────────────────────────────────────────────
        hl2   = (high + low) / 2.0
        upper = hl2 + multiplier * atr   # dn in Pine Script (resistance when bearish)
        lower = hl2 - multiplier * atr   # up in Pine Script (support when bullish)

        # ── Adjusted bands (carry-forward — Pine Script up1/dn1 logic) ───────
        final_lower = lower.copy()
        final_upper = upper.copy()

        for i in range(1, len(hist)):
            # Lower band: only raise if previous close was above it (or new band is higher)
            if close.iloc[i - 1] >= final_lower.iat[i - 1]:
                final_lower.iat[i] = max(lower.iat[i], final_lower.iat[i - 1])
            else:
                final_lower.iat[i] = lower.iat[i]

            # Upper band: only lower if previous close was below it (or new band is lower)
            if close.iloc[i - 1] <= final_upper.iat[i - 1]:
                final_upper.iat[i] = min(upper.iat[i], final_upper.iat[i - 1])
            else:
                final_upper.iat[i] = upper.iat[i]

        # ── Trend direction ──────────────────────────────────────────────────
        trend = pd.Series(1, index=hist.index, dtype=int)

        for i in range(1, len(hist)):
            prev_trend = trend.iat[i - 1]
            if prev_trend == 1:
                trend.iat[i] = -1 if close.iat[i] < final_lower.iat[i] else 1
            else:
                trend.iat[i] = 1  if close.iat[i] > final_upper.iat[i] else -1

        # ── Signal: detect flip within last `lookback` closed bars ──────────
        signal: Optional[str] = None
        bars_ago: int = 0

        for k in range(1, min(lookback + 1, len(trend))):
            t_now  = trend.iloc[-k]
            t_prev = trend.iloc[-k - 1]
            if t_now == 1 and t_prev == -1:
                signal = "BUY"
                bars_ago = k
                break
            elif t_now == -1 and t_prev == 1:
                signal = "SELL"
                bars_ago = k
                break

        t_now = trend.iloc[-1]

        # ── Current support/resistance level ─────────────────────────────────
        level = float(final_lower.iloc[-1]) if t_now == 1 else float(final_upper.iloc[-1])

        direction = "Bullish" if t_now == 1 else "Bearish"
        logger.debug(f"[supertrend] direction={direction}, signal={signal}, bars_ago={bars_ago}, level={level:.2f}")

        return {"direction": direction, "signal": signal, "level": level, "bars_ago": bars_ago}

    except Exception as e:
        logger.warning(f"[supertrend] calculation failed: {e}")
        return _empty


def scan_supertrend_universe(
    tickers: list,
    period: int = 10,
    multiplier: float = 3.0,
) -> list:
    """
    Batch-download daily OHLCV for `tickers` (same yf.download(..., threads=True)
    pattern as momentum_scanner.scan_momentum — ~0.2s/ticker at scale) and return
    every ticker with a FRESH bullish Supertrend flip (bars_ago == 1) on the daily
    bar, unfiltered by composite score. Mirrors a bare TradingView
    alertcondition(buySignal) applied to the whole scan universe, not just the
    pre-filtered IBKR monitoring queue (see src/monitoring_queue.py:39,
    SCANNER_MIN_SCORE=65) — the two are intentionally independent so a ticker
    with a strong technical flip but a middling composite score is still caught.

    Returns list of {"ticker", "price", "level", "avg_volume"} sorted by ticker.
    """
    if not tickers:
        return []

    logger.info(f"Supertrend universe scan: downloading {len(tickers)} tickers")
    try:
        raw = yf.download(
            tickers,
            period="1y",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error(f"Supertrend universe scan download failed: {e}")
        return []

    if raw.empty:
        logger.warning("Supertrend universe scan: empty data returned")
        return []

    if isinstance(raw.columns, pd.MultiIndex):
        highs, lows, closes, volumes = raw["High"], raw["Low"], raw["Close"], raw["Volume"]
    else:
        highs   = raw[["High"]].rename(columns={"High": tickers[0]})
        lows    = raw[["Low"]].rename(columns={"Low": tickers[0]})
        closes  = raw[["Close"]].rename(columns={"Close": tickers[0]})
        volumes = raw[["Volume"]].rename(columns={"Volume": tickers[0]})

    results = []
    for ticker in tickers:
        if ticker not in closes.columns:
            continue
        try:
            hist = pd.DataFrame({
                "High":  highs[ticker],
                "Low":   lows[ticker],
                "Close": closes[ticker],
            }).dropna()
            if len(hist) < period + 2:
                continue

            st = supertrend(hist, period=period, multiplier=multiplier, lookback=1)
            if st["signal"] != "BUY":
                continue

            vol = volumes[ticker].dropna() if ticker in volumes.columns else pd.Series(dtype=float)
            avg_volume = float(vol.tail(20).mean()) if len(vol) >= 20 else 0.0

            results.append({
                "ticker":     ticker,
                "price":      float(hist["Close"].iloc[-1]),
                "level":      st["level"],
                "avg_volume": avg_volume,
            })
        except Exception as e:
            logger.debug(f"Supertrend universe scan {ticker}: {e}")

    results.sort(key=lambda r: r["ticker"])
    logger.info(f"Supertrend universe scan complete: {len(results)}/{len(tickers)} fresh bullish flips")
    return results
