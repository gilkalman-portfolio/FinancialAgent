"""Weekly IBKR account performance vs SPY / IWM.

Answers one question honestly: is the automated IBKR activity beating a
passive benchmark? Built 2026-10-07 after the 2026-08-05 account reset made
`daily_pnl.net_liquidation` a clean series again (pre-reset history contains
the 2026-08-03/04 short-position incident and the paper-account reset, so the
baseline starts AFTER it).

Source of truth is the account's net liquidation value, NOT order_log — fill
prices there are incomplete (see Incident Archive) so per-trade P&L from
order_log is not trustworthy.
"""
from datetime import datetime
from typing import Optional

import pandas as pd

from src.database import get_connection
from src.yf_cache import get_history

BASELINE_DATE = "2026-08-05"   # first clean NLV after the paper-account reset
BENCHMARKS = ("SPY", "IWM")


def _nlv_series(baseline: str) -> pd.Series:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT date, net_liquidation FROM daily_pnl "
            "WHERE date >= ? AND net_liquidation > 0 ORDER BY date", (baseline,)
        ).fetchall()
    return pd.Series({pd.Timestamp(r["date"]): float(r["net_liquidation"]) for r in rows}, dtype=float)


def _bench_return(ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> Optional[float]:
    h = get_history(ticker, period="1y", interval="1d")
    if h is None or h.empty:
        return None
    close = h["Close"].copy()
    close.index = pd.DatetimeIndex(close.index).tz_localize(None).normalize()
    a, b = close[close.index <= start], close[close.index <= end]
    if a.empty or b.empty:
        return None
    return float(b.iloc[-1] / a.iloc[-1] - 1) * 100


def _positions_summary() -> tuple:
    with get_connection() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(market_value),0) mv, COALESCE(SUM(unrealized_pnl),0) u "
            "FROM ibkr_positions WHERE shares > 0"
        ).fetchone()
    return int(r["n"]), float(r["mv"]), float(r["u"])


def build_report(baseline: str = BASELINE_DATE, nlv: Optional[pd.Series] = None,
                 bench: Optional[dict] = None) -> Optional[dict]:
    """None if there isn't enough NLV history. `nlv`/`bench` injectable for tests."""
    nlv = _nlv_series(baseline) if nlv is None else nlv
    if len(nlv) < 2:
        return None
    start, end = nlv.index[0], nlv.index[-1]
    week_start = nlv.index[nlv.index <= end - pd.Timedelta(days=7)]
    wk = nlv.loc[week_start[-1]] if len(week_start) else nlv.iloc[0]

    def pct(a, b): return (b / a - 1) * 100

    bench = bench if bench is not None else {t: _bench_return(t, start, end) for t in BENCHMARKS}
    bench_wk = {t: _bench_return(t, end - pd.Timedelta(days=7), end) for t in BENCHMARKS} if bench is not None else {}
    r = nlv.pct_change().dropna()
    n_pos, mv, unreal = _positions_summary()
    return {
        "start": start.date(), "end": end.date(), "days": (end - start).days,
        "nlv_start": float(nlv.iloc[0]), "nlv_end": float(nlv.iloc[-1]),
        "ret_total": pct(nlv.iloc[0], nlv.iloc[-1]), "ret_week": pct(wk, nlv.iloc[-1]),
        "bench_total": bench, "bench_week": bench_wk,
        "max_drawdown": float(((nlv / nlv.cummax()) - 1).min() * 100),
        "daily_vol": float(r.std() * 100) if len(r) > 1 else 0.0,
        "positions": n_pos, "invested_pct": mv / nlv.iloc[-1] * 100, "unrealized": unreal,
    }


def format_report(d: dict) -> str:
    def b(name, key):
        v = d[key].get(name)
        return "n/a" if v is None else f"{v:+.2f}%"

    lines = [
        f"📈 IBKR vs Benchmark — week ending {d['end']}",
        f"Since {d['start']} ({d['days']}d):  Bot {d['ret_total']:+.2f}%  |  SPY {b('SPY','bench_total')}  |  IWM {b('IWM','bench_total')}",
    ]
    spy = d["bench_total"].get("SPY")
    if spy is not None:
        ex = d["ret_total"] - spy
        lines.append(f"Excess vs SPY: {ex:+.2f}pp" + ("  ✅" if ex > 0 else "  ❌"))
    lines += [
        f"Last 7d:  Bot {d['ret_week']:+.2f}%  |  SPY {b('SPY','bench_week')}  |  IWM {b('IWM','bench_week')}",
        f"Risk: max drawdown {d['max_drawdown']:.2f}%, daily vol {d['daily_vol']:.2f}%",
        f"Positions: {d['positions']} ({d['invested_pct']:.0f}% invested, unrealized ${d['unrealized']:+,.0f})",
        "NLV = paper account. Raw excess vs SPY, not risk-adjusted; short window — not evidence of edge.",
    ]
    return "\n".join(lines)
