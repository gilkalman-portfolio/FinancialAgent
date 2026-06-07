"""
Opportunity Tracker — records every BUY signal as an 'opportunity' and
measures whether price hit T1, hit stop, or expired over a 30-day window.

Public API:
    record_opportunity(...)   — insert a new opportunity row
    update_outcomes()         — daily job: fill status for matured rows
    weekly_digest()           — Friday job: Telegram summary
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf

from src.database import get_connection, retry_on_busy

logger = logging.getLogger(__name__)

OPEN      = "open"
HIT_T1    = "hit_target"
HIT_STOP  = "hit_stop"
EXPIRED   = "expired"
MAX_DAYS  = 30


# ─────────────────────────────────────────────────────────────────────────
# DB initialisation — called by init_db() in database.py
# ─────────────────────────────────────────────────────────────────────────

def ensure_table():
    """Create opportunity_log if it does not exist yet."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunity_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker       TEXT NOT NULL,
                detected_at  TEXT NOT NULL,
                signal_type  TEXT,
                entry_price  REAL,
                stop_loss    REAL,
                target1      REAL,
                target2      REAL,
                rr_ratio     REAL,
                status       TEXT NOT NULL DEFAULT 'open',
                outcome_price REAL,
                outcome_at    TEXT,
                outcome_pct   REAL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_opp_ticker ON opportunity_log(ticker)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_opp_status ON opportunity_log(status)"
        )
    logger.debug("[opportunity_tracker] table ensured")


# ─────────────────────────────────────────────────────────────────────────
# Record
# ─────────────────────────────────────────────────────────────────────────

@retry_on_busy()
def record_opportunity(
    ticker: str,
    signal_type: str,
    entry_price: float,
    stop_loss: float,
    target1: float,
    target2: Optional[float] = None,
    rr_ratio: Optional[float] = None,
) -> int:
    """
    Insert a new open opportunity.  Returns the new row id.
    Silently skips duplicate: same ticker + signal_type fired within last 24 h.
    """
    try:
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        with get_connection() as conn:
            dup = conn.execute(
                "SELECT id FROM opportunity_log "
                "WHERE ticker=? AND signal_type=? AND detected_at>=? AND status=?",
                (ticker.upper(), signal_type, cutoff, OPEN),
            ).fetchone()
            if dup:
                logger.debug(f"[opp_tracker] duplicate skip: {ticker} {signal_type}")
                return dup["id"]

            cur = conn.execute(
                """
                INSERT INTO opportunity_log
                    (ticker, detected_at, signal_type, entry_price, stop_loss,
                     target1, target2, rr_ratio, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker.upper(),
                    datetime.now().isoformat(),
                    signal_type,
                    entry_price,
                    stop_loss,
                    target1,
                    target2,
                    rr_ratio,
                    OPEN,
                ),
            )
            row_id = cur.lastrowid
        logger.info(
            f"[opp_tracker] recorded {signal_type} {ticker} "
            f"entry={entry_price:.2f} stop={stop_loss:.2f} T1={target1:.2f} id={row_id}"
        )
        return row_id
    except Exception as e:
        logger.warning(f"[opp_tracker] record_opportunity({ticker}): {e}")
        return -1


# ─────────────────────────────────────────────────────────────────────────
# Outcome update — run daily at 18:00
# ─────────────────────────────────────────────────────────────────────────

def _fetch_current_price(ticker: str) -> Optional[float]:
    try:
        hist = yf.Ticker(ticker).history(period="2d")
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"[opp_tracker] price fetch failed {ticker}: {e}")
        return None


@retry_on_busy()
def update_outcomes() -> dict:
    """
    For every open opportunity:
      - fetch current price
      - if price <= stop_loss  → hit_stop
      - if price >= target1    → hit_target
      - if elapsed > 30 days   → expired
    Returns summary stats dict.
    """
    stats = {"checked": 0, "hit_target": 0, "hit_stop": 0, "expired": 0}
    now = datetime.now()

    # Read all open rows (outside any lock)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, ticker, detected_at, entry_price, stop_loss, target1 "
            "FROM opportunity_log WHERE status = ?",
            (OPEN,),
        ).fetchall()
        rows = [dict(r) for r in rows]

    pending: list[tuple[int, str, float, float]] = []  # (id, new_status, outcome_price, outcome_pct)

    for row in rows:
        stats["checked"] += 1
        try:
            detected = datetime.fromisoformat(row["detected_at"])
        except Exception:
            continue

        elapsed_days = (now - detected).days
        price = _fetch_current_price(row["ticker"])
        if price is None:
            continue

        entry = row["entry_price"]
        stop  = row["stop_loss"]
        t1    = row["target1"]
        pct   = round((price - entry) / entry * 100, 2) if entry else 0.0

        if price >= t1:
            new_status = HIT_T1
            stats["hit_target"] += 1
        elif price <= stop:
            new_status = HIT_STOP
            stats["hit_stop"] += 1
        elif elapsed_days >= MAX_DAYS:
            new_status = EXPIRED
            stats["expired"] += 1
        else:
            continue  # still open, nothing to update

        pending.append((row["id"], new_status, price, pct))

    if pending:
        now_iso = now.isoformat()
        with get_connection() as conn:
            for row_id, new_status, outcome_price, outcome_pct in pending:
                conn.execute(
                    """
                    UPDATE opportunity_log
                    SET status=?, outcome_price=?, outcome_at=?, outcome_pct=?
                    WHERE id=?
                    """,
                    (new_status, outcome_price, now_iso, outcome_pct, row_id),
                )

    logger.info(f"[opp_tracker] update_outcomes: {stats}")
    return stats


# ─────────────────────────────────────────────────────────────────────────
# Weekly digest — run every Friday at 20:00
# ─────────────────────────────────────────────────────────────────────────

def weekly_digest(days: int = 7) -> dict:
    """Return aggregate stats for opportunities detected in the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT ticker, status, entry_price, outcome_price, outcome_pct
            FROM opportunity_log
            WHERE detected_at >= ?
            """,
            (cutoff,),
        ).fetchall()
        rows = [dict(r) for r in rows]

    hits, stops, open_rows, expired_rows = [], [], [], []
    for r in rows:
        if r["status"] == HIT_T1:
            hits.append(r)
        elif r["status"] == HIT_STOP:
            stops.append(r)
        elif r["status"] == EXPIRED:
            expired_rows.append(r)
        else:
            open_rows.append(r)

    all_closed = hits + stops
    wins = [r for r in all_closed if (r.get("outcome_pct") or 0) > 0]
    avg_win  = (sum(r["outcome_pct"] for r in hits)  / len(hits)  if hits  else None)
    avg_loss = (sum(r["outcome_pct"] for r in stops) / len(stops) if stops else None)
    win_rate = round(len(wins) / len(all_closed) * 100, 1) if all_closed else None

    return {
        "window_days": days,
        "hits":        hits,
        "stops":       stops,
        "open":        open_rows,
        "expired":     expired_rows,
        "avg_win_pct": round(avg_win,  2) if avg_win  is not None else None,
        "avg_loss_pct": round(avg_loss, 2) if avg_loss is not None else None,
        "win_rate_pct": win_rate,
    }


def format_digest_message(d: dict) -> str:
    """Telegram-ready opportunity digest."""
    hits   = d["hits"]
    stops  = d["stops"]
    opens  = d["open"]
    lines  = [f"📊 דוח הזדמנויות שבועי ({d['window_days']}d)", "──────────────────"]

    if hits:
        lines.append(f"✅ הגיעו ל-T1 ({len(hits)}):")
        for r in hits:
            pct_str = f"{r['outcome_pct']:+.1f}%" if r.get("outcome_pct") is not None else ""
            lines.append(f"  {r['ticker']}: {pct_str}")

    if stops:
        lines.append(f"❌ נפגעו ב-Stop ({len(stops)}):")
        for r in stops:
            pct_str = f"{r['outcome_pct']:+.1f}%" if r.get("outcome_pct") is not None else ""
            lines.append(f"  {r['ticker']}: {pct_str}")

    if opens:
        lines.append(f"⏳ פתוחות ({len(opens)}):")
        open_parts = []
        for r in opens:
            ep = r.get("entry_price") or 0
            op = r.get("outcome_price")
            if ep and op:
                live_pct = (op - ep) / ep * 100
                open_parts.append(f"{r['ticker']}: {live_pct:+.1f}%")
            else:
                open_parts.append(r["ticker"])
        lines.append("  " + " | ".join(open_parts))

    if d.get("avg_win_pct") is not None:
        lines.append(f"Avg win: {d['avg_win_pct']:+.1f}%")
    if d.get("avg_loss_pct") is not None:
        lines.append(f"Avg loss: {d['avg_loss_pct']:+.1f}%")
    if d.get("win_rate_pct") is not None:
        lines.append(f"Win rate: {d['win_rate_pct']:.0f}%")

    return "\n".join(lines)
