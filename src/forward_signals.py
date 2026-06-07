"""
Forward Signals — record every alert sent and measure its outcome forward
in time. This is the "honest backtest" channel: every signal is captured at
generation time with point-in-time data, then measured against actual
future prices at 7/14/30-day horizons.

Public API:
    record_signal(...)         insert a new signal row, return id
    update_outcomes()          fill price_after_Xd for matured signals
    weekly_digest(days=7)      return aggregate metrics for last N days
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf

from src.database import get_connection, retry_on_busy

logger = logging.getLogger(__name__)

OPEN = "open"
MATURED = "matured"
HORIZONS_DAYS = (7, 14, 30)


@dataclass
class SignalRecord:
    ticker: str
    signal_type: str            # BUY / SELL / WATCH
    entry_price: float
    composite_score: Optional[float] = None
    catalyst_summary: Optional[str] = None
    supertrend_level: Optional[float] = None
    supertrend_atr: Optional[float] = None
    ai_verdict: Optional[str] = None
    telegram_sent_at: Optional[str] = None


def _check_entry_price_plausibility(ticker: str, entry_price: float) -> str | None:
    """Compare entry_price against recent scan_results.price.

    Returns 'SUSPECT' if the price looks implausible (>20% divergence or
    exactly 105.0 — a known IBKR paper-account placeholder), else None.
    """
    if entry_price == 105.0:
        logger.warning(
            f"[forward_signals] {ticker} entry_price=105.0 — known IBKR placeholder"
        )
        return "SUSPECT"

    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT price FROM scan_results "
                "WHERE ticker = ? AND scanned_at >= ? AND price IS NOT NULL "
                "ORDER BY scanned_at DESC LIMIT 1",
                (ticker, cutoff),
            ).fetchone()
        if row and row["price"]:
            scan_price = float(row["price"])
            if scan_price > 0:
                divergence = abs(entry_price - scan_price) / scan_price
                if divergence > 0.20:
                    logger.warning(
                        f"[forward_signals] {ticker} entry_price={entry_price:.2f} "
                        f"diverges {divergence:.0%} from scan price {scan_price:.2f}"
                    )
                    return "SUSPECT"
    except Exception as e:
        logger.warning(f"[forward_signals] plausibility check failed for {ticker}: {e}")
    return None


@retry_on_busy()
def record_signal(rec: SignalRecord) -> int:
    """Insert a new forward_signals row. Returns the new id."""
    now = datetime.now().isoformat()
    quality_flag = _check_entry_price_plausibility(rec.ticker, rec.entry_price)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO forward_signals (
                ticker, signal_ts, signal_type, entry_price, composite_score,
                catalyst_summary, supertrend_level, supertrend_atr, ai_verdict,
                telegram_sent_at, status, data_quality_flag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.ticker,
                now,
                rec.signal_type,
                rec.entry_price,
                rec.composite_score,
                rec.catalyst_summary,
                rec.supertrend_level,
                rec.supertrend_atr,
                rec.ai_verdict,
                rec.telegram_sent_at or now,
                OPEN,
                quality_flag,
            ),
        )
        signal_id = cur.lastrowid
    logger.info(
        f"[forward_signals] recorded {rec.signal_type} {rec.ticker} id={signal_id}"
        + (f" quality={quality_flag}" if quality_flag else "")
    )
    return signal_id


@retry_on_busy()
def record_fill(ticker: str, actual_fill_price: float, ibkr_order_id: int) -> bool:
    """Update the most recent forward_signal for *ticker* with the real fill price.

    Targets the newest row where fill_price IS NULL and data_quality_flag != 'SUSPECT'.
    Returns True if a row was updated.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM forward_signals
            WHERE ticker = ?
              AND (data_quality_flag IS NULL OR data_quality_flag != 'SUSPECT')
              AND fill_price IS NULL
            ORDER BY signal_ts DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if row is None:
            logger.warning(
                f"[forward_signals] record_fill: no eligible row for {ticker} "
                f"(order_id={ibkr_order_id})"
            )
            return False
        # Guard: don't record a fill if the order was CANCELLED in order_log.
        # Bracket order race — cancel callback on a child leg can arrive after
        # the parent fill, leaving order_log CANCELLED while the fill was real.
        # We only skip on explicit CANCELLED; missing rows proceed normally.
        order_row = conn.execute(
            "SELECT status FROM order_log WHERE ibkr_order_id = ? LIMIT 1",
            (ibkr_order_id,),
        ).fetchone()
        if order_row and order_row["status"] == "CANCELLED":
            logger.warning(
                f"[forward_signals] record_fill: order {ibkr_order_id} is CANCELLED"
                f" — skipping fill write for {ticker}"
            )
            return False
        conn.execute(
            "UPDATE forward_signals SET fill_price = ?, fill_source = ? WHERE id = ?",
            (actual_fill_price, "IBKR_CALLBACK", row["id"]),
        )
    logger.info(
        f"[forward_signals] fill recorded: {ticker} id={row['id']} "
        f"fill_price={actual_fill_price:.2f} order_id={ibkr_order_id}"
    )
    return True


def _fetch_price_at(ticker: str, target_dt: datetime) -> Optional[float]:
    """Closing price on target_dt (or the next available trading day)."""
    start = target_dt.date()
    end = (target_dt + timedelta(days=7)).date()
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[0])
    except Exception as e:
        logger.warning(f"[forward_signals] price fetch failed for {ticker}@{target_dt}: {e}")
        return None


@retry_on_busy()
def update_outcomes() -> dict:
    """
    Fill price_after_Xd / return_Xd_pct for signals whose horizons have matured.
    A row is marked 'matured' once all three horizons are populated.
    """
    now = datetime.now()
    stats = {"checked": 0, "filled": 0, "matured": 0}

    # ── Phase 1: read open signals into memory, then close the connection ────
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, ticker, signal_ts, entry_price, price_after_7d, "
            "price_after_14d, price_after_30d FROM forward_signals WHERE status = ?",
            (OPEN,),
        ).fetchall()

    # ── Phase 2: do all yfinance lookups OUTSIDE any DB connection ───────────
    # No DB lock is held during these network calls (can be many seconds each).
    pending_updates: list[tuple[dict, str, int]] = []  # (updates, new_status, row_id)

    for row in rows:
        stats["checked"] += 1
        signal_dt = datetime.fromisoformat(row["signal_ts"])
        updates: dict = {}

        for horizon in HORIZONS_DAYS:
            col_price = f"price_after_{horizon}d"
            col_return = f"return_{horizon}d_pct"

            if row[col_price] is not None:
                continue
            target = signal_dt + timedelta(days=horizon)
            if target > now:
                continue

            price = _fetch_price_at(row["ticker"], target)
            if price is None:
                continue

            ret = ((price - row["entry_price"]) / row["entry_price"]) * 100.0
            updates[col_price] = price
            updates[col_return] = ret

        if not updates:
            continue

        all_filled = all(
            (updates.get(f"price_after_{h}d") is not None) or
            (row[f"price_after_{h}d"] is not None)
            for h in HORIZONS_DAYS
        )
        new_status = MATURED if all_filled else OPEN
        pending_updates.append((updates, new_status, row["id"]))

    # ── Phase 3: single short transaction for ALL updates ────────────────────
    if pending_updates:
        with get_connection() as conn:
            for updates, new_status, row_id in pending_updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
                params = list(updates.values()) + [new_status, row_id]
                conn.execute(
                    f"UPDATE forward_signals SET {set_clause}, status = ? WHERE id = ?",
                    params,
                )
                stats["filled"] += 1
                if new_status == MATURED:
                    stats["matured"] += 1

    logger.info(f"[forward_signals] outcomes update: {stats}")
    return stats


def weekly_digest(days: int = 7) -> dict:
    """Aggregate stats over the last `days` days of signals."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT signal_type, return_7d_pct, return_14d_pct, return_30d_pct,
                   composite_score, ticker
            FROM forward_signals
            WHERE signal_ts >= ?
            """,
            (cutoff,),
        ).fetchall()

    total = len(rows)
    by_type = {}
    returns_7d, returns_14d, returns_30d = [], [], []
    winners_7d = 0
    measured_7d = 0

    for r in rows:
        t = r["signal_type"]
        by_type[t] = by_type.get(t, 0) + 1
        if r["return_7d_pct"] is not None:
            returns_7d.append(r["return_7d_pct"])
            measured_7d += 1
            if r["return_7d_pct"] > 0:
                winners_7d += 1
        if r["return_14d_pct"] is not None:
            returns_14d.append(r["return_14d_pct"])
        if r["return_30d_pct"] is not None:
            returns_30d.append(r["return_30d_pct"])

    def _avg(xs):
        return sum(xs) / len(xs) if xs else None

    return {
        "window_days": days,
        "total_signals": total,
        "by_type": by_type,
        "avg_return_7d_pct": _avg(returns_7d),
        "avg_return_14d_pct": _avg(returns_14d),
        "avg_return_30d_pct": _avg(returns_30d),
        "win_rate_7d_pct": (winners_7d / measured_7d * 100.0) if measured_7d else None,
        "measured_7d": measured_7d,
    }


def format_digest_message(d: dict) -> str:
    """Telegram-ready human-readable summary."""
    lines = [
        f"📊 Weekly Forward Signals Digest ({d['window_days']}d)",
        f"Total signals: {d['total_signals']}",
    ]
    if d["by_type"]:
        breakdown = ", ".join(f"{k}={v}" for k, v in d["by_type"].items())
        lines.append(f"Breakdown: {breakdown}")
    if d["avg_return_7d_pct"] is not None:
        lines.append(f"Avg 7D return:  {d['avg_return_7d_pct']:+.2f}%  ({d['measured_7d']} measured)")
    if d["avg_return_14d_pct"] is not None:
        lines.append(f"Avg 14D return: {d['avg_return_14d_pct']:+.2f}%")
    if d["avg_return_30d_pct"] is not None:
        lines.append(f"Avg 30D return: {d['avg_return_30d_pct']:+.2f}%")
    if d["win_rate_7d_pct"] is not None:
        lines.append(f"Win rate 7D:    {d['win_rate_7d_pct']:.1f}%")
    return "\n".join(lines)
