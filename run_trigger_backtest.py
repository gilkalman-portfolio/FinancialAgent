"""
Historical validation of the live entry trigger.

    python run_trigger_backtest.py                    # default: 250 tickers, 730d
    python run_trigger_backtest.py --tickers 400
    python run_trigger_backtest.py --no-cache
    python run_trigger_backtest.py --benchmark SPY

Answers the question forward paper trading cannot answer in useful time: does a
bullish Supertrend(1H) flip beat the benchmark, and does it still do so when the
market is not rising? Writes the event table to data/backtest_cache/ so the raw
rows can be re-analysed without re-downloading.
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
    CACHE_DIR, BacktestConfig, load_bars, regime_report, report, run_backtest,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backtest")

DB = Path(__file__).parent / "data" / "financial_agent.db"


def universe(limit: int) -> list[str]:
    """Tickers the bot actually scans, most-scanned first.

    Drawn from scan_results rather than a live index download: it is the real
    operating universe, it needs no network, and it is reproducible.
    """
    if not DB.exists():
        raise SystemExit(f"database not found: {DB}")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT ticker, COUNT(*) n FROM scan_results "
        "GROUP BY ticker ORDER BY n DESC, ticker LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", type=int, default=250, help="universe size (default 250)")
    ap.add_argument("--period", default="730d", help="history window (default 730d)")
    ap.add_argument("--benchmark", default="IWM", choices=["IWM", "SPY"])
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    use_cache = not args.no_cache
    tick = universe(args.tickers)
    logger.info(f"universe: {len(tick)} tickers from scan_results")

    logger.info("downloading hourly bars (the trigger timeframe)...")
    hourly = load_bars(tick, interval="1h", period=args.period, use_cache=use_cache)
    logger.info(f"hourly: {len(hourly)} tickers")

    # Daily bars need a longer window than the hourly cap — SMA200 requires 200
    # closed sessions BEFORE the earliest signal, so ask for 5y and let it trim.
    logger.info("downloading daily bars (for the entry filters)...")
    daily = load_bars(tick, interval="1d", period="5y", use_cache=use_cache)
    logger.info(f"daily: {len(daily)} tickers")

    bench = load_bars(["IWM", "SPY"], interval="1d", period="5y", use_cache=use_cache)
    missing = [b for b in ("IWM", "SPY") if b not in bench]
    if missing:
        raise SystemExit(f"benchmark download failed: {missing}")

    logger.info("scanning for Supertrend(1H) flips...")
    events = run_backtest(hourly, daily, bench, BacktestConfig())
    if events.empty:
        raise SystemExit("no events produced — check data availability")

    out = CACHE_DIR / "trigger_events.parquet"
    try:
        events.to_parquet(out)
    except Exception:
        out = CACHE_DIR / "trigger_events.pkl"
        events.to_pickle(out)
    logger.info(f"wrote {len(events)} events to {out}")

    span = f"{events.ts.min():%Y-%m-%d} .. {events.ts.max():%Y-%m-%d}"
    print()
    print("=" * 90)
    print(f"TRIGGER BACKTEST — bullish Supertrend(1H) flip")
    print(f"events: {len(events)}   tickers: {events.ticker.nunique()}   window: {span}")
    print("=" * 90)
    print()
    print(report(events, benchmark=args.benchmark))
    print(regime_report(events, benchmark=args.benchmark, horizon=30))
    print()
    print("Excess is measured against the benchmark over the identical wall-clock")
    print("window. Raw return is shown for context only — a long-only signal in a")
    print("rising tape earns market beta, which is not an edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
