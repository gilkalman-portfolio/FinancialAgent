#!/usr/bin/env python3
"""
Run one day's cycle of the $10K Challenge paper-trading strategy
(src/challenge_portfolio.py) — see that module's docstring for the full
rules and, critically, the evidence basis and its limits before trusting
any result this produces.

WHY THIS IS A SEPARATE SCRIPT, NOT WIRED INTO scheduler.py's ALREADY-RUNNING
JOBS: this is a bounded, one-month experiment with its own $10,000 capital
figure, run for a head-to-head comparison against an external agent — it is
deliberately NOT wired into order_manager.py / execution_engine.py /
ibkr_worker.py, so it can never place a real or paper IBKR order and can
never interact with the production trading bot's state. Keeping it fully
separate means a bug here cannot touch the live account, and a bug in the
live account's exit logic (this project has a documented history of exactly
that — see CLAUDE.md's Incident Archive, the whole "shorts" incident family)
cannot touch this experiment either.

HOW TO ACTUALLY RUN THIS FOR A MONTH: it has to run on a machine that stays
on, once per trading day. A Claude Code cloud session cannot do this —
its cron jobs are session-only and hard-expire after 7 days even if the
session somehow stayed alive that long, and this sandbox's outbound network
policy blocks Yahoo Finance outright (confirmed live 2026-09-11 — an
organization-level proxy rejection, not a transient failure). Point a daily
Task Scheduler / cron entry (any time after the US market close, e.g. 16:30
ET) at:

    python run_challenge_cycle.py

on the same machine that already runs scheduler.py, using the same
CREATE_NO_WINDOW-watchdog pattern documented under "IBKR Real-Time
Architecture" in CLAUDE.md if you want it to survive reboots unattended.

Each run: checks exits on open positions (ATR stop-loss / 30-day time-stop
/ never-lowers trailing-stop ratchet), scans for new entries (Supertrend
daily bullish flip + trailing-45-day insider-purchase filter) if slots are
free, logs the day's equity snapshot, writes data/challenge_report.md, and
prints a summary to stdout.

Requires MASSIVE_API_KEY in .env. Without it, has_recent_insider_buy()
fails closed (see its docstring) and NO entries will ever fire — that is
correct behavior, not a bug: this strategy's entire evidence basis is that
specific filter, and silently trading without it would silently revert to
the plain Supertrend-flip signal already shown to have ~0% edge.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.database import init_db
from src.challenge_portfolio import run_daily_cycle, generate_report


def main() -> None:
    init_db()
    result = run_daily_cycle()
    report = generate_report()

    report_path = Path(__file__).parent / "data" / "challenge_report.md"
    report_path.write_text(report, encoding="utf-8")

    print(f"=== $10K Challenge — {result['date']} ===")
    print(f"Exits:   {len(result['exits'])}")
    for e in result["exits"]:
        print(f"  SELL {e['shares']:.0f} {e['ticker']} @ ${e['price']:.2f} ({e['reason']})")
    print(f"Entries: {len(result['entries'])}")
    for e in result["entries"]:
        print(f"  BUY  {e['shares']:.0f} {e['ticker']} @ ${e['price']:.2f} (stop ${e['stop']:.2f})")
    snap = result["snapshot"]
    print(f"Equity:  ${snap['total_equity']:,.2f}  "
          f"(cash ${snap['cash']:,.2f}, {snap['open_positions']} open positions)")
    print(f"\nFull report saved to {report_path}")


if __name__ == "__main__":
    main()
