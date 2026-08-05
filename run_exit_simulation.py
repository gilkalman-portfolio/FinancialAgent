"""
Which exit regime actually harvests the trigger's edge?

    python run_exit_simulation.py

The horizon study (run_trigger_backtest.py) found the edge lives at ~30 days and
that the live system exits in days. That study measures a FIXED holding period,
which no real system trades — real systems have stops, and a stop changes the
return distribution. This replays each signal bar by bar under competing exit
policies and reports the excess each one actually captures, net of friction.

Regime "A" reproduces what production does today: exit on the next bearish
Supertrend(1H) flip. It is the control.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from src.trigger_backtest import (  # noqa: E402
    CACHE_DIR, BacktestConfig, ExitConfig, exit_reason_breakdown, exit_report,
    load_bars, run_backtest, simulate_exits,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("exitsim")

DB = Path(__file__).parent / "data" / "financial_agent.db"

REGIMES = [
    # A — the control: what the bot does today.
    ExitConfig("A live: Supertrend flip exit", stop_atr=2.0, max_hold_days=None,
               supertrend_exit=True),
    # B — same stop, but hold to the horizon where the edge was measured.
    ExitConfig("B stop 2ATR + 30d time", stop_atr=2.0, max_hold_days=30),
    # C — wider stop, same horizon. Tests whether 2ATR is cutting winners.
    ExitConfig("C stop 3ATR + 30d time", stop_atr=3.0, max_hold_days=30),
    # D — trail only after the trade is working, so it cannot stop out at breakeven.
    ExitConfig("D 3ATR stop, trail 3ATR +5%", stop_atr=3.0, trail_atr=3.0,
               trail_arm_pct=5.0, max_hold_days=30),
    # E — let it run past the measured horizon.
    ExitConfig("E stop 3ATR + 60d time", stop_atr=3.0, max_hold_days=60),
    # F — fixed target instead of time.
    ExitConfig("F stop 2ATR target 4ATR 30d", stop_atr=2.0, target_atr=4.0,
               max_hold_days=30),
    # G — no stop at all. Not tradeable; isolates how much the stop itself costs.
    ExitConfig("G no stop, 30d time (ref)", stop_atr=None, max_hold_days=30),
]


def universe(limit: int) -> list[str]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT ticker, COUNT(*) n FROM scan_results "
        "GROUP BY ticker ORDER BY n DESC, ticker LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", type=int, default=250)
    ap.add_argument("--period", default="730d")
    ap.add_argument("--benchmark", default="IWM", choices=["IWM", "SPY"])
    ap.add_argument("--friction", type=float, default=0.20,
                    help="round-trip commission + slippage %% (default 0.20)")
    args = ap.parse_args()

    tick = universe(args.tickers)
    hourly = load_bars(tick, interval="1h", period=args.period)
    daily = load_bars(tick, interval="1d", period="5y")
    bench = load_bars(["IWM", "SPY"], interval="1d", period="5y")
    logger.info(f"loaded {len(hourly)} hourly / {len(daily)} daily")

    cached = CACHE_DIR / "trigger_events.parquet"
    if cached.exists():
        events = pd.read_parquet(cached)
        logger.info(f"reusing {len(events)} cached events")
    else:
        events = run_backtest(hourly, daily, bench, BacktestConfig())

    results: dict[str, pd.DataFrame] = {}
    for cfg in REGIMES:
        cfg.friction_pct = args.friction
        logger.info(f"simulating: {cfg.name}  (stop ATR on {cfg.stop_timeframe} bars)")
        results[cfg.name] = simulate_exits(events, hourly, bench, cfg, args.benchmark,
                                           daily=daily)

    print()
    print("=" * 94)
    print(f"EXIT REGIME COMPARISON — {len(events)} signals, friction {args.friction}% "
          f"round-trip, benchmark {args.benchmark}")
    print("=" * 94)
    print()
    print(exit_report(results))

    best = max((k for k, v in results.items() if not v.empty),
               key=lambda k: results[k].net_pct.mean(), default=None)
    if best:
        print()
        print(f"===== exit-reason breakdown for the best regime: {best} =====")
        print(exit_reason_breakdown(results[best]))
        print()
        print(f"===== control (live behaviour) for comparison =====")
        print(exit_reason_breakdown(results[REGIMES[0].name]))

    out = CACHE_DIR / "exit_sim.pkl"
    pd.to_pickle(results, out)
    print(f"\nper-trade results written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
