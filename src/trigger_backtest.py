"""
Historical validation of the live entry trigger.

The project has always used forward paper trading as its source of truth (see
CLAUDE.md, "Forward Signal Validation"). That is the slowest possible way to
learn whether a trigger works: at ~65 signals/month, proving an edge of +1%/trade
takes 8 months and +0.5%/trade takes over 3 years. This module answers the same
question from history instead — thousands of signal instances across multiple
regimes, in minutes.

What it measures
----------------
For every bullish Supertrend(1H) flip in the sample, it records the forward return
at each horizon and the return of a benchmark over the *identical wall-clock
window*, then reports the excess with an n and a t-stat. Raw win rate is not a
result — a long-only signal in a rising tape inherits market beta, which is what
hid the absence of an edge for three months.

Scope
-----
This validates the TRIGGER, not the whole production funnel. The monitoring queue
(scanner score >= 65, liquidity gate, recent-BUY feeder) cannot be reconstructed
historically because it depends on scan_results that only exist going forward.
Since the composite score was measured to carry no predictive information
(2026-08-05 audit), testing the trigger in isolation is the relevant question.

Point-in-time discipline
------------------------
- Entry is the CLOSE of the bar that flipped. Production reacts within one 5-min
  cycle of that bar closing (`bars_ago == 1`), so this is the honest fill proxy.
- Daily filters (SMA50/SMA200/RSI/ADV) are computed from the last daily bar that
  closed STRICTLY BEFORE the signal date. Using the signal day's own daily bar
  would leak the rest of that session into the entry decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent / "data" / "backtest_cache"
DEFAULT_HORIZONS = (7, 14, 30)
DEFAULT_BENCHMARKS = ("IWM", "SPY")

# Supertrend params — must match production (src/supertrend.py defaults, used by
# ibkr_worker._check_ticker via supertrend(hist, period=10, multiplier=3.0)).
ST_PERIOD = 10
ST_MULTIPLIER = 3.0


# ─────────────────────────────────────────────────────────────────────────
# Supertrend — full-history trend series
# ─────────────────────────────────────────────────────────────────────────

def trend_series(
    hist: pd.DataFrame,
    period: int = ST_PERIOD,
    multiplier: float = ST_MULTIPLIER,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (trend, final_lower, final_upper) for the whole history.

    Deliberately mirrors src/supertrend.py line for line — same Wilder-EMA ATR,
    same carry-forward band logic, same trend recursion — so a flip found here is
    the same flip production would have fired on. src/supertrend.py only exposes
    the last bar's signal, which is all the live worker needs but useless for a
    backtest. tests/test_trigger_backtest.py asserts the two agree.
    """
    high, low, close = hist["High"], hist["Low"], hist["Close"]

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    hl2 = (high + low) / 2.0
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    final_lower = lower.copy()
    final_upper = upper.copy()
    cl = close.to_numpy()
    fl = final_lower.to_numpy(copy=True)
    fu = final_upper.to_numpy(copy=True)
    lo = lower.to_numpy()
    up = upper.to_numpy()

    for i in range(1, len(hist)):
        fl[i] = max(lo[i], fl[i - 1]) if cl[i - 1] >= fl[i - 1] else lo[i]
        fu[i] = min(up[i], fu[i - 1]) if cl[i - 1] <= fu[i - 1] else up[i]

    tr_arr = np.ones(len(hist), dtype=int)
    for i in range(1, len(hist)):
        if tr_arr[i - 1] == 1:
            tr_arr[i] = -1 if cl[i] < fl[i] else 1
        else:
            tr_arr[i] = 1 if cl[i] > fu[i] else -1

    idx = hist.index
    return (
        pd.Series(tr_arr, index=idx, name="trend"),
        pd.Series(fl, index=idx, name="final_lower"),
        pd.Series(fu, index=idx, name="final_upper"),
    )


def find_flips(hist: pd.DataFrame, direction: str = "BUY") -> pd.DataFrame:
    """Every Supertrend flip in `hist`, as one row per flip.

    direction="BUY" returns bearish→bullish flips, "SELL" the reverse.
    The `warmup` guard drops flips inside the ATR warmup window, where the trend
    series is still seeded rather than measured.
    """
    if hist is None or len(hist) < ST_PERIOD + 2:
        return pd.DataFrame()

    trend, fl, fu = trend_series(hist)
    prev = trend.shift(1)
    if direction == "BUY":
        mask = (trend == 1) & (prev == -1)
        level = fl
    else:
        mask = (trend == -1) & (prev == 1)
        level = fu

    warmup = ST_PERIOD * 3
    mask.iloc[:warmup] = False

    if not mask.any():
        return pd.DataFrame()

    out = pd.DataFrame({
        "ts": hist.index[mask],
        "entry": hist["Close"][mask].to_numpy(),
        "level": level[mask].to_numpy(),
    })
    return out.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────

def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.pkl"


def load_bars(
    tickers: Sequence[str],
    interval: str = "1h",
    period: str = "730d",
    batch_size: int = 40,
    use_cache: bool = True,
) -> dict[str, pd.DataFrame]:
    """Download OHLCV per ticker, batched and cached to disk.

    yfinance caps intraday history at 730 days; it currently serves ~3 years of
    hourly bars in practice. Timestamps are normalised to tz-naive UTC so hourly
    bars and daily bars can be aligned without tz arithmetic at every use site.
    """
    import yfinance as yf

    key = f"{interval}_{period}_{len(tickers)}_{hash(tuple(sorted(tickers))) & 0xFFFFFFFF:08x}"
    cache = _cache_path(key)
    if use_cache and cache.exists():
        logger.info(f"[backtest] loading cached bars from {cache.name}")
        return pd.read_pickle(cache)

    out: dict[str, pd.DataFrame] = {}
    tickers = list(dict.fromkeys(tickers))
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        logger.info(f"[backtest] downloading {interval} bars {i + 1}-{i + len(batch)} of {len(tickers)}")
        try:
            data = yf.download(batch, interval=interval, period=period, auto_adjust=True,
                               progress=False, group_by="ticker", threads=True)
        except Exception as e:
            logger.warning(f"[backtest] batch download failed: {e}")
            continue
        for t in batch:
            try:
                df = data[t] if len(batch) > 1 else data
                df = df.dropna(how="all")
                if df is None or df.empty or len(df) < ST_PERIOD * 4:
                    continue
                df = df.copy()
                idx = pd.DatetimeIndex(df.index)
                df.index = (idx.tz_convert("UTC").tz_localize(None)
                            if idx.tz is not None else idx)
                out[t] = df
            except Exception:
                continue

    if use_cache and out:
        pd.to_pickle(out, cache)
        logger.info(f"[backtest] cached {len(out)} tickers to {cache.name}")
    return out


def daily_features(daily: pd.DataFrame) -> pd.DataFrame:
    """SMA50 / SMA200 / RSI14 / 20d average dollar volume on daily bars."""
    d = daily.copy()
    close = d["Close"]
    d["sma50"] = close.rolling(50).mean()
    d["sma200"] = close.rolling(200).mean()
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    d["advdol"] = (close * d["Volume"]).rolling(20).mean()
    return d


# ─────────────────────────────────────────────────────────────────────────
# Backtest
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    benchmarks: tuple[str, ...] = DEFAULT_BENCHMARKS
    min_price: float = 0.0
    signal_hours_only: bool = True   # production gates BUY to 09:30-20:00 ET


def _forward_return(close: pd.Series, ts: pd.Timestamp, days: int) -> float | None:
    """Percent change from the bar at `ts` to the first bar >= ts + days."""
    i0 = close.index.searchsorted(ts)
    i1 = close.index.searchsorted(ts + pd.Timedelta(days=days))
    if i0 >= len(close) or i1 >= len(close) or i1 <= i0:
        return None
    p0, p1 = float(close.iloc[i0]), float(close.iloc[i1])
    if p0 <= 0:
        return None
    return (p1 / p0 - 1.0) * 100.0


def run_backtest(
    hourly: dict[str, pd.DataFrame],
    daily: dict[str, pd.DataFrame],
    bench_daily: dict[str, pd.DataFrame],
    config: BacktestConfig | None = None,
) -> pd.DataFrame:
    """One row per bullish Supertrend(1H) flip, with forward and excess returns."""
    cfg = config or BacktestConfig()
    feats = {t: daily_features(df) for t, df in daily.items()}
    bench_close = {b: df["Close"] for b, df in bench_daily.items()}

    spy = bench_daily.get("SPY")
    spy_sma200 = spy["Close"].rolling(200).mean() if spy is not None else None

    rows: list[dict] = []
    for ticker, hdf in hourly.items():
        flips = find_flips(hdf, "BUY")
        if flips.empty or ticker not in feats:
            continue
        fdf = feats[ticker]
        hclose = hdf["Close"]

        for _, flip in flips.iterrows():
            ts: pd.Timestamp = flip["ts"]
            entry = float(flip["entry"])
            if entry <= cfg.min_price:
                continue

            if cfg.signal_hours_only:
                # Production blocks BUY outside 09:30-20:00 ET. Bars are UTC;
                # 09:30 ET is 13:30 or 14:30 UTC depending on DST, so 13:30-20:00
                # UTC is the conservative always-valid regular-session window.
                if not (13 <= ts.hour < 20):
                    continue

            # Daily features from the last bar closing STRICTLY before signal day.
            sig_day = ts.normalize()
            di = fdf.index.searchsorted(sig_day) - 1
            if di < 200:
                continue
            f = fdf.iloc[di]
            if pd.isna(f["sma200"]) or pd.isna(f["rsi"]):
                continue

            row = {
                "ticker": ticker,
                "ts": ts,
                "date": sig_day,
                "entry": entry,
                "st_level": float(flip["level"]),
                "risk_pct": (entry - float(flip["level"])) / entry * 100 if entry else np.nan,
                "d_close": float(f["Close"]),
                "sma50": float(f["sma50"]) if not pd.isna(f["sma50"]) else np.nan,
                "sma200": float(f["sma200"]),
                "rsi": float(f["rsi"]),
                "advdol": float(f["advdol"]) if not pd.isna(f["advdol"]) else 0.0,
            }

            if spy_sma200 is not None:
                si = spy_sma200.index.searchsorted(sig_day) - 1
                row["spy_bull"] = bool(
                    si >= 200 and not pd.isna(spy_sma200.iloc[si])
                    and float(spy["Close"].iloc[si]) > float(spy_sma200.iloc[si])
                )
            else:
                row["spy_bull"] = True

            keep = False
            for h in cfg.horizons:
                r = _forward_return(hclose, ts, h)
                row[f"ret{h}"] = r
                for b, bc in bench_close.items():
                    br = _forward_return(bc, sig_day, h)
                    row[f"bench_{b}_{h}"] = br
                    row[f"exc_{b}_{h}"] = (r - br) if (r is not None and br is not None) else None
                if r is not None:
                    keep = True
            if keep:
                rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("ts").reset_index(drop=True)
    return df


# ─────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────

FILTERS: dict[str, callable] = {
    "baseline (trigger only)": lambda d: pd.Series(True, index=d.index),
    "close > SMA50": lambda d: d.d_close > d.sma50,
    "close > SMA50 > SMA200": lambda d: (d.d_close > d.sma50) & (d.sma50 > d.sma200),
    "+ RSI < 70": lambda d: (d.d_close > d.sma50) & (d.sma50 > d.sma200) & (d.rsi < 70),
    "+ SPY bull regime": lambda d: (d.d_close > d.sma50) & (d.sma50 > d.sma200) & (d.rsi < 70) & d.spy_bull,
    "+ price >= $10": lambda d: (d.d_close > d.sma50) & (d.sma50 > d.sma200) & (d.rsi < 70) & d.spy_bull & (d.entry >= 10),
    "+ ADV >= $10M": lambda d: (d.d_close > d.sma50) & (d.sma50 > d.sma200) & (d.rsi < 70) & d.spy_bull & (d.entry >= 10) & (d.advdol >= 10e6),
    "INVERSE: close < SMA200": lambda d: d.d_close < d.sma200,
}


def stats(x: Iterable[float]) -> dict:
    a = np.asarray([v for v in x if v is not None and not pd.isna(v)], dtype=float)
    n = len(a)
    if n < 3:
        return {"n": n, "mean": np.nan, "sd": np.nan, "t": np.nan, "win": np.nan}
    m, sd = a.mean(), a.std(ddof=1)
    return {
        "n": n, "mean": m, "sd": sd,
        "t": m / (sd / np.sqrt(n)) if sd > 0 else 0.0,
        "win": (a > 0).mean() * 100,
    }


def report(events: pd.DataFrame, benchmark: str = "IWM",
           horizons: Sequence[int] = DEFAULT_HORIZONS) -> str:
    """Excess-return table per filter per horizon. Excess vs benchmark is the
    headline; raw return is shown only for context."""
    if events.empty:
        return "no events"
    lines: list[str] = []
    for h in horizons:
        col = f"exc_{benchmark}_{h}"
        raw = f"ret{h}"
        if col not in events.columns:
            continue
        lines.append(f"===== {h}-day horizon — excess vs {benchmark} =====")
        lines.append(f"{'filter':<28}{'n':>6}{'raw':>9}{'excess':>9}{'sd':>8}{'t':>7}{'win%':>7}  verdict")
        lines.append("-" * 90)
        for name, fn in FILTERS.items():
            try:
                sub = events[fn(events)]
            except Exception:
                continue
            s = stats(sub[col])
            r = stats(sub[raw])
            if s["n"] < 3:
                lines.append(f"{name:<28}{s['n']:>6}   (too few)")
                continue
            verdict = "SIGNIFICANT" if abs(s["t"]) > 2 else ""
            lines.append(
                f"{name:<28}{s['n']:>6}{r['mean']:>+8.2f}%{s['mean']:>+8.2f}%"
                f"{s['sd']:>7.2f}{s['t']:>+7.2f}{s['win']:>6.1f}%  {verdict}"
            )
        lines.append("")
    return "\n".join(lines)


def regime_report(events: pd.DataFrame, benchmark: str = "IWM", horizon: int = 30) -> str:
    """Does the trigger hold up when the market is NOT rising?

    The forward-paper window (2026-05..08) was a single rising regime. An edge
    that only exists in a bull tape is beta wearing a costume.
    """
    col = f"exc_{benchmark}_{horizon}"
    if events.empty or col not in events.columns:
        return "no events"
    lines = [f"===== regime split — {horizon}d excess vs {benchmark} =====",
             f"{'regime':<22}{'n':>6}{'excess':>9}{'t':>7}{'win%':>7}", "-" * 52]
    for label, mask in (("SPY above SMA200", events.spy_bull),
                        ("SPY below SMA200", ~events.spy_bull)):
        s = stats(events[mask][col])
        if s["n"] < 3:
            lines.append(f"{label:<22}{s['n']:>6}   (too few)")
            continue
        lines.append(f"{label:<22}{s['n']:>6}{s['mean']:>+8.2f}%{s['t']:>+7.2f}{s['win']:>6.1f}%")
    lines.append("")
    lines.append(f"{'year':<22}{'n':>6}{'excess':>9}{'t':>7}{'win%':>7}")
    lines.append("-" * 52)
    for year, grp in events.groupby(events.ts.dt.year):
        s = stats(grp[col])
        if s["n"] < 3:
            continue
        lines.append(f"{str(year):<22}{s['n']:>6}{s['mean']:>+8.2f}%{s['t']:>+7.2f}{s['win']:>6.1f}%")
    return "\n".join(lines)
