"""
Historical validation of the `stock_scorer.py` WEIGHTS dict.

    python run_weight_tuning_backtest.py                  # default: 30d horizon
    python run_weight_tuning_backtest.py --horizon 14
    python run_weight_tuning_backtest.py --no-cache
    python run_weight_tuning_backtest.py --min-rows 200    # skip if too little history

Requires:
  - The production data/financial_agent.db, with months of accumulated
    scan_results (a fresh clone's empty DB will produce "no data" — this is
    not a bug, there is nothing to analyze yet).
  - Live network access to yfinance, to fetch forward returns.

Neither is available in a fresh clone or a network-sandboxed session — this
script was written and unit-tested (tests/test_weight_tuning_backtest.py,
pure math, no network/DB) in such an environment but never executed there.
Run it on the machine that runs scheduler.py — see CLAUDE.md's
"Weight Tuning Backtest" section for the full write-up of what this
measures and why.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.stock_scorer import WEIGHTS  # noqa: E402
from src.weight_tuning_backtest import (  # noqa: E402
    CAP_ONLY, EXACT_LINEAR, SCORE_KEY_TO_WEIGHT_KEY, attach_forward_returns,
    component_report, fit_ic_proportional_weights, load_scored_history,
    reconstruction_sanity_check, reweight_report,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("weight_tuning")

DB = Path(__file__).parent / "data" / "financial_agent.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=30, choices=[7, 14, 30])
    ap.add_argument("--min-rows", type=int, default=100,
                     help="minimum scored (ticker, day) rows required to proceed (default 100)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--train-frac", type=float, default=0.6,
                     help="fraction of the date range used to fit the data-driven candidate (default 0.6)")
    args = ap.parse_args()

    if not DB.exists():
        raise SystemExit(f"database not found: {DB}")

    logger.info(f"loading scored history from {DB}...")
    hist = load_scored_history(DB)
    if len(hist) < args.min_rows:
        raise SystemExit(
            f"only {len(hist)} scored (ticker, day) rows found — need >= {args.min_rows}. "
            f"This is expected on a fresh clone; run this against the production DB "
            f"after scheduler.py has been scanning for a while."
        )
    logger.info(f"{len(hist)} scored (ticker, day) rows, {hist['ticker'].nunique()} tickers, "
                f"{hist['date'].min():%Y-%m-%d} .. {hist['date'].max():%Y-%m-%d}")

    sanity = reconstruction_sanity_check(hist, WEIGHTS)
    logger.info(f"reconstruction sanity check: {sanity}")
    if sanity["pct_within_1pt"] < 95:
        raise SystemExit(
            "reconstruction sanity check failed — recomputed composite scores don't match "
            "the DB's stored scores under the CURRENT WEIGHTS. This means WEIGHTS has "
            "changed since some of these rows were scanned (weights_at_scan must be the "
            "weights live AT SCAN TIME, not today's), or this module's formula has drifted "
            "from stock_scorer.py's. Do not trust the reweighting results below until this "
            "is resolved — see CLAUDE.md's Weight Tuning Backtest section."
        )

    logger.info("fetching forward returns from yfinance (this needs live network access)...")
    hist = attach_forward_returns(hist, horizons=(7, 14, 30), use_cache=not args.no_cache)
    if hist.empty:
        raise SystemExit("no rows survived forward-return attachment — check yfinance access")

    print()
    print("=" * 90)
    print("WEIGHT TUNING BACKTEST — stock_scorer.py WEIGHTS")
    print(f"rows: {len(hist)}   tickers: {hist['ticker'].nunique()}   "
          f"window: {hist['date'].min():%Y-%m-%d} .. {hist['date'].max():%Y-%m-%d}")
    print(f"reconstruction check: mean|diff|={sanity['mean_abs_diff']:.2f}pt, "
          f"{sanity['pct_within_1pt']:.1f}% of rows within 1pt of the stored score")
    print("=" * 90)
    print()
    print(component_report(hist, horizons=(7, 14, 30)))

    live_levers = EXACT_LINEAR + CAP_ONLY
    dates = hist["date"].sort_values().unique()
    split_ts = dates[int(len(dates) * args.train_frac)]
    train = hist[hist["date"] < split_ts]

    fitted = fit_ic_proportional_weights(train, live_levers, WEIGHTS, ret_col=f"ret{args.horizon}")
    budget = sum(WEIGHTS[SCORE_KEY_TO_WEIGHT_KEY[k]] for k in live_levers)
    equal_share = round(budget / len(live_levers), 1)
    candidates = {
        "control (current WEIGHTS)": {},
        "equal_weight (live levers only)": {SCORE_KEY_TO_WEIGHT_KEY[k]: equal_share for k in live_levers},
        "ic_proportional (fit on train)": fitted,
    }

    print(reweight_report(hist, WEIGHTS, candidates, horizon=args.horizon, train_frac=args.train_frac))
    print()
    print("A component or candidate with no significant IC is a valid, expected finding, not")
    print("a tool failure — the composite score was already found to carry no measurable edge")
    print("(2026-08-05 audit). See CLAUDE.md before wiring any 'SURVIVES' result into")
    print("production: HARDCODED components (fundamentals, dcf) are intentionally excluded")
    print("from every candidate here because wiring them up needs a coordinated fix in")
    print("execution_engine.py too, not just stock_scorer.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
