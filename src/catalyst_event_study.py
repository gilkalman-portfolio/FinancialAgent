"""
News-Catalyst Event-Study Measurement.

Measures whether a momentum/supertrend technical hit *plus* fresh news of a
given sentiment direction predicts forward returns differently than a hit
with no news attached at all — the no-news group is the control, not noise
to discard. This is the prerequisite research step before ever considering a
news-driven trading signal; see the parked IBKR item in CLAUDE.md's Incident
Archive, 2026-08-25. Nothing in this module writes to order_manager,
execution_engine, or ibkr_worker, and nothing here is called from the
scheduler — it is a standalone analysis module, run on demand (like
src/trigger_backtest.py) once enough WATCH data has accumulated.

Data source: signal_type='WATCH' rows in `forward_signals`, written by
src.forward_signals.record_watch_signals() (wired into scheduler.py's
momentum/supertrend monitor threads). Direction is read from `ai_verdict`
('positive' / 'negative' / 'neutral' / 'no_news' — see record_watch_signals()
for why that column, not a new one, holds it).

In the same family as src/trigger_backtest.py, run_exit_simulation.py, and
src/signal_library.py — this module reuses their MATH (excess-vs-benchmark,
clustered t-stats), applied to live-captured rows instead of a historical
bar-by-bar replay, rather than reinventing either concept:

  1. Abnormal returns, not raw returns — every reported effect is return
     relative to a benchmark (default SPY) over the identical wall-clock
     window, the same discipline forward_signals.benchmark_excess() and
     trigger_backtest.py's whole reporting layer already use. A raw win rate
     measures market beta, not a signal — see CLAUDE.md's 2026-08-05
     Live-Readiness Audit for the three months that hid.

  2. Cross-sectional clustering correction — trigger_backtest.clustered_stats()
     corrects for the SAME ticker's overlapping holding periods (time-axis
     clustering) by averaging into monthly buckets before taking a t-stat.
     The clustering problem here is different in axis, not in kind: multiple
     DIFFERENT tickers sharing a catalyst date (earnings season, a
     market-wide news day) have correlated abnormal returns, so naive
     per-observation t-tests overstate significance (Kolari & Pynnonen 2010).
     _date_clustered_stats() below applies the identical technique — average
     same-bucket observations into one point, then run the t-stat on the
     bucket means — just bucketed by calendar date across tickers instead of
     by month within one ticker.

  3. Placebo/control-date test — compares the measured effect at real
     WATCH event dates against random non-catalyst dates for the SAME
     tickers (excluding a buffer window around every real event for that
     ticker), to confirm a measured effect isn't just generic drift/beta that
     would show up on any day for these same names. Pattern observed in
     matthias-wyss/iti-8k-analysis (GitHub) — not previously done anywhere
     in this codebase.

  4. Documented, uncorrectable caveat (see also
     src/forward_signals.py::record_watch_signals docstring): Polygon's
     per-article sentiment label is a third-party black box this project does
     not independently validate — read every result here as "does Polygon's
     sentiment model predict returns", not "does news sentiment predict
     returns" in some model-independent sense. Separately, an LLM-computed
     sentiment label on already-published news carries a theoretical
     look-ahead-bias risk if the underlying model's training data included
     the article's aftermath (arXiv:2309.17322) — unverifiable from our side,
     documented here rather than "fixed" because there is no fix available to
     this project.

Run (once enough WATCH rows have accumulated — this needs real history, not
a single live-verification row):
    .venv\\Scripts\\python.exe -c \
        "from src.catalyst_event_study import direction_report, format_direction_report; \
         print(format_direction_report(direction_report(days=90)))"
"""

from __future__ import annotations

import logging
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from src.database import get_connection

logger = logging.getLogger(__name__)

HORIZONS = (1, 2, 3, 7, 14, 30)
DEFAULT_BENCHMARK = "SPY"


# ─────────────────────────────────────────────────────────────────────────
# Shared stats primitives (mirrors trigger_backtest.stats() / clustered_stats())
# ─────────────────────────────────────────────────────────────────────────

def _t_stat(values: list) -> dict:
    """n / mean / sd / t for a 1-D sample. Same shape as trigger_backtest.stats()."""
    xs = [v for v in values if v is not None]
    n = len(xs)
    if n < 3:
        return {"n": n, "mean": None, "sd": None, "t": None}
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return {"n": n, "mean": mean, "sd": sd, "t": t}


def _welch_t(a: list, b: list) -> Optional[float]:
    """Welch's t-statistic for two independent samples of unequal variance —
    used by placebo_test() to compare the real-event distribution against the
    placebo-date distribution."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return 0.0
    return (ma - mb) / se


def _date_clustered_stats(rows: list, col: str) -> dict:
    """Cross-sectional clustering correction (methodology upgrade #2).

    Averages same-calendar-date observations into one point per date before
    computing a t-stat, so tickers sharing a catalyst date don't each count as
    independent evidence (Kolari & Pynnonen 2010). Extends
    trigger_backtest.clustered_stats()'s exact idea — bucket into a coarser
    group, then run stats() on the bucket means — to the cross-ticker axis;
    that function buckets by month per-ticker to fix time-overlap, this
    buckets by calendar date across tickers to fix cross-sectional overlap.

    `rows` items must have a "date" key (a date-like object, used as the
    grouping key) and the `col` key holding the value to average.
    """
    buckets: dict = defaultdict(list)
    for r in rows:
        v = r.get(col)
        if v is None:
            continue
        buckets[r["date"]].append(v)
    if not buckets:
        return {"n": 0, "dates": 0, "mean": None, "t": None}
    means = [sum(vs) / len(vs) for vs in buckets.values()]
    s = _t_stat(means)
    return {
        "n": sum(len(vs) for vs in buckets.values()),
        "dates": len(buckets),
        "mean": s["mean"],
        "t": s["t"],
    }


def _forward_pct_return(close, start_ts, days: int) -> Optional[float]:
    """Percent change from the bar at/after `start_ts` to the first bar >=
    start_ts + days. Deliberately the same algorithm as
    trigger_backtest._forward_return() — reused as a pattern (per CLAUDE.md's
    instruction to reuse trigger_backtest.py's math), not imported directly,
    because that function operates on trigger_backtest's own historical-replay
    DataFrame shape, not on the price series this module fetches live."""
    i0 = close.index.searchsorted(start_ts)
    i1 = close.index.searchsorted(start_ts + _timedelta_days(days))
    if i0 >= len(close) or i1 >= len(close) or i1 <= i0:
        return None
    p0, p1 = float(close.iloc[i0]), float(close.iloc[i1])
    if p0 <= 0:
        return None
    return (p1 / p0 - 1.0) * 100.0


def _timedelta_days(days: int):
    import pandas as pd
    return pd.Timedelta(days=days)


def _normalize_close_index(close):
    """tz-naive, midnight-normalized DatetimeIndex — same normalization as
    forward_signals.benchmark_excess() and trigger_backtest.load_bars()."""
    import pandas as pd
    idx = pd.DatetimeIndex(close.index)
    close.index = (idx.tz_convert("UTC").tz_localize(None) if idx.tz is not None
                   else idx).normalize()
    return close


def _fetch_ticker_close(ticker: str, lookback_days: int):
    """Daily close series for `ticker` going back `lookback_days`. Returns
    None on any failure — every caller must treat that as "skip this ticker",
    matching every other yfinance call site in this codebase."""
    import yfinance as yf

    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    try:
        hist = yf.Ticker(ticker).history(start=start, auto_adjust=True)
    except Exception as e:
        logger.debug(f"[catalyst_event_study] price fetch failed for {ticker}: {e}")
        return None
    if hist is None or hist.empty:
        return None
    return _normalize_close_index(hist["Close"])


def _fetch_benchmark_close(benchmark: str, lookback_days: int):
    return _fetch_ticker_close(benchmark, lookback_days + max(HORIZONS) + 10)


# ─────────────────────────────────────────────────────────────────────────
# Data access
# ─────────────────────────────────────────────────────────────────────────

def _watch_rows(days: int) -> list:
    """Raw WATCH rows (ticker, signal_ts, direction, return_Xd_pct...) within
    the lookback window. `direction` defaults to 'no_news' for any row
    somehow missing ai_verdict, so downstream grouping never silently drops
    an observation into a NULL bucket."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cols = ", ".join(f"return_{h}d_pct" for h in HORIZONS)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT ticker, signal_ts, ai_verdict AS direction, {cols} "
            "FROM forward_signals WHERE signal_type = 'WATCH' AND signal_ts >= ?",
            (cutoff,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["direction"] = d.get("direction") or "no_news"
        out.append(d)
    return out


# ─────────────────────────────────────────────────────────────────────────
# Methodology upgrade #1: abnormal returns vs benchmark
# ─────────────────────────────────────────────────────────────────────────

def compute_abnormal_returns(days: int = 90, benchmark: str = DEFAULT_BENCHMARK) -> list:
    """One row per WATCH observation with abnormal (excess-vs-benchmark)
    return at each horizon in HORIZONS. This is the per-observation table
    every aggregate below is built from.

    Returns [] when there is no WATCH data yet or the benchmark can't be
    fetched — callers must treat that as "not enough data", not an error.
    """
    rows = _watch_rows(days)
    if not rows:
        return []

    bench_close = _fetch_benchmark_close(benchmark, days)
    if bench_close is None:
        logger.warning(
            f"[catalyst_event_study] benchmark {benchmark} fetch failed — "
            "abnormal returns unavailable this run"
        )
        return []

    import pandas as pd

    out = []
    for r in rows:
        try:
            sig_date = pd.Timestamp(r["signal_ts"][:10]).normalize()
        except Exception:
            continue
        rec = {"ticker": r["ticker"], "direction": r["direction"], "date": sig_date}
        for h in HORIZONS:
            raw = r.get(f"return_{h}d_pct")
            if raw is None:
                rec[f"abn_{h}"] = None
                continue
            bench_ret = _forward_pct_return(bench_close, sig_date, h)
            rec[f"abn_{h}"] = (raw - bench_ret) if bench_ret is not None else None
        out.append(rec)
    return out


# ─────────────────────────────────────────────────────────────────────────
# Methodology upgrade #2: cross-sectional clustering, applied per direction
# ─────────────────────────────────────────────────────────────────────────

def direction_report(days: int = 90, benchmark: str = DEFAULT_BENCHMARK,
                      horizons=HORIZONS) -> dict:
    """Per (sentiment direction, horizon): naive n/mean/t plus the
    cross-sectional date-clustered n/mean/t of the abnormal return.

    Always read the clustered figure, never the naive one — the same
    discipline trigger_backtest.py established for its own (time-axis)
    clustering problem applies here for the cross-sectional one.
    """
    rows = compute_abnormal_returns(days, benchmark)
    if not rows:
        return {"benchmark": benchmark, "window_days": days, "directions": {}}

    by_direction: dict = defaultdict(list)
    for r in rows:
        by_direction[r["direction"]].append(r)

    result = {"benchmark": benchmark, "window_days": days, "directions": {}}
    for direction, drows in by_direction.items():
        per_horizon = {}
        for h in horizons:
            naive = _t_stat([r.get(f"abn_{h}") for r in drows])
            clustered = _date_clustered_stats(drows, f"abn_{h}")
            per_horizon[h] = {"naive": naive, "clustered": clustered}
        result["directions"][direction] = per_horizon
    return result


def format_direction_report(d: dict) -> str:
    """Text report, same table-per-horizon shape as trigger_backtest.report()."""
    directions = d.get("directions") or {}
    if not directions:
        return "no WATCH data in window"
    lines = [f"===== News-Catalyst Event Study — abnormal return vs {d['benchmark']} "
             f"({d['window_days']}d window) ====="]
    for direction, per_horizon in sorted(directions.items()):
        lines.append(f"\n--- direction: {direction} ---")
        lines.append(f"{'horizon':>8}{'n(naive)':>10}{'t(naive)':>10}"
                      f"{'n(dates)':>10}{'mean':>9}{'t(clustered)':>14}")
        for h, s in sorted(per_horizon.items()):
            naive, clus = s["naive"], s["clustered"]
            naive_t = f"{naive['t']:+.2f}" if naive["t"] is not None else "  n/a"
            clus_mean = f"{clus['mean']:+.2f}%" if clus["mean"] is not None else "  n/a"
            clus_t = f"{clus['t']:+.2f}" if clus["t"] is not None else " n/a"
            lines.append(
                f"{h:>7}d{naive['n']:>10}{naive_t:>10}"
                f"{clus['dates']:>10}{clus_mean:>9}{clus_t:>14}"
            )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Methodology upgrade #3: placebo / control-date test
# ─────────────────────────────────────────────────────────────────────────

def placebo_test(
    days: int = 90,
    horizon: int = 7,
    buffer_days: int = 5,
    n_placebo_per_event: int = 3,
    seed: int = 42,
) -> dict:
    """
    Compare the raw forward return measured at real WATCH event dates against
    random non-catalyst dates for the SAME tickers, excluding a
    `buffer_days`-wide window around every real event for that ticker so a
    placebo date can never accidentally land inside a real move's aftermath.
    Confirms the measured effect isn't just generic drift/beta that would show
    up on any day for these same names — pattern observed in
    matthias-wyss/iti-8k-analysis (GitHub), not previously done anywhere in
    this codebase.

    `seed` makes the random placebo-date sample reproducible run to run.

    Returns {"status": "insufficient_data"} when there are fewer than 3
    matured real observations at `horizon` — the placebo comparison is
    meaningless below that. Otherwise returns real/placebo n/mean/t plus a
    Welch's-t two-sample comparison (`diff_t_stat`) and
    `distinguishable_from_placebo` (True when |diff_t_stat| > 2.0).
    """
    import pandas as pd

    rows = _watch_rows(days)
    ret_col = f"return_{horizon}d_pct"

    real_by_ticker: dict = defaultdict(list)
    for r in rows:
        try:
            d = pd.Timestamp(r["signal_ts"][:10]).normalize()
        except Exception:
            continue
        real_by_ticker[r["ticker"]].append(d)

    real_returns = [r[ret_col] for r in rows if r.get(ret_col) is not None]
    if len(real_returns) < 3:
        return {"status": "insufficient_data", "real_n": len(real_returns)}

    rng = random.Random(seed)
    placebo_returns: list = []
    price_cache: dict = {}
    buffer_td = pd.Timedelta(days=buffer_days)

    for ticker, event_dates in real_by_ticker.items():
        close = price_cache.get(ticker)
        if close is None:
            close = _fetch_ticker_close(ticker, days + horizon + buffer_days * 4)
            price_cache[ticker] = close
        if close is None or close.empty:
            continue

        excluded = set()
        for d in event_dates:
            lo, hi = d - buffer_td, d + buffer_td
            for ts in close.index:
                if lo <= ts <= hi:
                    excluded.add(ts)

        eligible = [ts for ts in close.index if ts not in excluded]
        if not eligible:
            continue

        n_draw = n_placebo_per_event * len(event_dates)
        for _ in range(n_draw):
            d0 = rng.choice(eligible)
            ret = _forward_pct_return(close, d0, horizon)
            if ret is not None:
                placebo_returns.append(ret)

    real_stats = _t_stat(real_returns)
    placebo_stats = _t_stat(placebo_returns)
    diff_t = None
    if real_stats["n"] >= 3 and placebo_stats["n"] >= 3:
        diff_t = _welch_t(real_returns, placebo_returns)

    return {
        "status": "ok",
        "horizon": horizon,
        "buffer_days": buffer_days,
        "real": real_stats,
        "placebo": placebo_stats,
        "diff_t_stat": diff_t,
        "distinguishable_from_placebo": (abs(diff_t) > 2.0) if diff_t is not None else None,
    }
