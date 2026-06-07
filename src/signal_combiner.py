"""
Signal Combiner — the pre-momentum swing alert engine.

Architecture (agreed after multi-model review):

    Catalyst Detection  ─┐
    Composite Score    ─├─► Watchlist (monitoring queue)
    Liquidity Gate     ─┘
                          │
                          ▼
                Real-time Supertrend (1H, IBKR)
                          │
                          ▼
                  TRADE ALERT (Telegram)

The combiner takes a freshly-detected Supertrend 1H flip event for a ticker
that is in the monitoring queue and fires a BUY or SELL alert.
Supertrend flip is the sole trigger — no composite score gate.

It enforces:
    - Daily cap (10 alerts/day)
    - Per-ticker / per-type 24h dedup (uses watchlist_alerts table)
    - Records every fired signal in forward_signals for outcome tracking
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.database import get_connection
from src.forward_signals import SignalRecord, record_signal
from src.monitoring_queue import build_queue
from src.execution_engine import format_trade_plan_block

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────
DAILY_ALERT_CAP = 10
DEDUP_HOURS = 24
ALERT_TYPE_BUY = "combined_buy"
ALERT_TYPE_SELL = "combined_sell"


@dataclass
class SupertrendEvent:
    ticker: str
    direction: str              # "Bullish" | "Bearish"
    signal: str                 # "BUY" | "SELL" (from supertrend.py)
    level: float
    last_price: float
    bars_ago: int = 0


@dataclass
class CombinedAlert:
    ticker: str
    alert_type: str             # ALERT_TYPE_BUY / ALERT_TYPE_SELL
    entry_price: float
    composite_score: Optional[float]
    catalyst_summary: Optional[str]
    supertrend_level: float
    message: str                # ready-to-send telegram body


# ── Helpers ───────────────────────────────────────────────────────────────


def _alerts_sent_today() -> int:
    cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM watchlist_alerts WHERE sent_at >= ? "
            "AND alert_type IN (?, ?)",
            (cutoff, ALERT_TYPE_BUY, ALERT_TYPE_SELL),
        ).fetchone()
    return int(row["n"]) if row else 0


def _alert_recently_sent(ticker: str, alert_type: str, hours: int = DEDUP_HOURS) -> bool:
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM watchlist_alerts WHERE ticker = ? AND alert_type = ? "
            "AND sent_at >= ? LIMIT 1",
            (ticker, alert_type, cutoff),
        ).fetchone()
    return row is not None


def _try_claim_dedup(ticker: str, alert_type: str, message: str, price: float, score: Optional[float], hours: int = DEDUP_HOURS) -> bool:
    """Atomically check dedup and claim it in a single transaction.

    Returns True if this call won the slot (no recent alert existed).
    Using a single connection for both the SELECT and INSERT eliminates the
    race window between the old _alert_recently_sent + _record_dedup pair.
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM watchlist_alerts WHERE ticker = ? AND alert_type = ? "
            "AND sent_at >= ? LIMIT 1",
            (ticker, alert_type, cutoff),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO watchlist_alerts (ticker, alert_type, message, sent_at, score, price) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, alert_type, message, datetime.now().isoformat(), score, price),
        )
    return True


def _latest_scan_context(ticker: str) -> dict:
    """Pull the most recent scan_results row for this ticker."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT explosion_score, catalyst, raw_data, recommendation "
            "FROM scan_results WHERE ticker = ? ORDER BY scanned_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    if row is None:
        return {}
    raw: dict = {}
    if row["raw_data"]:
        try:
            raw = json.loads(row["raw_data"])
        except json.JSONDecodeError:
            pass
    return {
        "composite_score": float(row["explosion_score"]) if row["explosion_score"] is not None else None,
        "catalyst": row["catalyst"],
        "recommendation": row["recommendation"],
        "raw": raw,
    }


def _format_buy_message(ev: SupertrendEvent, ctx: dict) -> str:
    score = ctx.get("composite_score")
    catalyst = ctx.get("catalyst") or "—"
    rec = ctx.get("recommendation") or "—"
    stop = ev.level
    risk_pct = ((ev.last_price - stop) / ev.last_price) * 100.0 if ev.last_price else None

    parts = [
        f"🟢 BUY — {ev.ticker}",
        f"Supertrend flipped BULLISH (1H)",
        f"Price:  ${ev.last_price:.2f}",
        f"Stop:   ${stop:.2f}" + (f"  (risk {risk_pct:.1f}%)" if risk_pct else ""),
    ]
    if score is not None:
        parts.append(f"Score:  {score:.0f}")
    parts.append(f"Catalyst: {catalyst}")
    parts.append(f"Recommendation: {rec}")
    parts.append("🎯 Action: trail stop below Supertrend level; size to 1% risk.")
    msg = "\n".join(parts)
    try:
        plan_block = format_trade_plan_block(ev.ticker, ev.last_price)
        if plan_block:
            msg += plan_block
    except Exception as _e:
        logger.warning(f"[combiner] trade plan block failed for {ev.ticker}: {_e}")
    return msg


def _format_sell_message(ev: SupertrendEvent, ctx: dict) -> str:
    parts = [
        f"🔴 SELL — {ev.ticker}",
        f"Supertrend flipped BEARISH (1H)",
        f"Price: ${ev.last_price:.2f}",
        f"Level: ${ev.level:.2f}",
    ]
    rec = ctx.get("recommendation")
    if rec:
        parts.append(f"Recommendation: {rec}")
    parts.append("🎯 Action: exit longs / consider partial.")
    return "\n".join(parts)


# ── Public entry point ────────────────────────────────────────────────────


def evaluate(event: SupertrendEvent) -> Optional[CombinedAlert]:
    """
    Decide whether a Supertrend flip should fire a combined alert.

    Signal logic: Supertrend 1H flip is the sole trigger for both BUY and SELL.
    No composite score gate — any watchlist ticker gets alerted on flip.

    Returns the CombinedAlert (already persisted + deduped) if fired,
    or None if suppressed (cap reached, deduped, queue-miss).
    """
    # 1. Ticker must be in the monitoring queue (watchlist + scanner ≥65 + recent BUY 72h)
    queued = {e.ticker for e in build_queue(apply_liquidity_gate=False)}
    if event.ticker not in queued:
        logger.debug(f"[combiner] {event.ticker} not in monitoring queue — ignoring flip")
        return None

    # 2. Daily cap
    if _alerts_sent_today() >= DAILY_ALERT_CAP:
        logger.info(f"[combiner] daily cap ({DAILY_ALERT_CAP}) reached — suppressing {event.ticker}")
        return None

    is_buy = event.signal == "BUY"
    alert_type = ALERT_TYPE_BUY if is_buy else ALERT_TYPE_SELL

    # 3. Pull context for message enrichment (score shown in message but does NOT gate the alert)
    ctx = _latest_scan_context(event.ticker)
    score = ctx.get("composite_score")

    # 4. Build message
    message = _format_buy_message(event, ctx) if is_buy else _format_sell_message(event, ctx)

    # 5. Atomically claim dedup slot — check + insert in a single DB transaction to
    # eliminate the race window that allowed duplicate alerts when two cycles overlapped.
    if not _try_claim_dedup(event.ticker, alert_type, message, event.last_price, score):
        logger.debug(f"[combiner] {event.ticker} {alert_type} dedup hit")
        return None
    record_signal(SignalRecord(
        ticker=event.ticker,
        signal_type="BUY" if is_buy else "SELL",
        entry_price=event.last_price,
        composite_score=score,
        catalyst_summary=ctx.get("catalyst"),
        supertrend_level=event.level,
    ))

    # 6. Record in opportunity tracker (BUY only — need a trade plan)
    if is_buy:
        try:
            from src.opportunity_tracker import record_opportunity
            from src.execution_engine import build_trade_plan
            plan = build_trade_plan(event.ticker, event.last_price)
            if plan:
                record_opportunity(
                    ticker=event.ticker,
                    signal_type=alert_type,
                    entry_price=event.last_price,
                    stop_loss=plan["stop_loss"],
                    target1=plan["target1"],
                    target2=plan["target2"],
                    rr_ratio=plan["rr_ratio"],
                )
        except Exception as _oe:
            logger.warning(f"[combiner] opportunity record failed for {event.ticker}: {_oe}")

    return CombinedAlert(
        ticker=event.ticker,
        alert_type=alert_type,
        entry_price=event.last_price,
        composite_score=score,
        catalyst_summary=ctx.get("catalyst"),
        supertrend_level=event.level,
        message=message,
    )
