"""
Alert Monitor — daily health check + auto-fix agent.
Runs at 09:30 via scheduler.py after all morning scans complete.

Checks:
  1. Breakout alert noise (same ticker 2+ consecutive days) → suppress
  2. score_drop noise (same ticker 2+ days, same score) → suppress
  3. Dead alert threads (0 records of certain types in 24h) → warn
  4. Portfolio P&L (drawdown > 8%, score < 35 held > 3 days) → warn
  5. Sends a Telegram health report + logs to data/alert_monitor_log.txt
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger

from src.database import get_connection

LOG_FILE = Path(__file__).parent.parent / "data" / "alert_monitor_log.txt"

# Alert types that SHOULD fire every day (expected active threads)
EXPECTED_DAILY_TYPES = [
    "breakout_alert",
    "score_threshold",
    "score_delta_rise",
    "score_delta_drop",
]

# Alert types that indicate active daemon threads
THREAD_TYPES = [
    "supertrend_1h_flip",
    "supertrend_flip",
    "news_catalyst",
]



def _log(msg: str):
    logger.info(f"[AlertMonitor] {msg}")
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n")
    except Exception:
        pass


def _suppress(conn: sqlite3.Connection, ticker: str, alert_type: str, reason: str):
    """Write a suppression record to reset the 24h cooldown."""
    conn.execute(
        "INSERT INTO watchlist_alerts (ticker, alert_type, message, sent_at, score, price) "
        "VALUES (?, ?, ?, ?, NULL, NULL)",
        (ticker, alert_type, f"[AUTO-SUPPRESSED] {reason}", datetime.now().isoformat())
    )
    conn.commit()
    _log(f"Suppressed {alert_type} for {ticker}: {reason}")


# ─────────────────────────────────────────────
# Section 1: Breakout alert noise
# ─────────────────────────────────────────────

def check_breakout_noise(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """
    Returns (noisy_tickers, suppressed_tickers).
    Suppresses breakout_alert for any ticker that fired today AND yesterday.
    """
    noisy, suppressed = [], []
    try:
        today     = datetime.now().date().isoformat()
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()

        rows = conn.execute(
            """SELECT ticker,
                      MAX(CASE WHEN DATE(sent_at) = ? THEN 1 ELSE 0 END) as today_fired,
                      MAX(CASE WHEN DATE(sent_at) = ? THEN 1 ELSE 0 END) as yest_fired
               FROM watchlist_alerts
               WHERE alert_type = 'breakout_alert'
                 AND sent_at >= datetime('now', '-3 days')
               GROUP BY ticker""",
            (today, yesterday)
        ).fetchall()

        for r in rows:
            if r["today_fired"] and r["yest_fired"]:
                noisy.append(r["ticker"])
                _suppress(conn, r["ticker"], "breakout_alert",
                          "fired 2+ consecutive days — extending cooldown")
                suppressed.append(r["ticker"])

        _log(f"Breakout noise: {len(noisy)} tickers ({', '.join(noisy) or 'none'})")
    except Exception as e:
        _log(f"ERROR in check_breakout_noise: {e}")
    return noisy, suppressed


# ─────────────────────────────────────────────
# Section 2: score_drop noise
# ─────────────────────────────────────────────

def check_score_drop_noise(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """
    Suppresses score_drop if same ticker fired today AND yesterday with score within 1pt.
    """
    noisy, suppressed = [], []
    try:
        today     = datetime.now().date().isoformat()
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()

        rows = conn.execute(
            """SELECT ticker,
                      MAX(CASE WHEN DATE(sent_at) = ? THEN score ELSE NULL END) as score_today,
                      MAX(CASE WHEN DATE(sent_at) = ? THEN score ELSE NULL END) as score_yest
               FROM watchlist_alerts
               WHERE alert_type = 'score_drop'
                 AND sent_at >= datetime('now', '-3 days')
               GROUP BY ticker""",
            (today, yesterday)
        ).fetchall()

        for r in rows:
            st = r["score_today"]
            sy = r["score_yest"]
            if st is not None and sy is not None and abs(st - sy) <= 1.0:
                noisy.append(r["ticker"])
                _suppress(conn, r["ticker"], "score_drop",
                          f"repeated score_drop with same score ({st:.1f}) — extending cooldown")
                suppressed.append(r["ticker"])

        _log(f"score_drop noise: {len(noisy)} tickers ({', '.join(noisy) or 'none'})")
    except Exception as e:
        _log(f"ERROR in check_score_drop_noise: {e}")
    return noisy, suppressed


# ─────────────────────────────────────────────
# Section 3: Dead thread detection
# ─────────────────────────────────────────────

def check_dead_threads(conn: sqlite3.Connection) -> list[str]:
    """
    Checks if key alert types fired at least once in the last 48h.
    Returns list of suspected-dead types.
    """
    warnings = []
    try:
        for atype in THREAD_TYPES:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM watchlist_alerts "
                "WHERE alert_type = ? AND sent_at >= datetime('now', '-48 hours')",
                (atype,)
            ).fetchone()
            if row["cnt"] == 0:
                warnings.append(atype)
                _log(f"WARNING: No '{atype}' alerts in last 48h — thread may be dead")

        if not warnings:
            _log("All daemon thread alert types active in last 48h")
    except Exception as e:
        _log(f"ERROR in check_dead_threads: {e}")
    return warnings


# ─────────────────────────────────────────────
# Section 4: Portfolio P&L health
# ─────────────────────────────────────────────

def check_portfolio_health(conn: sqlite3.Connection) -> list[str]:
    """
    Returns list of warning strings for flagged positions.
    Flags: PnL < -8% with no stop_loss, or score < 35 held > 3 days.
    """
    flags = []
    try:
        portfolio = conn.execute(
            "SELECT ticker, entry_price, shares, added_at, stop_loss FROM portfolio"
        ).fetchall()

        now = datetime.now()

        for p in portfolio:
            ticker     = p["ticker"]
            entry      = p["entry_price"]
            added_at   = datetime.fromisoformat(p["added_at"])
            days_held  = (now - added_at).days
            stop_loss  = p["stop_loss"]

            # Get latest price and score from scan_results
            row = conn.execute(
                """SELECT price, raw_data FROM scan_results
                   WHERE ticker = ?
                   ORDER BY scanned_at DESC LIMIT 1""",
                (ticker,)
            ).fetchone()

            if not row:
                continue

            price = row["price"]
            score = None
            try:
                rd = json.loads(row["raw_data"]) if row["raw_data"] else {}
                score = rd.get("score")
            except Exception:
                pass

            if not entry or entry <= 0 or not price:
                continue

            pnl_pct = (price - entry) / entry * 100

            if pnl_pct < -8.0 and not stop_loss:
                msg = (f"{ticker}: PnL {pnl_pct:+.1f}% | entry=${entry:.2f} now=${price:.2f} "
                       f"| NO STOP LOSS | held {days_held}d")
                flags.append(msg)
                _log(f"Portfolio flag (drawdown): {msg}")

            if score is not None and score < 35 and days_held > 3:
                msg = (f"{ticker}: score={score:.0f} (SKIP) | held {days_held}d "
                       f"| entry=${entry:.2f} now=${price:.2f} | PnL={pnl_pct:+.1f}%")
                flags.append(msg)
                _log(f"Portfolio flag (low score): {msg}")

        _log(f"Portfolio health: {len(flags)} flags")
    except Exception as e:
        _log(f"ERROR in check_portfolio_health: {e}")
    return flags


# ─────────────────────────────────────────────
# Section 5: Alert volume summary
# ─────────────────────────────────────────────

def get_alert_summary(conn: sqlite3.Connection) -> tuple[int, dict]:
    """Returns (total_last_24h, {alert_type: count}) for last 24h."""
    total = 0
    breakdown = {}
    try:
        rows = conn.execute(
            """SELECT alert_type, COUNT(*) as cnt
               FROM watchlist_alerts
               WHERE sent_at >= datetime('now', '-24 hours')
               GROUP BY alert_type
               ORDER BY cnt DESC"""
        ).fetchall()
        for r in rows:
            breakdown[r["alert_type"]] = r["cnt"]
            total += r["cnt"]
    except Exception as e:
        _log(f"ERROR in get_alert_summary: {e}")
    return total, breakdown


# ─────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────

def run_alert_monitor():
    """
    Full daily alert health check. Called by scheduler.py at 09:30.
    Auto-fixes noise, sends Telegram report.
    """
    from src.telegram_notifier import TelegramNotifier

    _log("=" * 60)
    _log("Alert Monitor starting")

    telegram = TelegramNotifier()

    with get_connection() as conn:
        # 1. Alert volume
        total_24h, breakdown = get_alert_summary(conn)

        # 2. Breakout noise
        breakout_noisy, breakout_suppressed = check_breakout_noise(conn)

        # 3. score_drop noise
        drop_noisy, drop_suppressed = check_score_drop_noise(conn)

        # 4. Dead threads
        dead_threads = check_dead_threads(conn)

        # 5. Portfolio health
        portfolio_flags = check_portfolio_health(conn)

    # ── Build Telegram report ─────────────────────────────────────────────
    all_suppressed = breakout_suppressed + drop_suppressed
    all_noisy      = (
        [f"{t} (breakout)" for t in breakout_noisy] +
        [f"{t} (score_drop)" for t in drop_noisy]
    )
    issue_count = len(dead_threads) + len(portfolio_flags)

    lines = [
        f"🤖 Daily Alert Health Report — {datetime.now().strftime('%Y-%m-%d')}",
        f"📊 Alerts last 24h: {total_24h}",
        ""
    ]

    # Alert breakdown (top 6)
    if breakdown:
        top_types = list(breakdown.items())[:6]
        lines.append("📋 Top alert types:")
        for atype, cnt in top_types:
            lines.append(f"  {atype}: {cnt}")
        lines.append("")

    if all_noisy:
        lines.append(f"⚠️ Noise detected ({len(all_noisy)}):")
        for n in all_noisy:
            lines.append(f"  • {n}")
        lines.append("")

    if all_suppressed:
        lines.append(f"🔕 Auto-suppressed ({len(all_suppressed)}): {', '.join(all_suppressed)}")
        lines.append("")

    if dead_threads:
        lines.append(f"💀 Possible dead threads ({len(dead_threads)}):")
        for t in dead_threads:
            lines.append(f"  • {t} — no alerts in 48h")
        lines.append("")

    if portfolio_flags:
        lines.append(f"💼 Portfolio flags ({len(portfolio_flags)}):")
        for f in portfolio_flags:
            lines.append(f"  ⚠️ {f}")
        lines.append("")

    if issue_count == 0 and not all_noisy:
        lines.append("✅ All systems OK")
    else:
        lines.append(f"❌ Issues found: {issue_count} | Noise suppressed: {len(all_suppressed)}")

    report = "\n".join(lines)
    _log("Sending Telegram report")
    telegram.send_message(report, parse_mode="")

    _log(f"Alert Monitor complete. Issues={issue_count}, suppressed={len(all_suppressed)}")
    _log("=" * 60)
