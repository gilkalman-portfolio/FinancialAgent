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
            if close.iloc[i - 1] >= final_lower.iloc[i - 1]:
                final_lower.iloc[i] = max(lower.iloc[i], final_lower.iloc[i - 1])
            else:
                final_lower.iloc[i] = lower.iloc[i]

            # Upper band: only lower if previous close was below it (or new band is lower)
            if close.iloc[i - 1] <= final_upper.iloc[i - 1]:
                final_upper.iloc[i] = min(upper.iloc[i], final_upper.iloc[i - 1])
            else:
                final_upper.iloc[i] = upper.iloc[i]

        # ── Trend direction ──────────────────────────────────────────────────
        trend = pd.Series(1, index=hist.index, dtype=int)

        for i in range(1, len(hist)):
            prev_trend = trend.iloc[i - 1]
            if prev_trend == 1:
                trend.iloc[i] = -1 if close.iloc[i] < final_lower.iloc[i] else 1
            else:
                trend.iloc[i] = 1  if close.iloc[i] > final_upper.iloc[i] else -1

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
