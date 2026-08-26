"""
Historical validation of the `stock_scorer.py` WEIGHTS dict.

Open Backlog item: "Weight tuning based on backtest data — WEIGHTS dict in
stock_scorer.py is static." This module answers two questions:

1. Does each scoring component, as currently computed, actually correlate
   with forward returns? (component_report)
2. Does a data-driven reweighting of the components that ARE live levers
   today beat the current static WEIGHTS out of sample? (reweight_report)

Why this doesn't need to reconstruct anything historically
------------------------------------------------------------
Unlike src/trigger_backtest.py (which has to replay OHLCV from scratch
because the monitoring queue and composite score cannot be reconstructed
point-in-time — see that module's docstring), every historical
`scan_results` row written by `stock_scorer.score_stock()` already carries
the full per-component breakdown in `raw_data._scores` (rsi, macd, ma,
volume, momentum, short, institutional, insider, fundamentals, dcf, news,
trends), exactly as computed live at scan time. No point-in-time
reconstruction is needed — only forward returns have to be fetched, which
needs live yfinance access.

Which components are actually reweightable
-------------------------------------------
Not every WEIGHTS entry scales its own component's score (see CLAUDE.md,
"Scoring Engine" section, 2026-08-26). Concretely, `_scores[key] = weight *
frac(inputs)` for six components, so the weight can be swapped out exactly:

    EXACT_LINEAR = rsi, macd, ma, volume, short, institutional

Two more are `min(weight, raw_points)` — the weight only sets a ceiling, so
recovering the "what if the weight were higher" answer from the ceiling
alone is a floor-conservative approximation, not exact, whenever the stored
value was already sitting at the old ceiling:

    CAP_ONLY = momentum, insider

Two are computed from hardcoded literals with NO dependence on the WEIGHTS
value at all (`_score_fundamentals()` caps at a hardcoded 10, `dcf_score`
buckets are hardcoded 15/11/7/3/0). Reweighting them here is a "what if this
were wired to scale with weight" hypothetical, not a preview of current
production behavior — and even if the data recommends it, do not wire it up
in `stock_scorer.py` without also fixing `execution_engine.py`'s
independent hardcoded normalization of the same two fields (see CLAUDE.md):

    HARDCODED = fundamentals, dcf

One is fully inactive (`forecast_score` hardcoded to 0, excluded from
`core`/`core_max`) and two are bonus-band components capped by their weight
but never part of the reweightable `core` sum (`news`, `trends`) — all three
are reported on for informational IC only, never included in a candidate
reweighting of `core`.

Point-in-time discipline
-------------------------
Forward returns are measured on the SAME yfinance daily-close series used
for the entry price (never the DB's stored `price` field), so there is no
split/spinoff basis mismatch to patch around — see backtester.py's
`price_sig_adj` workaround, which exists only because it mixes a DB price
with a yfinance price.

Usage
-----
    python run_weight_tuning_backtest.py

See CLAUDE.md, "Weight Tuning Backtest" section, for full instructions —
this needs the production data/financial_agent.db (with months of
accumulated scan_results) and live yfinance access, neither of which is
available from a fresh clone or a network-sandboxed environment.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from src.trigger_backtest import CACHE_DIR, load_bars, stats  # noqa: F401 (CACHE_DIR re-exported)

logger = logging.getLogger(__name__)

DEFAULT_HORIZONS = (7, 14, 30)

# _scores dict key -> WEIGHTS dict key (they don't match 1:1 — see stock_scorer.py)
SCORE_KEY_TO_WEIGHT_KEY = {
    "rsi": "rsi", "macd": "macd", "ma": "ma", "volume": "volume",
    "momentum": "momentum", "forecast": "forecast", "short": "short_interest",
    "institutional": "institutional", "insider": "insider",
    "fundamentals": "fundamentals", "dcf": "dcf",
    "news": "news_sentiment", "trends": "trends",
}

EXACT_LINEAR = ("rsi", "macd", "ma", "volume", "short", "institutional")
CAP_ONLY = ("momentum", "insider")
HARDCODED = ("fundamentals", "dcf")
CORE_COMPONENTS = EXACT_LINEAR + CAP_ONLY + HARDCODED  # matches stock_scorer.py's `core` sum
BONUS_ONLY = ("news", "trends")  # capped by weight but never in `core`/`core_max`
INACTIVE = ("forecast",)         # excluded from `core`/`core_max` entirely


# ─────────────────────────────────────────────────────────────────────────
# Loading scored history
# ─────────────────────────────────────────────────────────────────────────

def load_scored_history(db_path: str | Path) -> pd.DataFrame:
    """One row per historical scan_results row that has a `_scores` breakdown.

    Deliberately does NOT filter by scan_runs.scan_type. backtester.py filters
    to scan_type='scheduled' because automated_scanner.py reuses the
    `explosion_score` COLUMN NAME for an unrelated catalyst metric — but that
    collision is about that one column, not about raw_data. Checking for a
    populated `_scores` dict directly is a more accurate filter: it is only
    ever written by stock_scorer.score_stock(), regardless of which scan
    triggered it (scheduled, manual, watchlist, sector) — so this also
    includes manual/watchlist scans that backtester.py's filter silently
    drops.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ticker, scanned_at, price, explosion_score AS score, raw_data "
            "FROM scan_results WHERE raw_data IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError as e:
        raise SystemExit(
            f"{db_path} has no `scan_results` table ({e}). This is a fresh/uninitialized "
            f"DB, not the production one this tool needs — see CLAUDE.md's "
            f"'Weight Tuning Backtest' section."
        ) from e
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        try:
            raw = json.loads(r["raw_data"])
        except (json.JSONDecodeError, TypeError):
            continue
        sc = raw.get("_scores")
        if not sc or not all(k in sc for k in SCORE_KEY_TO_WEIGHT_KEY):
            continue  # not a stock_scorer.score_stock() row

        rec = {
            "ticker": r["ticker"],
            "scanned_at": pd.Timestamp(r["scanned_at"]),
            "price": r["price"],
            "score": r["score"],
            "squeeze_bonus": raw.get("squeeze_bonus", 0) or 0,
        }
        for key in SCORE_KEY_TO_WEIGHT_KEY:
            rec[f"raw_{key}"] = sc.get(key, 0) or 0
        out.append(rec)

    df = pd.DataFrame(out)
    if df.empty:
        return df
    df["date"] = df["scanned_at"].dt.normalize()
    # One row per ticker per day — keep the LAST scan of the day (freshest
    # inputs). Multiple scans/day (scheduled + manual + watchlist) would
    # otherwise inject near-duplicate, highly correlated observations.
    df = (df.sort_values("scanned_at")
            .drop_duplicates(subset=["ticker", "date"], keep="last")
            .reset_index(drop=True))
    return df


# ─────────────────────────────────────────────────────────────────────────
# Counterfactual re-weighting
# ─────────────────────────────────────────────────────────────────────────

def component_frac(row: pd.Series, score_key: str, weights_at_scan: dict) -> float:
    """The weight-independent 'quality fraction' for one component, in [0, 1].

    Exact for EXACT_LINEAR (score = weight * frac by construction — proven in
    CLAUDE.md's Scoring Engine section: every term in _score_rsi/_score_ma/
    _score_short_interest/_score_institutional and the macd/volume inline
    formulas is `weight * fixed_fraction`, and the fractions never sum above
    1.0, so the `min(score, weight)` cap in the production code never binds).

    Approximate (conservative) for CAP_ONLY: if the stored score sits exactly
    at the scan-time weight, the true fraction may have been higher — this
    returns 1.0 in that case rather than guessing, which never overstates a
    component's quality.

    A "what if this were wired to scale with weight" hypothetical for
    HARDCODED — current production ignores the weight for these two entirely,
    so this fraction has no live analog to compare against.
    """
    weight_key = SCORE_KEY_TO_WEIGHT_KEY[score_key]
    w = weights_at_scan.get(weight_key, 0)
    if w <= 0:
        return 0.0
    return min(1.0, max(0.0, row[f"raw_{score_key}"] / w))


def counterfactual_total(row: pd.Series, candidate_weights: dict, weights_at_scan: dict) -> float:
    """Recompute the composite 0-100 score under `candidate_weights`.

    Mirrors stock_scorer.score_stock()'s formula exactly:
        core     = sum(component scores over CORE_COMPONENTS)
        core_max = sum(WEIGHTS over the same keys)   (forecast excluded)
        bonus    = news + squeeze_bonus + trends, capped at 20
        total    = min(core/core_max*90 + bonus, 100)

    `weights_at_scan` must be the WEIGHTS dict that was live when the row was
    scanned (the fractions in component_frac() are computed against it).
    `candidate_weights` may omit keys — missing ones fall back to
    `weights_at_scan`, so a candidate only needs to name what it changes.
    """
    cw = {**weights_at_scan, **candidate_weights}
    core = 0.0
    core_max = 0.0
    for key in CORE_COMPONENTS:
        w = cw[SCORE_KEY_TO_WEIGHT_KEY[key]]
        core += component_frac(row, key, weights_at_scan) * w
        core_max += w
    if core_max <= 0:
        return 0.0

    news = min(cw["news_sentiment"], row["raw_news"])
    trends = min(cw["trends"], row["raw_trends"])
    bonus = min(20.0, news + row["squeeze_bonus"] + trends)
    return round(min(core / core_max * 90 + bonus, 100.0), 1)


def reconstruction_sanity_check(df: pd.DataFrame, weights: dict) -> dict:
    """Recompute every row's total under its OWN scan-time weights and compare
    to the score actually stored in the DB. Should be near-zero — if not,
    something in this module's formula has drifted from stock_scorer.py's,
    and no reweighting conclusion below should be trusted until it's fixed.
    """
    recomputed = df.apply(lambda r: counterfactual_total(r, {}, weights), axis=1)
    diff = (recomputed - df["score"]).abs()
    return {
        "n": len(df), "mean_abs_diff": float(diff.mean()),
        "max_diff": float(diff.max()), "pct_within_1pt": float((diff <= 1.0).mean() * 100),
    }


# ─────────────────────────────────────────────────────────────────────────
# Forward returns
# ─────────────────────────────────────────────────────────────────────────

def _forward_return(close: pd.Series, ts: pd.Timestamp, days: int) -> float | None:
    """Percent change from the bar at `ts` to the first bar >= ts + days.

    Mirrors src/trigger_backtest.py::_forward_return exactly (kept as a local
    copy rather than importing a `_`-prefixed symbol across module
    boundaries) — same point-in-time semantics: both prices come from the
    SAME yfinance series, so there is no DB-vs-yfinance price-basis mismatch
    for backtester.py's split/spinoff workaround to exist for in the first
    place.
    """
    i0 = close.index.searchsorted(ts)
    i1 = close.index.searchsorted(ts + pd.Timedelta(days=days))
    if i0 >= len(close) or i1 >= len(close) or i1 <= i0:
        return None
    p0, p1 = float(close.iloc[i0]), float(close.iloc[i1])
    if p0 <= 0:
        return None
    return (p1 / p0 - 1.0) * 100.0


def attach_forward_returns(
    df: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Add ret{h} columns for each horizon, fetched from yfinance daily closes.

    Requires live network access — this is the step that cannot run in a
    network-sandboxed environment.
    """
    tickers = sorted(df["ticker"].unique())
    daily = load_bars(tickers, interval="1d", period="5y", use_cache=use_cache)

    rows = []
    for _, row in df.iterrows():
        d = daily.get(row["ticker"])
        if d is None or d.empty:
            continue
        rec = row.to_dict()
        keep = False
        for h in horizons:
            r = _forward_return(d["Close"], row["date"], h)
            rec[f"ret{h}"] = r
            keep = keep or r is not None
        if keep:
            rows.append(rec)

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("date").reset_index(drop=True)
    return out


# ─────────────────────────────────────────────────────────────────────────
# Fama-MacBeth-style monthly IC
# ─────────────────────────────────────────────────────────────────────────

def monthly_ic(df: pd.DataFrame, score_col: str, return_col: str, min_obs: int = 5) -> pd.DataFrame:
    """Cross-sectional Spearman rank IC per calendar month.

    One row per ticker per month (first scan of the month) before computing
    each month's cross-sectional correlation — a ticker scanned 20 times in
    one month would otherwise inject 20 highly autocorrelated observations
    into that single month's IC, understating how few independent months of
    evidence actually exist. This is the panel-data analog of
    trigger_backtest.clustered_stats()'s non-overlap discipline.
    """
    d = df[[score_col, return_col, "ticker", "date"]].dropna()
    if d.empty:
        return pd.DataFrame(columns=["month", "ic", "n"])
    d = d.assign(month=d["date"].dt.to_period("M"))
    d = d.sort_values("date").drop_duplicates(subset=["ticker", "month"], keep="first")

    rows = []
    for month, grp in d.groupby("month"):
        if len(grp) < min_obs:
            continue
        ic = grp[score_col].corr(grp[return_col], method="spearman")
        if pd.notna(ic):
            rows.append({"month": month, "ic": ic, "n": len(grp)})
    return pd.DataFrame(rows)


def fm_stats(monthly: pd.DataFrame) -> dict:
    """Mean/t-stat of the monthly IC series — the actual sample size is the
    number of independent months, not the number of (ticker, date) rows."""
    if monthly.empty:
        return {"n": 0, "mean": np.nan, "t": np.nan, "months": 0}
    s = stats(monthly["ic"])
    return {"n": s["n"], "mean": s["mean"], "t": s["t"], "months": s["n"]}


# ─────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────

def component_report(df: pd.DataFrame, horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> str:
    """Does each stored component score, standalone, predict forward returns?

    This is the question that actually matters for weight tuning: a
    component with a monthly IC indistinguishable from zero is a component
    no amount of weight-tuning can help — the composite score itself was
    already found to carry no predictive information (2026-08-05 audit), so
    a component-level null result here would be the expected, consistent
    finding, not a surprise.
    """
    lines = []
    all_keys = EXACT_LINEAR + CAP_ONLY + HARDCODED + BONUS_ONLY
    for h in horizons:
        ret_col = f"ret{h}"
        if ret_col not in df.columns:
            continue
        lines.append(f"===== {h}-day horizon — standalone component IC =====")
        lines.append(f"{'component':<16}{'months':>8}{'mean IC':>10}{'t':>7}  verdict")
        lines.append("-" * 55)
        for key in all_keys:
            m = monthly_ic(df, f"raw_{key}", ret_col)
            s = fm_stats(m)
            if s["months"] < 3:
                lines.append(f"{key:<16}{s['months']:>8}   (too few months)")
                continue
            verdict = "SIGNIFICANT" if abs(s["t"]) > 2 else ""
            lines.append(f"{key:<16}{s['months']:>8}{s['mean']:>+10.3f}{s['t']:>+7.2f}  {verdict}")
        lines.append("")
    lines.append("IC = Spearman rank correlation, cross-sectional per month, one scan per")
    lines.append("ticker per month. |t| > 2 across independent months is the significance bar —")
    lines.append("consistent with trigger_backtest.py's month-clustering discipline elsewhere")
    lines.append("in this project.")
    return "\n".join(lines)


def reweight_report(
    df: pd.DataFrame,
    weights_at_scan: dict,
    candidates: dict[str, dict],
    horizon: int = 30,
    train_frac: float = 0.6,
) -> str:
    """Walk-forward comparison of candidate weight vectors vs the current
    production WEIGHTS, on the composite score's IC.

    Any data-driven candidate MUST be fit on the train split only and
    evaluated on the untouched test split — fitting and evaluating on the
    same rows is exactly the in-sample overfitting this project's other
    backtests (trigger_backtest.py, signal_library.py) go out of their way
    to avoid.
    """
    ret_col = f"ret{horizon}"
    if ret_col not in df.columns or df.empty:
        return "no data"
    dates = df["date"].sort_values().unique()
    split_ts = dates[int(len(dates) * train_frac)]
    train, test = df[df["date"] < split_ts], df[df["date"] >= split_ts]

    n = len(candidates)
    bonferroni_t = 2.0 + 0.55 * np.log(max(n, 1))  # same heuristic as run_signal_panel.py

    lines = [
        f"===== {horizon}-day composite IC — walk-forward "
        f"(train < {pd.Timestamp(split_ts):%Y-%m-%d} <= test) =====",
        f"{'candidate':<28}{'train IC':>10}{'test months':>13}{'test IC':>10}{'t':>7}  verdict",
        "-" * 80,
    ]
    for name, cand in candidates.items():
        train_score = train.apply(lambda r: counterfactual_total(r, cand, weights_at_scan), axis=1)
        test_score = test.apply(lambda r: counterfactual_total(r, cand, weights_at_scan), axis=1)
        train_ic = monthly_ic(train.assign(_score=train_score), "_score", ret_col)
        test_ic = monthly_ic(test.assign(_score=test_score), "_score", ret_col)
        tr, te = fm_stats(train_ic), fm_stats(test_ic)
        flag = "  <<< SURVIVES" if te["months"] >= 3 and abs(te["t"]) > bonferroni_t else ""
        lines.append(
            f"{name:<28}{tr['mean']:>+10.3f}{te['months']:>13}{te['mean']:>+10.3f}{te['t']:>+7.2f}{flag}"
        )

    lines.append("")
    lines.append(f"Multiple-comparison threshold: {n} candidates tested, so a result needs")
    lines.append(f"|t| > {bonferroni_t:.2f} on the TEST split to count as evidence rather than")
    lines.append(f"the best of {n} coin flips. 'train IC' is shown for context only — it is")
    lines.append(f"the number a data-driven candidate was fit to, so it is expected to look")
    lines.append(f"good and proves nothing by itself.")
    return "\n".join(lines)


def fit_ic_proportional_weights(
    train: pd.DataFrame, live_lever_keys: tuple[str, ...], current_weights: dict,
    ret_col: str = "ret30", min_share: float = 0.3,
) -> dict:
    """A single pre-committed data-driven candidate: reallocate the current
    total weight budget across `live_lever_keys` proportional to each
    component's TRAIN-set monthly IC (clipped at 0), with a floor so a
    component with a noisy/negative in-sample IC isn't zeroed outright on
    one training window's estimate — this is an allocation heuristic, not a
    claimed-optimal solution, and it is deliberately restricted to
    components that are actual live levers today (EXACT_LINEAR + CAP_ONLY) —
    HARDCODED and INACTIVE components are never touched by this.
    """
    budget = sum(current_weights[SCORE_KEY_TO_WEIGHT_KEY[k]] for k in live_lever_keys)
    ics = {}
    for key in live_lever_keys:
        s = fm_stats(monthly_ic(train, f"raw_{key}", ret_col))
        ics[key] = max(0.0, s["mean"]) if pd.notna(s["mean"]) else 0.0

    total_ic = sum(ics.values())
    if total_ic <= 0:
        logger.info("[weight_tuning] all train-set ICs <= 0 — falling back to equal weight")
        share = {k: 1.0 / len(live_lever_keys) for k in live_lever_keys}
    else:
        floor = min_share / len(live_lever_keys)
        raw_share = {k: (1 - min_share) * (v / total_ic) + floor for k, v in ics.items()}
        norm = sum(raw_share.values())
        share = {k: v / norm for k, v in raw_share.items()}

    return {SCORE_KEY_TO_WEIGHT_KEY[k]: round(budget * share[k], 1) for k in live_lever_keys}
