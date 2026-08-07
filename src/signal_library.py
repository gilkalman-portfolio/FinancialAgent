"""
Candidate entry signals, all testable through the same backtest harness.

Each function takes (hourly_df, daily_df) for ONE ticker and returns a DataFrame
with columns `ts` (entry timestamp) and `entry` (entry price), optionally `level`.
`src.trigger_backtest.run_backtest(signal_fn=...)` does the rest: point-in-time
filters, benchmark alignment over identical windows, maturity handling, and the
exit simulation.

Why a panel and not one idea
---------------------------
Intuition has already been wrong here twice. `close > SMA50` looked like the best
filter naively and turned out worse than no filter once overlap was corrected.
Testing several pre-committed candidates at once, with an explicit
multiple-comparison threshold, is the only honest way to search — otherwise the
best of N random ideas always looks significant.

Signals fire on DAILY bars unless noted. Entry is the daily close of the trigger
bar, which is tradeable at the close or next open. Signals are deliberately
restricted to OHLCV so nothing depends on data the project cannot reconstruct
point-in-time.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.trigger_backtest import find_flips


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["ts", "entry"])


def _events(daily: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    """Turn a boolean daily mask into entry events at that bar's close."""
    mask = mask.fillna(False)
    if not mask.any():
        return _empty()
    return pd.DataFrame({
        "ts": daily.index[mask],
        "entry": daily["Close"][mask].to_numpy(),
    }).reset_index(drop=True)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


# ── The control ────────────────────────────────────────────────────────────

def supertrend_flip(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Production trigger. Measured at +0.00% excess — the benchmark to beat."""
    return find_flips(hourly, "BUY")


# ── Trend / momentum family ────────────────────────────────────────────────

def golden_cross(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """SMA50 crosses above SMA200. Slow, few signals, widely watched."""
    c = daily["Close"]
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    cross = (s50 > s200) & (s50.shift(1) <= s200.shift(1))
    return _events(daily, cross)


def breakout_52w(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Close at a new 252-day high, on above-average volume.

    Time-series momentum. The volume condition is what separates a real breakout
    from a drift through an old high.
    """
    c, v = daily["Close"], daily["Volume"]
    prior_high = c.shift(1).rolling(252).max()
    volume_ok = v > v.rolling(50).mean() * 1.5
    return _events(daily, (c > prior_high) & volume_ok)


def donchian_20(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Close above the highest high of the prior 20 sessions (turtle entry)."""
    c = daily["Close"]
    prior = daily["High"].shift(1).rolling(20).max()
    return _events(daily, (c > prior) & (c.shift(1) <= prior.shift(1)))


def momentum_12_1(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Classic 12-1 momentum: strong 12-month return excluding the last month.

    Fires on the first session of each month when the condition holds, mimicking
    a monthly-rebalanced book rather than firing every day.
    """
    c = daily["Close"]
    mom = c.shift(21) / c.shift(252) - 1.0
    month_start = daily.index.to_series().dt.month.diff().fillna(1) != 0
    return _events(daily, (mom > 0.20) & month_start)


# ── Mean reversion family ──────────────────────────────────────────────────

def rsi_dip_in_uptrend(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """RSI(14) crosses back up through 35 while price is above SMA200.

    Buying weakness only within an established uptrend — the standard way to keep
    a mean-reversion rule from catching falling knives.
    """
    c = daily["Close"]
    r = _rsi(c)
    s200 = c.rolling(200).mean()
    return _events(daily, (r > 35) & (r.shift(1) <= 35) & (c > s200))


def gap_up_continuation(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Opens >3% above the prior close and holds it into the close, on volume.

    A proxy for reacting to news the market is still absorbing — the same family
    as post-earnings drift, but computable from price alone. True PEAD needs
    point-in-time earnings dates, which yfinance does not serve far enough back.
    """
    o, c, v = daily["Open"], daily["Close"], daily["Volume"]
    prev = c.shift(1)
    gap = (o - prev) / prev
    return _events(daily, (gap > 0.03) & (c > o) & (v > v.rolling(50).mean() * 2.0))
