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


def _default_signal(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """The production trigger: bullish Supertrend(1H) flip."""
    return find_flips(hourly, "BUY")


def run_backtest(
    hourly: dict[str, pd.DataFrame],
    daily: dict[str, pd.DataFrame],
    bench_daily: dict[str, pd.DataFrame],
    config: BacktestConfig | None = None,
    signal_fn=None,
) -> pd.DataFrame:
    """One row per signal, with forward and benchmark-relative returns.

    signal_fn(hourly_df, daily_df) -> DataFrame with columns ts, entry, and
    optionally level. Defaults to the production Supertrend(1H) flip. Everything
    downstream — point-in-time filters, benchmark alignment, maturity handling,
    the exit simulator — is signal-agnostic, so a new idea is tested by writing
    one function rather than a new backtest.
    """
    cfg = config or BacktestConfig()
    fn = signal_fn or _default_signal
    feats = {t: daily_features(df) for t, df in daily.items()}
    bench_close = {b: df["Close"] for b, df in bench_daily.items()}

    spy = bench_daily.get("SPY")
    spy_sma200 = spy["Close"].rolling(200).mean() if spy is not None else None

    rows: list[dict] = []
    for ticker, hdf in hourly.items():
        if ticker not in feats:
            continue
        try:
            flips = fn(hdf, daily[ticker])
        except Exception as e:
            logger.warning(f"[backtest] signal failed for {ticker}: {e}")
            continue
        if flips is None or flips.empty:
            continue
        if "level" not in flips.columns:
            flips = flips.assign(level=np.nan)
        fdf = feats[ticker]
        hclose = hdf["Close"]
        dclose = daily[ticker]["Close"]
        daily_index = daily[ticker].index

        for _, flip in flips.iterrows():
            ts: pd.Timestamp = flip["ts"]
            entry = float(flip["entry"])
            if entry <= cfg.min_price:
                continue

            # Production blocks BUY outside 09:30-20:00 ET. Bars are UTC; 09:30 ET
            # is 13:30 or 14:30 UTC depending on DST, so 13:30-20:00 UTC is the
            # conservative always-valid regular-session window.
            #
            # Only meaningful for INTRADAY signals. A daily bar is timestamped at
            # midnight, so applying the gate to it rejects every daily signal —
            # which silently reported six candidate strategies as producing zero
            # signals rather than as being filtered out.
            is_intraday = ts != ts.normalize()
            if cfg.signal_hours_only and is_intraday:
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

            # Measure the forward return on the timeframe the signal fired on.
            # A daily signal's entry is a daily CLOSE; measuring it against the
            # hourly series would start the return from a mid-session bar and
            # silently disagree with `entry`. Hourly signals keep the hourly path.
            fwd_close = dclose if ts in daily_index else hclose

            keep = False
            for h in cfg.horizons:
                r = _forward_return(fwd_close, ts, h)
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


# ─────────────────────────────────────────────────────────────────────────
# Exit-regime simulation
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ExitConfig:
    """One exit regime. Distances are in ATR(14) units.

    stop_timeframe MUST match what production actually uses or the comparison is
    meaningless. `execution_engine._get_atr()` calls `history(period="30d")` with
    yfinance's default daily interval, so live stops are 2 x ATR(14, DAILY) — a
    multiple of the hourly ATR. Sizing a stop off hourly ATR produces a stop so
    tight that ordinary intraday noise takes it out within days, which looks like
    a strategy result but is a simulation artifact.
    """
    name: str
    stop_atr: float | None = 2.0        # initial stop = entry − stop_atr × ATR
    trail_atr: float | None = None      # trail from high-water once armed
    trail_arm_pct: float = 0.0          # only start trailing after +this %
    target_atr: float | None = None     # take profit at entry + target_atr × ATR
    max_hold_days: int | None = 30      # time exit
    supertrend_exit: bool = False       # exit on the next bearish 1H flip
    friction_pct: float = 0.20          # round-trip commission + slippage
    stop_timeframe: str = "daily"       # "daily" (matches production) | "hourly"


def _atr_at(hist: pd.DataFrame, i: int, period: int = 14) -> float:
    """ATR(14) using only bars up to and including i."""
    lo = max(0, i - period * 3)
    w = hist.iloc[lo:i + 1]
    if len(w) < period + 1:
        return float(w["Close"].iloc[-1]) * 0.02
    h, l, c = w["High"], w["Low"], w["Close"].shift(1)
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    v = tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    return float(v) if v > 0 else float(w["Close"].iloc[-1]) * 0.02


def _daily_atr_at(daily: pd.DataFrame, when: pd.Timestamp, period: int = 14) -> float | None:
    """ATR(14) on daily bars, using only sessions closed BEFORE `when`.

    Mirrors execution_engine._get_atr(), which is what sets the live stop distance.
    """
    i = daily.index.searchsorted(when.normalize()) - 1
    if i < period:
        return None
    w = daily.iloc[max(0, i - period * 3):i + 1]
    h, l, c = w["High"], w["Low"], w["Close"].shift(1)
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    v = tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    return float(v) if v > 0 else None


def simulate_exits(
    events: pd.DataFrame,
    hourly: dict[str, pd.DataFrame],
    bench_daily: dict[str, pd.DataFrame],
    cfg: ExitConfig,
    benchmark: str = "IWM",
    daily: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Walk each signal forward bar by bar and apply a real exit policy.

    The horizon study measures a fixed holding period, which no live system
    actually trades — it has stops. This replays the path.

    Fill assumptions are deliberately pessimistic: a bar that gaps through the
    stop exits at the OPEN, not the stop price; when a bar touches both the stop
    and the target, the stop is assumed first. Benchmark return is measured over
    the REALISED holding window so the comparison stays apples-to-apples.
    """
    bench_close = bench_daily[benchmark]["Close"]
    trend_cache: dict[str, pd.Series] = {}
    rows: list[dict] = []

    for ev in events.itertuples():
        # Manage the trade on the timeframe the signal fired on. A daily signal
        # walked forward on hourly bars is limited to the ~730d hourly window,
        # and any signal older than that maps to the FIRST hourly bar — turning a
        # 2022 entry into a year-long hold that exits in 2023. That produced
        # 98-day average holds under a 30-day time stop and inflated every daily
        # candidate's return. Daily signals walk daily bars, which cover 5y.
        ddf = (daily or {}).get(ev.ticker)
        is_daily_signal = ddf is not None and ev.ts in ddf.index
        hist = ddf if is_daily_signal else hourly.get(ev.ticker)
        if hist is None or len(hist) < 2:
            continue

        i0 = int(hist.index.searchsorted(ev.ts))
        if i0 >= len(hist) or i0 + 1 >= len(hist):
            continue
        # The signal must actually land inside this price series. A large gap
        # means the series starts after the signal, so there is no honest way to
        # simulate the trade.
        if abs((hist.index[i0] - ev.ts).total_seconds()) > 5 * 86400:
            continue

        entry = float(ev.entry)
        if cfg.stop_timeframe == "daily":
            ddf = (daily or {}).get(ev.ticker)
            atr = _daily_atr_at(ddf, ev.ts) if ddf is not None else None
            if atr is None:
                # Without a daily ATR the stop would silently fall back to a much
                # tighter hourly one, which is the exact artifact this guards
                # against. Skip the signal instead of simulating a wrong stop.
                continue
        else:
            atr = _atr_at(hist, i0)
        stop = entry - cfg.stop_atr * atr if cfg.stop_atr else None
        target = entry + cfg.target_atr * atr if cfg.target_atr else None
        deadline = ev.ts + pd.Timedelta(days=cfg.max_hold_days) if cfg.max_hold_days else None

        # The Supertrend exit is defined on the 1H series the trigger uses; it is
        # not meaningful for a daily-bar signal.
        if cfg.supertrend_exit and not is_daily_signal:
            if ev.ticker not in trend_cache:
                trend_cache[ev.ticker], _, _ = trend_series(hist)
            trend = trend_cache.get(ev.ticker)
        else:
            trend = None

        high_water = entry
        exit_px, exit_ts, reason = None, None, None

        for i in range(i0 + 1, len(hist)):
            bar = hist.iloc[i]
            ts = hist.index[i]
            o, h, l = float(bar["Open"]), float(bar["High"]), float(bar["Low"])

            if stop is not None and l <= stop:
                # Gap through the stop fills at the open, not the stop price.
                exit_px, exit_ts, reason = (min(o, stop), ts, "stop")
                break
            if target is not None and h >= target:
                exit_px, exit_ts, reason = (target, ts, "target")
                break

            if h > high_water:
                high_water = h
            if cfg.trail_atr and high_water >= entry * (1 + cfg.trail_arm_pct / 100.0):
                cand = high_water - cfg.trail_atr * atr
                stop = cand if stop is None else max(stop, cand)

            if trend is not None and trend.iloc[i] == -1 and trend.iloc[i - 1] == 1:
                exit_px, exit_ts, reason = (float(bar["Close"]), ts, "supertrend")
                break
            if deadline is not None and ts >= deadline:
                exit_px, exit_ts, reason = (float(bar["Close"]), ts, "time")
                break

        if exit_px is None:
            exit_px = float(hist["Close"].iloc[-1])
            exit_ts = hist.index[-1]
            reason = "eod"
            if deadline is not None and exit_ts < deadline:
                continue  # not yet matured — must not be counted as a closed trade

        gross = (exit_px / entry - 1.0) * 100.0
        net = gross - cfg.friction_pct

        b0 = bench_close.index.searchsorted(ev.ts.normalize())
        b1 = bench_close.index.searchsorted(exit_ts.normalize())
        bench_ret = None
        if b0 < len(bench_close) and b1 < len(bench_close) and b1 > b0:
            bench_ret = (float(bench_close.iloc[b1]) / float(bench_close.iloc[b0]) - 1.0) * 100.0

        rows.append({
            "ticker": ev.ticker, "ts": ev.ts, "exit_ts": exit_ts,
            "entry": entry, "exit": exit_px, "reason": reason,
            "hold_days": (exit_ts - ev.ts).total_seconds() / 86400.0,
            "gross_pct": gross, "net_pct": net,
            "bench_pct": bench_ret,
            "excess_pct": (net - bench_ret) if bench_ret is not None else None,
            "spy_bull": getattr(ev, "spy_bull", True),
        })

    return pd.DataFrame(rows)


def clustered_stats(trades: pd.DataFrame, col: str = "excess_pct") -> dict:
    """Mean and t-stat with monthly clustering and non-overlapping windows.

    Naive per-trade t-stats are inflated: overlapping holding periods share market
    noise, so correlated observations get counted as independent evidence.

    Non-overlap is defined by the PREVIOUS kept trade's exit, not by the current
    trade's own holding period. Spacing by the current trade's duration looks
    equivalent and is not: a trade that stopped out in 3 days then only needs 3
    days of clearance to be admitted, while a 30-day winner needs 30. That
    preferentially admits fast losers and drops slow winners, which made every
    stopped regime look far worse than its own raw mean (regime A: raw +0.00%
    excess reported as -1.03%). Selection on the outcome is exactly what this
    function exists to avoid.
    """
    if trades.empty or col not in trades:
        return {"n": 0, "mean": np.nan, "t": np.nan, "months": 0}
    d = trades[trades[col].notna()].sort_values("ts").copy()
    has_exit = "exit_ts" in d.columns
    keep, free_at = [], {}
    for r in d.itertuples():
        busy_until = free_at.get(r.ticker)
        if busy_until is None or r.ts >= busy_until:
            keep.append(r.Index)
            free_at[r.ticker] = (
                r.exit_ts if has_exit
                else r.ts + pd.Timedelta(days=float(getattr(r, "hold_days", 0) or 0))
            )
    d = d.loc[keep]
    if d.empty:
        return {"n": 0, "mean": np.nan, "t": np.nan, "months": 0}
    monthly = d.groupby(d.ts.dt.to_period("M"))[col].mean()
    s = stats(monthly)
    return {"n": len(d), "mean": s["mean"], "t": s["t"], "months": s["n"]}


def exit_report(results: dict[str, pd.DataFrame]) -> str:
    """Compare exit regimes. Excess vs benchmark, net of friction, month-clustered."""
    lines = [
        f"{'exit regime':<34}{'trades':>7}{'hold':>7}{'net':>8}{'bench':>8}"
        f"{'excess':>9}{'t':>7}{'indep':>7}{'win%':>7}",
        "-" * 96,
    ]
    for name, tr in results.items():
        if tr.empty:
            lines.append(f"{name:<34}{0:>7}   (no trades)")
            continue
        d = tr[tr.excess_pct.notna()]
        cs = clustered_stats(tr)
        lines.append(
            f"{name:<34}{len(d):>7}{d.hold_days.mean():>6.1f}d"
            f"{d.net_pct.mean():>+7.2f}%{d.bench_pct.mean():>+7.2f}%"
            f"{cs['mean']:>+8.2f}%{cs['t']:>+7.2f}{cs['n']:>7}"
            f"{(d.net_pct > 0).mean() * 100:>6.1f}%"
            + ("  SIGNIFICANT" if abs(cs["t"]) > 2 else "")
        )
    lines.append("")
    lines.append("net    = after friction.  bench = benchmark over the REALISED holding window.")
    lines.append("excess = net - bench, on outcome-blind non-overlapping trades, month-clustered.")
    lines.append("indep  = trades surviving the non-overlap filter (the real sample size).")
    lines.append("A high 'net' with zero 'excess' means the regime earned market beta, not alpha.")
    return "\n".join(lines)


def exit_reason_breakdown(trades: pd.DataFrame) -> str:
    if trades.empty:
        return "no trades"
    lines = [f"{'reason':<14}{'n':>7}{'share':>8}{'avg net':>10}{'avg hold':>10}", "-" * 50]
    for reason, grp in trades.groupby("reason"):
        lines.append(
            f"{reason:<14}{len(grp):>7}{len(grp) / len(trades) * 100:>7.1f}%"
            f"{grp.net_pct.mean():>+9.2f}%{grp.hold_days.mean():>9.1f}d"
        )
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
