"""
Challenge Portfolio — a $10,000, one-month, standalone paper-trading strategy
built for a bounded head-to-head comparison against an external agent.

*** READ THIS BEFORE TREATING ANY OUTPUT AS A PROVEN EDGE ***

This project's own research (CLAUDE.md Incident Archive, 2026-08-05 through
2026-08-28) has already tested this exact question — "is there a validated,
profitable long-only entry signal in this codebase" — across ten-plus
strategy families: technical trend-following on large-cap AND genuine
small-cap universes, pairs trading, PEAD, catalyst-conditioned entries, and
an exit-policy sweep, each measured with a real train/holdout split and a
Bonferroni-corrected significance bar (not naive overlapping-window stats).
The written conclusion, dated 2026-08-28: "are we on track for live trading
in 1-2 months: no ... [no strategy] not yet met [the] gate" for a live-capital
decision.

Exactly ONE configuration did not collapse to zero or negative: Supertrend
daily-bar bullish flip, gated on any open-market insider purchase (SEC Form
4, transaction code 'P') in the trailing 45 days. It survived a walk-forward
train/holdout split with the right sign, but weakened sharply out of sample
(t=+1.64 in-sample -> t=+0.35 out-of-sample on the 7d/14d tradeable return),
and a STRICTER version of the identical idea (requiring 2+ insider buys
instead of any) *flipped negative* out of sample — a live, documented
example of "the best-looking variant is the most overfit one" from that same
research session. That is the single most evidence-backed configuration
this project has ever produced, which is exactly why it is the one
implemented below — and it is still a thin, weak, small-sample signal that
directionally survived a holdout, not a confirmed source of alpha. Treat
this module as a disciplined, bounded, capital-preservation-first bet, not
a money machine. Whoever reads its results after 30 days should update
this docstring (or CLAUDE.md's "$10K Challenge Strategy" section) with what
actually happened, the same "needs real history before drawing conclusions"
discipline every other research tool in this project follows.

Deliberately independent of order_manager.py / execution_engine.py /
ibkr_worker.py — this NEVER touches the live/paper IBKR trading path. It is
a self-contained simulator: yfinance daily closes stand in for fills, and
its own challenge_trades / challenge_positions / challenge_equity_log
tables (src/database.py) hold all state. Nothing in this module can affect
a real order or the production trading bot.

Rules (see run_challenge_cycle.py for the daily driver):
  - Universe: Russell 2000 + S&P 500 (SCAN_INDICES), same as the production
    scheduler's own supertrend-universe monitor, further restricted to
    sub-$2B market cap / no analyst coverage (_passes_market_cap_filter) —
    see "External verification" below for why this was added.
  - Entry: fresh daily Supertrend bullish flip (src.supertrend.
    scan_supertrend_universe, bars_ago==1) + price >= MIN_PRICE + passes the
    market-cap filter + any open-market insider purchase in the trailing
    INSIDER_LOOKBACK_DAYS days (has_recent_insider_buy — fails CLOSED on any
    lookup error or missing MASSIVE_API_KEY, since silently falling through
    to "unfiltered" would silently trade the exact configuration already
    shown to have ~0% edge).
  - Sizing: equal-weight across MAX_POSITIONS slots of total equity, capped
    by an explicit per-trade risk budget (RISK_PER_TRADE_PCT of equity,
    divided by the ATR-implied stop distance) — whichever cap binds tighter.
  - Exit: hard stop at entry_price - STOP_ATR_MULT*ATR(14), trailed upward
    (never down) as price rises; hard time-stop at TIME_STOP_DAYS held.

External verification (2026-09-11, user-directed web research against
academic/practitioner sources — see CLAUDE.md's "$10K Challenge Strategy"
section for full citations):
  - Insider open-market buying predicting forward returns is a real,
    decades-old, well-replicated finding (Seyhun 1986/98; Jeng, Metrick &
    Zeckhauser 2003; Lakonishok & Lee 2002), NOT specific to this project's
    internal backtest. About half the abnormal return accrues within the
    first month post-purchase (Wharton/Seyhun) — external support for this
    module's ~45-day filter window and 30-day time-stop horizon.
  - The literature also says the effect concentrates in smaller,
    thinly-covered names — this is WHY the market-cap filter above was
    added; the original version of this module scanned the full Russell
    2000 + S&P 500 universe with no cap filter, which academic evidence
    says would have diluted the signal with mega-cap noise.
  - Tension found, NOT acted on: multiple sources say CLUSTER buying (2+
    insiders) beats single-insider buying by roughly 2x — the opposite of
    this project's own internal finding (see "Evidence basis" above) that a
    stricter 2+-insider cut flipped negative out of sample on this exact
    joint (flip + insider) signal. Not resolved either way: that internal
    result came from a thin sample (~51 clustered units), so it may be noise
    rather than a real reversal of a much better-powered academic finding —
    but changing this module's filter from "any insider buy" to "2+" on the
    strength of outside literature alone, without testing it on THIS joint
    signal, would be exactly the kind of untested tweak this project's own
    incident archive warns against. Left as "any insider buy in 45 days";
    flagged in CLAUDE.md's Open Backlog as worth testing once real
    challenge-cycle history (or a proper backtest of the 2+ variant on a
    larger sample) exists.
  - ATR stop multiple (2.5x here) vs. the "canonical" Turtle Trading system
    (2x ATR(20)): checked and left as is — 2-3x is the broadly accepted
    range for trend-following stops, and 2.5x/ATR(14) was chosen for
    consistency with this project's own existing price_alert_monitor.py
    convention, not an oversight.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger

from src.database import get_connection
from src.index_loader import get_index
from src.massive_insider_client import get_insider_transactions
from src.supertrend import scan_supertrend_universe
from src.yf_cache import get_info as _yf_info

# ── Tunables ─────────────────────────────────────────────────────────────
STARTING_CASH = 10_000.0
MAX_POSITIONS = 5
RISK_PER_TRADE_PCT = 0.02   # 2% of total equity risked per new entry
STOP_ATR_MULT = 2.5         # matches this project's existing ATR-trailing-stop
                             # scale (price_alert_monitor.py's 2.5xATR/1h),
                             # applied to daily bars since this cycles once/day
TIME_STOP_DAYS = 30         # matches the challenge's own horizon and
                             # run_exit_simulation.py's validated 30d time-stop
INSIDER_LOOKBACK_DAYS = 45  # exact figure from the one configuration that
                             # survived holdout — see module docstring
MIN_PRICE = 5.0              # matches price floors used elsewhere in this
                             # codebase (gap_scanner, auto_watchlist supertrend)
SCAN_INDICES = ["Russell 2000", "S&P 500"]
ATR_PERIOD = 14
NO_COVERAGE_MAX_MARKET_CAP = 2_000_000_000  # matches insider_cluster_scanner.py's
                             # already-QC-validated universe (sub-$2B, no analyst
                             # coverage). Added 2026-09-11 after external
                             # verification: academic literature (Lakonishok &
                             # Lee 2002; Cohen, Malloy & Pomorski 2012) is
                             # consistent that insider-buying's predictive power
                             # concentrates in smaller, thinly-covered names —
                             # scanning the full Russell 2000 + S&P 500 universe
                             # with NO market-cap filter (the original version of
                             # this module) let mega-caps, where the academic
                             # literature says the effect is weakest, dilute the
                             # entry signal. See module docstring and CLAUDE.md's
                             # "$10K Challenge Strategy" section for sources.


@dataclass
class ChallengePosition:
    ticker: str
    shares: float
    avg_cost: float
    stop_price: float
    opened_date: str


def _now_iso() -> str:
    return datetime.now().isoformat()


def _today() -> str:
    return date.today().isoformat()


# ── State: cash / positions ────────────────────────────────────────────────

def get_cash_balance() -> float:
    """STARTING_CASH plus/minus every simulated fill. The trade ledger
    (challenge_trades) is the source of truth rather than a separately
    maintained running balance, so cash can never drift out of sync with
    the fills actually recorded."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN action='BUY' THEN -shares*price "
            "ELSE shares*price END), 0) AS net FROM challenge_trades"
        ).fetchone()
    return STARTING_CASH + float(row["net"] or 0.0)


def get_open_positions() -> list[ChallengePosition]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, shares, avg_cost, stop_price, opened_date "
            "FROM challenge_positions ORDER BY ticker"
        ).fetchall()
    return [ChallengePosition(**dict(r)) for r in rows]


def _upsert_position(ticker: str, shares: float, avg_cost: float, stop_price: float, opened_date: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO challenge_positions (ticker, shares, avg_cost, stop_price, opened_date, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET shares=excluded.shares, "
            "stop_price=excluded.stop_price, updated_at=excluded.updated_at",
            (ticker, shares, avg_cost, stop_price, opened_date, _now_iso()),
        )


def _delete_position(ticker: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM challenge_positions WHERE ticker = ?", (ticker,))


def _record_trade(ticker: str, action: str, shares: float, price: float,
                   reason: str, stop_price: Optional[float] = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO challenge_trades (ticker, action, shares, price, trade_date, "
            "reason, stop_price, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, action, shares, price, _today(), reason, stop_price, _now_iso()),
        )


# ── Market data helpers (yfinance only — no IBKR, no live order path) ──────

def _fetch_history(ticker: str, period: str = "3mo") -> Optional[pd.DataFrame]:
    try:
        hist = yf.Ticker(ticker).history(period=period, auto_adjust=True)
        if hist is None or hist.empty:
            return None
        return hist
    except Exception as e:
        logger.debug(f"[challenge] history fetch failed for {ticker}: {e}")
        return None


def compute_atr(ticker: str, period: int = ATR_PERIOD) -> Optional[float]:
    hist = _fetch_history(ticker)
    if hist is None or len(hist) < period + 1:
        return None
    high, low, close = hist["High"], hist["Low"], hist["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else None


def fetch_latest_close(ticker: str) -> Optional[float]:
    hist = _fetch_history(ticker, period="5d")
    if hist is None or hist.empty:
        return None
    return float(hist["Close"].iloc[-1])


def has_recent_insider_buy(ticker: str, lookback_days: int = INSIDER_LOOKBACK_DAYS) -> bool:
    """True if any open-market insider purchase (Form 4, code P) was filed
    for this ticker in the trailing `lookback_days`. Fails CLOSED (returns
    False) on any lookup error or missing MASSIVE_API_KEY — this filter IS
    the entire evidence basis of this strategy (see module docstring);
    silently falling through to "unfiltered" would silently trade the exact
    configuration already shown to have ~0% edge."""
    try:
        txs = get_insider_transactions(ticker, days=lookback_days)
    except Exception as e:
        logger.debug(f"[challenge] insider lookup failed for {ticker}: {e}")
        return False
    return any(tx.get("type") == "BUY" for tx in txs)


def _passes_market_cap_filter(ticker: str) -> bool:
    """Sub-$2B / no-analyst-coverage check, matching insider_cluster_scanner.py's
    already-validated universe definition exactly. Academic literature is
    consistent that insider buying's predictive power concentrates in smaller,
    thinly-covered names (see module docstring) — without this, a Russell
    2000 + S&P 500 scan lets mega-caps (where the effect is weakest) dilute
    the entry signal. Fails CLOSED on any lookup error, same convention as
    has_recent_insider_buy() — a data failure must never be read as "passes"."""
    try:
        info = _yf_info(ticker, ttl=3600)
        market_cap = info.get("marketCap") or 0
        if market_cap <= 0 or market_cap >= NO_COVERAGE_MAX_MARKET_CAP:
            return False
        forward_pe = info.get("forwardPE")
        if forward_pe is not None and forward_pe > 0:
            return False
        return True
    except Exception as e:
        logger.debug(f"[challenge] market-cap filter failed for {ticker}: {e}")
        return False


def _load_universe() -> list[str]:
    tickers: set[str] = set()
    for index_name in SCAN_INDICES:
        try:
            df = get_index(index_name)
        except Exception as e:
            logger.warning(f"[challenge] get_index({index_name}) failed: {e}")
            df = None
        if df is not None:
            tickers.update(df["ticker"].tolist())
    return list(tickers)


# ── Sizing ──────────────────────────────────────────────────────────────

def _size_entry(price: float, stop: float, cash: float, total_equity: float) -> int:
    """Equal-weight (total_equity / MAX_POSITIONS) capped by an explicit
    risk budget (RISK_PER_TRADE_PCT of equity / stop distance), whichever
    binds tighter. Never sizes past available cash."""
    if price <= 0 or price <= stop:
        return 0
    target_value = total_equity / MAX_POSITIONS
    budget = min(target_value, cash)
    shares_by_budget = math.floor(budget / price)
    risk_budget = total_equity * RISK_PER_TRADE_PCT
    shares_by_risk = math.floor(risk_budget / (price - stop))
    return max(0, min(shares_by_budget, shares_by_risk))


# ── Daily cycle ────────────────────────────────────────────────────────

def check_exits() -> list[dict]:
    """Check every open position for a stop-loss or time-stop exit; trail
    the stop upward (never down) otherwise. Returns exit events."""
    events = []
    for pos in get_open_positions():
        price = fetch_latest_close(pos.ticker)
        if price is None:
            logger.warning(f"[challenge] {pos.ticker}: price fetch failed, skipping exit check")
            continue

        held_days = (date.today() - date.fromisoformat(pos.opened_date)).days
        if price <= pos.stop_price:
            _record_trade(pos.ticker, "SELL", pos.shares, price, reason="stop_loss")
            _delete_position(pos.ticker)
            events.append({"ticker": pos.ticker, "action": "SELL", "reason": "stop_loss",
                            "price": price, "shares": pos.shares})
            continue
        if held_days >= TIME_STOP_DAYS:
            _record_trade(pos.ticker, "SELL", pos.shares, price, reason="time_stop")
            _delete_position(pos.ticker)
            events.append({"ticker": pos.ticker, "action": "SELL", "reason": "time_stop",
                            "price": price, "shares": pos.shares})
            continue

        atr = compute_atr(pos.ticker)
        if atr:
            trailed_stop = max(pos.stop_price, price - STOP_ATR_MULT * atr)
            if trailed_stop != pos.stop_price:
                _upsert_position(pos.ticker, pos.shares, pos.avg_cost, trailed_stop, pos.opened_date)
    return events


def find_entries(open_slots: int) -> list[dict]:
    """Fresh daily-bar Supertrend bullish flips, filtered to tickers with an
    open-market insider purchase in the trailing 45 days (see module
    docstring — this filter IS the strategy's evidence basis)."""
    if open_slots <= 0:
        return []
    universe = _load_universe()
    if not universe:
        logger.warning("[challenge] no universe tickers loaded — skipping entry scan")
        return []

    flips = scan_supertrend_universe(universe)
    held = {p.ticker for p in get_open_positions()}
    flips = [f for f in flips if f["price"] >= MIN_PRICE and f["ticker"] not in held]

    # Market-cap/coverage filter runs before the insider-buy lookup: it's a
    # cached local check (yf_cache), cheaper than the Massive/Polygon call,
    # and also saves an API call for every mega-cap flip that would be
    # excluded anyway.
    flips = [f for f in flips if _passes_market_cap_filter(f["ticker"])]

    candidates = [f for f in flips if has_recent_insider_buy(f["ticker"])]
    candidates.sort(key=lambda f: f["avg_volume"], reverse=True)
    return candidates[:open_slots]


def execute_entries(candidates: list[dict]) -> list[dict]:
    """Size and fill each candidate in order, against a total_equity figure
    fixed at the start of the cycle (moving cash into a position at the same
    day's price doesn't change equity) while decrementing live cash so
    multiple same-cycle entries can never overspend."""
    events: list[dict] = []
    if not candidates:
        return events

    cash = get_cash_balance()
    positions = get_open_positions()
    positions_value = sum((fetch_latest_close(p.ticker) or p.avg_cost) * p.shares for p in positions)
    total_equity = cash + positions_value
    open_count = len(positions)

    for cand in candidates:
        if open_count >= MAX_POSITIONS:
            break
        atr = compute_atr(cand["ticker"])
        if not atr:
            continue
        stop = cand["price"] - STOP_ATR_MULT * atr
        shares = _size_entry(cand["price"], stop, cash, total_equity)
        if shares <= 0:
            continue

        _record_trade(cand["ticker"], "BUY", shares, cand["price"], reason="entry_signal", stop_price=stop)
        _upsert_position(cand["ticker"], shares, cand["price"], stop, _today())
        cash -= shares * cand["price"]
        open_count += 1
        events.append({"ticker": cand["ticker"], "action": "BUY", "shares": shares,
                        "price": cand["price"], "stop": stop})
    return events


def log_equity_snapshot() -> dict:
    cash = get_cash_balance()
    positions = get_open_positions()
    positions_value = sum((fetch_latest_close(p.ticker) or p.avg_cost) * p.shares for p in positions)
    total_equity = cash + positions_value
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO challenge_equity_log (log_date, cash, positions_value, total_equity, "
            "open_positions, notes) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(log_date) DO UPDATE SET cash=excluded.cash, "
            "positions_value=excluded.positions_value, total_equity=excluded.total_equity, "
            "open_positions=excluded.open_positions",
            (_today(), cash, positions_value, total_equity, len(positions), None),
        )
    return {"cash": cash, "positions_value": positions_value,
            "total_equity": total_equity, "open_positions": len(positions)}


def run_daily_cycle() -> dict:
    """One full cycle: check exits, look for new entries if slots are free,
    log the day's equity snapshot. Intended to be called at most once per
    trading day (see run_challenge_cycle.py)."""
    exit_events = check_exits()
    open_slots = MAX_POSITIONS - len(get_open_positions())
    candidates = find_entries(open_slots)
    entry_events = execute_entries(candidates)
    snapshot = log_equity_snapshot()
    return {"date": _today(), "exits": exit_events, "entries": entry_events, "snapshot": snapshot}


def generate_report() -> str:
    positions = get_open_positions()
    cash = get_cash_balance()
    with get_connection() as conn:
        trades = conn.execute("SELECT * FROM challenge_trades ORDER BY created_at").fetchall()

    lines = ["# $10K Challenge — Status Report", f"_Generated {_now_iso()}_", "",
              f"**Cash:** ${cash:,.2f}", "", "## Open Positions"]
    positions_value = 0.0
    if positions:
        lines.append("| Ticker | Shares | Avg Cost | Stop | Opened | Unrealized |")
        lines.append("|---|---|---|---|---|---|")
        for p in positions:
            price = fetch_latest_close(p.ticker) or p.avg_cost
            positions_value += p.shares * price
            pnl_pct = (price - p.avg_cost) / p.avg_cost * 100 if p.avg_cost else 0.0
            lines.append(f"| {p.ticker} | {p.shares:.0f} | ${p.avg_cost:.2f} | "
                         f"${p.stop_price:.2f} | {p.opened_date} | {pnl_pct:+.1f}% |")
    else:
        lines.append("_none_")

    total_equity = cash + positions_value
    total_return_pct = (total_equity - STARTING_CASH) / STARTING_CASH * 100
    lines += ["", f"**Total equity: ${total_equity:,.2f}  "
                   f"({total_return_pct:+.2f}% since ${STARTING_CASH:,.0f} start)**",
              "", f"## Trade Log ({len(trades)} fills)"]
    for t in trades:
        lines.append(f"- {t['trade_date']}: {t['action']} {t['shares']:.0f} {t['ticker']} "
                     f"@ ${t['price']:.2f} ({t['reason']})")
    return "\n".join(lines)
