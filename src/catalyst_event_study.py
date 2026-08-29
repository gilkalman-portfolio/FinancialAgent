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

  5. Survivorship check (added 2026-08-26, same external design review as #4):
     the Telegram enrichment in auto_watchlist_agent.py only ever shows for
     tickers that already passed auto_watchlist_agent's own filters (0-10 of
     the ~270-290 hits/cycle) -- so any sentiment/return relationship found
     over the FULL WATCH population could be an artifact of that selection,
     not evidence sentiment itself predicts anything. direction_report() and
     compute_abnormal_returns() both take promoted_only=True to restrict to
     exactly that promoted subset -- run both and compare before trusting
     either one alone.

  6. Within-ticker serial-correlation correction (added 2026-08-29, same
     external design review as #2/#3 -- flagged as a gap the review found,
     not fixed at the time): _date_clustered_stats() (#2 above) only
     corrects for DIFFERENT tickers sharing a calendar date. It does nothing
     for the SAME ticker hit twice days apart, whose forward-return windows
     (e.g. two overlapping 7-day windows) share underlying price history and
     are serially correlated -- every row still lands on its own distinct
     date, so the cross-sectional correction sees no cluster to collapse.
     _block_bootstrap_stats() fixes this on the other axis: it resamples
     whole TICKERS with replacement (every observation belonging to a drawn
     ticker moves together as one block) rather than individual rows, so a
     ticker hit 10 times only ever counts as ONE independent draw's worth of
     uncertainty, not 10. Reported alongside (never in place of) the naive/
     clustered figures in direction_report(day, block_bootstrap=True) under
     a "block_bootstrap" key, opt-in because the resampling cost is real and
     usually unnecessary (only matters once a ticker recurs in the window).
     Chosen over a parametric Newey-West HAC correction because per-ticker
     observation counts and date-spacing are too irregular here for one
     lag-truncation parameter to describe honestly; a cluster bootstrap makes
     no assumption about the shape of the within-ticker correlation.

  7. Liquidity-matched control group (added 2026-08-29, same external design
     review as #6): the no-news control group is not otherwise matched on
     liquidity to the news-accompanied group -- if news-covered tickers skew
     toward larger/more-liquid names, a measured return difference could be
     a pure size/liquidity effect misread as a news effect.
     liquidity_matched_direction_report() fixes this with a matched-pairs
     design: each non-'no_news' observation is paired via nearest-neighbor
     on ADV (average daily dollar volume, the same liquidity metric
     monitoring_queue.py's real-time-monitoring gate already computes) against
     an unused no-news observation within a log10(ADV) caliper, dropping any
     observation with no close-enough match rather than force-matching it.
     Deliberately a separate function from direction_report(), not a mode
     flag on it -- a matched comparison reports two clustered figures per
     horizon (treatment vs. its matched control), a different shape from the
     unmatched per-direction table.

Run (once enough WATCH rows have accumulated — this needs real history, not
a single live-verification row):
    .venv\\Scripts\\python.exe -c \
        "from src.catalyst_event_study import direction_report, format_direction_report; \
         print(format_direction_report(direction_report(days=90)))"

Run BOTH the full population and the promoted-only subset and compare, per
the 2026-08-26 IdeaDistill review (see #5 below):
    .venv\\Scripts\\python.exe -c \
        "from src.catalyst_event_study import direction_report, format_direction_report; \
         print(format_direction_report(direction_report(days=90))); \
         print(format_direction_report(direction_report(days=90, promoted_only=True)))"
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


def _block_bootstrap_stats(rows: list, col: str, n_boot: int = 1000, seed: int = 42) -> dict:
    """Ticker-blocked bootstrap correction for WITHIN-ticker serial
    correlation (methodology upgrade #6 in the module docstring) -- a
    different axis from _date_clustered_stats() above, which only collapses
    rows sharing an exact calendar date across DIFFERENT tickers. Two WATCH
    hits on the SAME ticker days apart have overlapping forward-return
    windows that share the same underlying price history and are not
    independent evidence; when the two hits fall on different dates (the
    common case) _date_clustered_stats() provides zero protection against
    that, since nothing in it ever looks at which ticker a row belongs to.

    Resamples whole TICKERS with replacement (every observation belonging to
    a drawn ticker moves together as one block, preserving whatever
    within-ticker correlation exists) rather than resampling individual
    rows -- the standard fix for clustered/serially-correlated panel data.
    Chosen over a parametric Newey-West HAC correction because per-ticker
    observation counts and date-spacing are too irregular here for a single
    lag-truncation parameter to describe honestly; a cluster bootstrap makes
    no assumption about the shape or decay rate of the within-ticker
    correlation, only that observations sharing a ticker should resample
    together.

    Returns the naive n/mean/sd/t (same shape as _t_stat(), for direct
    comparison) plus bootstrap_se / bootstrap_t (t computed from the
    ORIGINAL sample mean divided by the bootstrap standard error -- the
    resampled means are only used to estimate that standard error, not as
    the point estimate itself) and n_tickers (the true independent-cluster
    count, which is what actually bounds how much this can be trusted).
    `seed` makes the resample reproducible run to run, same convention as
    placebo_test().

    Fewer than 3 tickers or fewer than 3 raw observations returns
    bootstrap_se/bootstrap_t as None -- too few clusters to resample
    meaningfully, same "can't compute, don't fabricate" floor _t_stat() uses.
    """
    by_ticker: dict = defaultdict(list)
    for r in rows:
        v = r.get(col)
        if v is None:
            continue
        by_ticker[r["ticker"]].append(v)

    naive = _t_stat([v for vs in by_ticker.values() for v in vs])
    tickers = list(by_ticker.keys())
    n_tickers = len(tickers)

    if n_tickers < 3 or naive["n"] < 3:
        return {**naive, "bootstrap_se": None, "bootstrap_t": None, "n_tickers": n_tickers}

    rng = random.Random(seed)
    boot_means = []
    for _ in range(n_boot):
        sample: list = []
        for _ in range(n_tickers):
            sample.extend(by_ticker[tickers[rng.randrange(n_tickers)]])
        if sample:
            boot_means.append(sum(sample) / len(sample))

    if len(boot_means) < 2:
        return {**naive, "bootstrap_se": None, "bootstrap_t": None, "n_tickers": n_tickers}

    boot_mean = sum(boot_means) / len(boot_means)
    boot_var = sum((m - boot_mean) ** 2 for m in boot_means) / (len(boot_means) - 1)
    boot_se = math.sqrt(boot_var)
    boot_t = (naive["mean"] / boot_se) if boot_se > 0 else 0.0

    return {**naive, "bootstrap_se": boot_se, "bootstrap_t": boot_t, "n_tickers": n_tickers}


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


def _fetch_ticker_adv(ticker: str, lookback_days: int = 20) -> Optional[float]:
    """Average daily dollar volume (Close * Volume), trailing `lookback_days`
    -- the exact liquidity metric monitoring_queue.py's real-time-monitoring
    gate already computes (_liquid(): `(hist["Close"] * hist["Volume"]).
    tail(20).mean()` over a 1-month history), reused here rather than
    inventing a second ADV definition in this codebase. Returns None on any
    failure or a non-positive result, same "skip this ticker" contract as
    _fetch_ticker_close()."""
    import yfinance as yf

    try:
        hist = yf.Ticker(ticker).history(period="1mo", auto_adjust=False)
    except Exception as e:
        logger.debug(f"[catalyst_event_study] ADV fetch failed for {ticker}: {e}")
        return None
    if hist is None or hist.empty:
        return None
    adv = float((hist["Close"] * hist["Volume"]).tail(lookback_days).mean())
    return adv if adv > 0 else None


# ─────────────────────────────────────────────────────────────────────────
# Data access
# ─────────────────────────────────────────────────────────────────────────

_PROMOTED_ALERT_TYPES = ("auto_wl_momentum", "auto_wl_supertrend")
_PROMOTED_MATCH_WINDOW_MINUTES = 30


def _watch_rows(days: int, promoted_only: bool = False) -> list:
    """Raw WATCH rows (ticker, signal_ts, direction, return_Xd_pct...) within
    the lookback window. `direction` defaults to 'no_news' for any row
    somehow missing ai_verdict, so downstream grouping never silently drops
    an observation into a NULL bucket.

    `promoted_only=True` restricts to WATCH rows whose ticker was ALSO
    promoted to the watchlist (an auto_wl_momentum/auto_wl_supertrend
    watchlist_alerts row within +-30 min of the WATCH row's signal_ts) --
    added 2026-08-26 per an external design review (IdeaDistill panel):
    Claude's argument in that review was that any apparent sentiment/return
    relationship measured over the full hit population is partly a
    survivorship artifact, since the enriched Telegram notification only
    ever shows for the small already-filtered promoted subset (0-10/cycle
    out of ~270-290 hits) -- comparing direction_report(promoted_only=True)
    against the unrestricted default is the direct way to check whether the
    relationship holds, strengthens, or is an artifact of that selection.
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cols = ", ".join(f"return_{h}d_pct" for h in HORIZONS)
    promoted_clause = ""
    if promoted_only:
        placeholders = ", ".join("?" for _ in _PROMOTED_ALERT_TYPES)
        # datetime(wa.sent_at) on the left is not cosmetic: SQLite's
        # datetime()/'+N minutes' functions normalize their OUTPUT to a
        # space-separated 'YYYY-MM-DD HH:MM:SS' form, but every timestamp
        # this project stores comes from Python's datetime.isoformat() (a
        # 'T' separator, e.g. '2026-06-01T10:05:00' -- see record_signal()/
        # watchlist_save_alert()). Comparing that raw 'T' string against a
        # datetime()-computed space-separated bound is a plain string
        # comparison that silently returns the wrong answer (the 'T' vs ' '
        # byte alone decides the ordering, before the actual time value is
        # ever considered) -- caught by test_promoted_ticker_included_when_
        # promoted_only failing during development, not a live incident.
        # Wrapping wa.sent_at in datetime() too normalizes both sides to the
        # same format before comparing.
        promoted_clause = f"""
            AND EXISTS (
                SELECT 1 FROM watchlist_alerts wa
                WHERE wa.ticker = forward_signals.ticker
                  AND wa.alert_type IN ({placeholders})
                  AND datetime(wa.sent_at) BETWEEN
                      datetime(forward_signals.signal_ts, '-{_PROMOTED_MATCH_WINDOW_MINUTES} minutes')
                      AND datetime(forward_signals.signal_ts, '+{_PROMOTED_MATCH_WINDOW_MINUTES} minutes')
            )
        """
    params = (cutoff, *(_PROMOTED_ALERT_TYPES if promoted_only else ()))
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT ticker, signal_ts, ai_verdict AS direction, {cols} "
            f"FROM forward_signals WHERE signal_type = 'WATCH' AND signal_ts >= ?{promoted_clause}",
            params,
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

def compute_abnormal_returns(days: int = 90, benchmark: str = DEFAULT_BENCHMARK,
                              promoted_only: bool = False) -> list:
    """One row per WATCH observation with abnormal (excess-vs-benchmark)
    return at each horizon in HORIZONS. This is the per-observation table
    every aggregate below is built from.

    `promoted_only` — see _watch_rows() docstring: restricts to WATCH rows
    for tickers that were also promoted to the watchlist, to directly test
    whether a measured sentiment/return relationship survives outside the
    full (unfiltered) hit population or is a survivorship artifact of that
    selection.

    Returns [] when there is no WATCH data yet or the benchmark can't be
    fetched — callers must treat that as "not enough data", not an error.
    """
    rows = _watch_rows(days, promoted_only=promoted_only)
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
                      horizons=HORIZONS, promoted_only: bool = False,
                      block_bootstrap: bool = False, n_boot: int = 1000,
                      boot_seed: int = 42) -> dict:
    """Per (sentiment direction, horizon): naive n/mean/t plus the
    cross-sectional date-clustered n/mean/t of the abnormal return.

    Always read the clustered figure, never the naive one — the same
    discipline trigger_backtest.py established for its own (time-axis)
    clustering problem applies here for the cross-sectional one.

    Run this twice — once with promoted_only=False (the full hit
    population) and once with promoted_only=True (only tickers that were
    also promoted to the watchlist, i.e. the population that actually shows
    the sentiment tag in a real Telegram message) — and compare. Per the
    2026-08-26 IdeaDistill design review: if a sentiment/return relationship
    only shows up in the promoted-only subset and not the full population,
    that's evidence it's a survivorship artifact of the selection filters
    rather than a real effect of sentiment itself.

    `block_bootstrap=True` additionally reports a ticker-blocked bootstrap
    correction (see _block_bootstrap_stats(), module docstring #6) per
    (direction, horizon) under a "block_bootstrap" key — the within-ticker
    serial-correlation analog of the cross-ticker "clustered" figure above,
    for the same reason: two WATCH hits on the same ticker with overlapping
    forward-return windows are not independent evidence, and
    _date_clustered_stats() only ever catches that when the two hits happen
    to also share an exact date. Off by default (n_boot resamples per
    direction/horizon is real, usually-unneeded cost) — opt in once a ticker
    is likely to recur in the window.
    """
    rows = compute_abnormal_returns(days, benchmark, promoted_only=promoted_only)
    if not rows:
        return {"benchmark": benchmark, "window_days": days, "promoted_only": promoted_only,
                "directions": {}}

    by_direction: dict = defaultdict(list)
    for r in rows:
        by_direction[r["direction"]].append(r)

    result = {"benchmark": benchmark, "window_days": days, "promoted_only": promoted_only,
              "directions": {}}
    for direction, drows in by_direction.items():
        per_horizon = {}
        for h in horizons:
            naive = _t_stat([r.get(f"abn_{h}") for r in drows])
            clustered = _date_clustered_stats(drows, f"abn_{h}")
            entry = {"naive": naive, "clustered": clustered}
            if block_bootstrap:
                entry["block_bootstrap"] = _block_bootstrap_stats(
                    drows, f"abn_{h}", n_boot=n_boot, seed=boot_seed)
            per_horizon[h] = entry
        result["directions"][direction] = per_horizon
    return result


def format_direction_report(d: dict) -> str:
    """Text report, same table-per-horizon shape as trigger_backtest.report()."""
    directions = d.get("directions") or {}
    if not directions:
        return "no WATCH data in window"
    scope = "PROMOTED-ONLY (watchlist-added)" if d.get("promoted_only") else "full hit population"
    lines = [f"===== News-Catalyst Event Study — abnormal return vs {d['benchmark']} "
             f"({d['window_days']}d window, {scope}) ====="]
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
            bb = s.get("block_bootstrap")
            if bb and bb.get("bootstrap_t") is not None:
                lines.append(
                    f"          block-bootstrap: n_tickers={bb['n_tickers']} "
                    f"se={bb['bootstrap_se']:.3f} t={bb['bootstrap_t']:+.2f}"
                )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Liquidity-matched control group (module docstring item #7) — placed here,
# ahead of the pre-existing placebo-test section (#3) below, since it builds
# directly on direction_report()/format_direction_report() just above it.
# ─────────────────────────────────────────────────────────────────────────
# If news-accompanied hits skew toward larger/more-liquid tickers than the
# no-news control group, any measured return difference between them could be
# a pure size/liquidity effect misread as a news effect — flagged by the
# 2026-08-26 external design review alongside the serial-correlation gap
# above. Fixed with a matched-pairs design (nearest-neighbor on ADV, the same
# liquidity metric monitoring_queue.py's real-time-monitoring gate already
# computes), not a full propensity-score-matching framework — see CLAUDE.md.

def _nearest_neighbor_match(treatment: list, control: list, adv_key: str = "adv",
                             caliper: float = 0.5) -> tuple:
    """Greedy 1:1 nearest-neighbor match without replacement, in log10(ADV)
    space — dollar volume spans orders of magnitude across the scan universe
    (micro-caps to mega-caps), so matching on raw-dollar distance would let a
    huge-cap be called the "nearest" match for a micro-cap; log-space
    distance treats a doubling of ADV the same regardless of where it
    starts, the standard way this kind of covariate gets matched.

    Processes treatment rows from LARGEST adv to smallest — the extremes have
    the fewest plausible matches, so giving them first pick of the (shared,
    used-once) control pool avoids a large-ADV treatment row losing its only
    plausible match to a mid-range treatment row processed first. A
    treatment row with no unused control inside `caliper` log10(ADV) units
    (0.5 ~= a ~3x ADV band) is dropped rather than force-matched to a poor
    comparison — standard practice in matched-pairs designs, and consistent
    with this module's existing "skip, don't fabricate" discipline. Rows
    missing an `adv_key` value (an ADV fetch failure) are excluded from both
    pools before matching starts.

    Returns (matched_treatment_rows, matched_control_rows), same length and
    index-aligned — matched_treatment_rows[i] is paired with
    matched_control_rows[i].
    """
    treat_ok = [r for r in treatment if r.get(adv_key)]
    ctrl_pool = [r for r in control if r.get(adv_key)]

    matched_treat: list = []
    matched_ctrl: list = []
    used = [False] * len(ctrl_pool)
    for t in sorted(treat_ok, key=lambda r: r[adv_key], reverse=True):
        t_log = math.log10(t[adv_key])
        best_i, best_dist = None, None
        for i, c in enumerate(ctrl_pool):
            if used[i]:
                continue
            dist = abs(math.log10(c[adv_key]) - t_log)
            if best_dist is None or dist < best_dist:
                best_i, best_dist = i, dist
        if best_i is not None and best_dist <= caliper:
            used[best_i] = True
            matched_treat.append(t)
            matched_ctrl.append(ctrl_pool[best_i])
    return matched_treat, matched_ctrl


def _adv_balance(treat_all: list, ctrl_all: list, treat_matched: list, ctrl_matched: list,
                  adv_key: str = "adv") -> dict:
    """Mean ADV of each group before and after matching — the diagnostic that
    proves (or disproves) the match actually balanced the covariate, rather
    than just asserting that it did. Standard reporting practice for any
    matched-pairs design."""
    def _mean(rs):
        vals = [r[adv_key] for r in rs if r.get(adv_key)]
        return sum(vals) / len(vals) if vals else None

    return {
        "treatment_mean_adv_before": _mean(treat_all),
        "control_mean_adv_before": _mean(ctrl_all),
        "treatment_mean_adv_after": _mean(treat_matched),
        "control_mean_adv_after": _mean(ctrl_matched),
    }


def liquidity_matched_direction_report(days: int = 90, benchmark: str = DEFAULT_BENCHMARK,
                                        horizons=HORIZONS, caliper: float = 0.5,
                                        adv_lookback_days: int = 20,
                                        promoted_only: bool = False) -> dict:
    """direction_report()'s no-news comparison, restricted to a liquidity-
    matched subset: every non-'no_news' direction is compared only against
    the no-news rows whose ADV is close to its matched treatment row's ADV
    (see _nearest_neighbor_match()), so a measured return difference can't
    simply reflect news-covered tickers skewing larger/more liquid than the
    no-news pool.

    One ADV fetch per unique ticker in the window (cached — many WATCH rows
    share a ticker), on top of compute_abnormal_returns()'s existing
    benchmark fetch.

    Returns {"benchmark", "window_days", "caliper", "directions": {direction:
    {"n_matched_pairs", "adv_balance", "horizons": {h: {"treatment":
    <clustered stats>, "control": <clustered stats>}}}}} — deliberately a
    DIFFERENT shape from direction_report()'s output (a matched comparison
    has two clustered figures per horizon, not one), so this is a new
    function rather than a mode flag on direction_report().
    """
    rows = compute_abnormal_returns(days, benchmark, promoted_only=promoted_only)
    if not rows:
        return {"benchmark": benchmark, "window_days": days, "caliper": caliper, "directions": {}}

    adv_cache: dict = {}
    for r in rows:
        tk = r["ticker"]
        if tk not in adv_cache:
            adv_cache[tk] = _fetch_ticker_adv(tk, adv_lookback_days)
        r["adv"] = adv_cache[tk]

    no_news = [r for r in rows if r["direction"] == "no_news"]
    by_direction: dict = defaultdict(list)
    for r in rows:
        if r["direction"] != "no_news":
            by_direction[r["direction"]].append(r)

    result = {"benchmark": benchmark, "window_days": days, "caliper": caliper, "directions": {}}
    for direction, drows in by_direction.items():
        matched_treat, matched_ctrl = _nearest_neighbor_match(drows, no_news, caliper=caliper)
        per_horizon = {}
        for h in horizons:
            per_horizon[h] = {
                "treatment": _date_clustered_stats(matched_treat, f"abn_{h}"),
                "control": _date_clustered_stats(matched_ctrl, f"abn_{h}"),
            }
        result["directions"][direction] = {
            "n_matched_pairs": len(matched_treat),
            "adv_balance": _adv_balance(drows, no_news, matched_treat, matched_ctrl),
            "horizons": per_horizon,
        }
    return result


def format_liquidity_matched_report(d: dict) -> str:
    """Text report for liquidity_matched_direction_report(), same rendering
    convention as format_direction_report()."""
    directions = d.get("directions") or {}
    if not directions:
        return "no WATCH data in window"
    lines = [f"===== News-Catalyst Event Study — liquidity-matched vs {d['benchmark']} "
             f"({d['window_days']}d window, caliper={d['caliper']}) ====="]
    for direction, dd in sorted(directions.items()):
        bal = dd["adv_balance"]
        lines.append(f"\n--- direction: {direction} (n_matched_pairs={dd['n_matched_pairs']}) ---")
        lines.append(
            f"  ADV before: treatment=${(bal['treatment_mean_adv_before'] or 0)/1e6:.1f}M "
            f"control=${(bal['control_mean_adv_before'] or 0)/1e6:.1f}M"
        )
        lines.append(
            f"  ADV after:  treatment=${(bal['treatment_mean_adv_after'] or 0)/1e6:.1f}M "
            f"control=${(bal['control_mean_adv_after'] or 0)/1e6:.1f}M"
        )
        lines.append(f"{'horizon':>8}{'t(treat)':>10}{'t(ctrl)':>10}")
        for h, s in sorted(dd["horizons"].items()):
            tt, ct = s["treatment"]["t"], s["control"]["t"]
            tt_s = f"{tt:+.2f}" if tt is not None else "  n/a"
            ct_s = f"{ct:+.2f}" if ct is not None else "  n/a"
            lines.append(f"{h:>7}d{tt_s:>10}{ct_s:>10}")
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
