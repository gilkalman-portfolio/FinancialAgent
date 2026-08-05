"""
Test a panel of candidate entry signals through one harness.

    python run_signal_panel.py

Every candidate is measured identically: excess return vs benchmark over the
same wall-clock window, month-clustered on outcome-blind non-overlapping trades,
and then re-measured under a real exit policy with a stop.

Multiple comparisons are handled explicitly. Testing 7 signals means the best of
7 looks good by chance; the Bonferroni threshold below is what a result must
clear to count as evidence rather than noise.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from src import signal_library as sl  # noqa: E402
from src.trigger_backtest import (  # noqa: E402
    CACHE_DIR, BacktestConfig, ExitConfig, clustered_stats, load_bars,
    run_backtest, simulate_exits, stats,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("panel")
DB = Path(__file__).parent / "data" / "financial_agent.db"

CANDIDATES = {
    "supertrend_flip (control)": sl.supertrend_flip,
    "golden_cross": sl.golden_cross,
    "breakout_52w + volume": sl.breakout_52w,
    "donchian_20": sl.donchian_20,
    "momentum_12_1": sl.momentum_12_1,
    "rsi_dip_in_uptrend": sl.rsi_dip_in_uptrend,
    "gap_up_continuation": sl.gap_up_continuation,
}

# A realistic tradeable policy — a stop is mandatory, so this is what matters.
TRADEABLE = ExitConfig("stop 3ATR + 30d", stop_atr=3.0, max_hold_days=30,
                       stop_timeframe="daily", friction_pct=0.20)


def universe(limit: int) -> list[str]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT ticker, COUNT(*) n FROM scan_results "
        "GROUP BY ticker ORDER BY n DESC, ticker LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", type=int, default=250)
    ap.add_argument("--benchmark", default="IWM", choices=["IWM", "SPY"])
    args = ap.parse_args()

    tick = universe(args.tickers)
    hourly = load_bars(tick, interval="1h", period="730d")
    daily = load_bars(tick, interval="1d", period="5y")
    bench = load_bars(["IWM", "SPY"], interval="1d", period="5y")
    logger.info(f"{len(hourly)} hourly / {len(daily)} daily tickers")

    n_tests = len(CANDIDATES)
    bonferroni_t = 2.0 + 0.55 * np.log(n_tests)  # ~ two-sided 0.05/n on ~34 months

    rows = []
    for name, fn in CANDIDATES.items():
        logger.info(f"signal: {name}")
        ev = run_backtest(hourly, daily, bench, BacktestConfig(), signal_fn=fn)
        if ev.empty:
            rows.append({"signal": name, "n": 0})
            continue

        col = f"exc_{args.benchmark}_30"
        horizon = stats(ev[col]) if col in ev else {"n": 0, "mean": np.nan, "t": np.nan}

        tr = simulate_exits(ev, hourly, bench, TRADEABLE, args.benchmark, daily=daily)
        cs = clustered_stats(tr) if not tr.empty else {"n": 0, "mean": np.nan, "t": np.nan}

        rows.append({
            "signal": name,
            "n": len(ev),
            "h_mean": horizon["mean"], "h_t": horizon["t"],
            "t_n": cs["n"], "t_mean": cs["mean"], "t_t": cs["t"],
            "hold": tr.hold_days.mean() if not tr.empty else np.nan,
            "win": (tr.net_pct > 0).mean() * 100 if not tr.empty else np.nan,
        })

    df = pd.DataFrame(rows)
    print()
    print("=" * 100)
    print(f"SIGNAL PANEL — {len(CANDIDATES)} candidates, benchmark {args.benchmark}")
    print("=" * 100)
    print()
    print(f"{'signal':<28}{'signals':>8}{'30d exc':>9}{'(naive t)':>10}"
          f"{'| tradeable:':>13}{'indep':>7}{'excess':>9}{'t':>7}{'hold':>7}{'win%':>7}")
    print("-" * 100)
    for r in df.itertuples():
        if not r.n:
            print(f"{r.signal:<28}{0:>8}   (no signals)")
            continue
        flag = ""
        if not np.isnan(r.t_t) and abs(r.t_t) > bonferroni_t:
            flag = "  <<< SURVIVES"
        print(f"{r.signal:<28}{r.n:>8}{r.h_mean:>+8.2f}%{r.h_t:>+10.2f}"
              f"{'':>13}{r.t_n:>7}{r.t_mean:>+8.2f}%{r.t_t:>+7.2f}"
              f"{r.hold:>6.1f}d{r.win:>6.1f}%{flag}")

    print()
    print(f"'30d exc' is the unmanaged close-to-close excess with its NAIVE t — inflated by")
    print(f"overlapping windows, shown only because it is the number that misleads.")
    print(f"'tradeable' applies {TRADEABLE.name} with {TRADEABLE.friction_pct}% friction and is")
    print(f"month-clustered on outcome-blind non-overlapping trades. That is the real column.")
    print()
    print(f"Multiple-comparison threshold: {len(CANDIDATES)} signals tested, so a result needs")
    print(f"|t| > {bonferroni_t:.2f} to count as evidence rather than the best of {len(CANDIDATES)} coin flips.")

    out = CACHE_DIR / "signal_panel.csv"
    df.to_csv(out, index=False)
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
