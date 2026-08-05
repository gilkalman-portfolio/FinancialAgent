"""
Position Tracker — syncs IBKR positions to DB and exposes exposure queries.

Called every 5 min from ibkr_worker.run_once() to keep ibkr_positions
and daily_pnl tables current.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.database import get_connection, retry_on_busy

logger = logging.getLogger(__name__)

# Throttle for the short-position alarm. The pause itself is never throttled.
SHORT_ALARM_COOLDOWN_HOURS = 6


class PositionTracker:
    def __init__(self, ibkr_client, db_module=None):
        self.ibkr = ibkr_client
        # db_module kept for future extensibility; we use get_connection() directly

    @retry_on_busy()
    def sync_positions(self) -> None:
        """Fetch positions from IBKR and upsert into ibkr_positions table."""
        try:
            positions = self.ibkr.get_positions()
        except Exception as e:
            logger.error(f"[position_tracker] get_positions failed: {e}")
            return

        # Short positions are RECORDED, not filtered. This used to drop every
        # negative-share row on the assumption they were "paper-account artifacts".
        # They were not: on 2026-08-05 the account held 14 real shorts worth
        # -$2.37M against an $853k NLV, with -$121k unrealized, and not one of them
        # appeared in ibkr_positions — so every veto, the /positions command, and
        # alert_monitor were all blind to them, and the DELETE below treated them
        # as closed. A long-only bot must be able to SEE a short in order to
        # refuse to deepen it and to tell its operator.
        shorts = {t: d for t, d in positions.items() if d.get("shares", 0) < 0}
        if shorts:
            self._raise_short_alarm(shorts)

        now = datetime.now().isoformat()
        with get_connection() as conn:
            for ticker, data in positions.items():
                conn.execute(
                    """
                    INSERT INTO ibkr_positions (ticker, shares, avg_cost, unrealized_pnl, market_value, last_synced)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ticker) DO UPDATE SET
                        shares=excluded.shares,
                        avg_cost=excluded.avg_cost,
                        unrealized_pnl=excluded.unrealized_pnl,
                        market_value=excluded.market_value,
                        last_synced=excluded.last_synced
                    """,
                    (ticker, data["shares"], data["avg_cost"],
                     data["unrealized_pnl"], data["market_value"], now),
                )

            # Remove positions that IBKR no longer reports (fully closed)
            if positions:
                placeholders = ",".join("?" for _ in positions)
                conn.execute(
                    f"DELETE FROM ibkr_positions WHERE ticker NOT IN ({placeholders})",
                    list(positions.keys()),
                )
            else:
                conn.execute("DELETE FROM ibkr_positions")

        logger.debug(f"[position_tracker] synced {len(positions)} position(s)")

    def _raise_short_alarm(self, shorts: dict) -> None:
        """A short in a long-only bot is a stop-everything event.

        Halts order submission and tells the operator. Deliberately does NOT try
        to close the positions: unwinding is a sizing and timing decision a human
        makes, and an automated cover here could just as easily make it worse.

        Alerting is throttled to once per SHORT_ALARM_COOLDOWN_HOURS per ticker
        set so a 5-minute sync loop cannot flood Telegram, but the pause is
        re-applied every cycle regardless of throttling.
        """
        detail = ", ".join(
            f"{t} {d['shares']:+.0f}sh (${d.get('market_value', 0):,.0f})"
            for t, d in sorted(shorts.items(), key=lambda kv: kv[1].get("market_value", 0))
        )
        logger.error(f"[position_tracker] SHORT POSITIONS DETECTED — halting trading: {detail}")

        # Pause first — do this every cycle, never throttled.
        try:
            from src import order_manager
            if not order_manager.is_paused():
                order_manager.set_paused(True)
                logger.error("[position_tracker] trading PAUSED due to short exposure")
        except Exception as e:
            logger.error(f"[position_tracker] could not pause trading: {e}")

        key = "|".join(sorted(shorts))
        try:
            cutoff = (datetime.now() - timedelta(hours=SHORT_ALARM_COOLDOWN_HOURS)).isoformat()
            with get_connection() as conn:
                recent = conn.execute(
                    "SELECT 1 FROM watchlist_alerts WHERE alert_type = 'short_position_alarm' "
                    "AND message = ? AND sent_at >= ? LIMIT 1",
                    (key, cutoff),
                ).fetchone()
                if recent:
                    return
                conn.execute(
                    "INSERT INTO watchlist_alerts (ticker, alert_type, message, sent_at, score, price) "
                    "VALUES (?, 'short_position_alarm', ?, ?, NULL, NULL)",
                    (sorted(shorts)[0], key, datetime.now().isoformat()),
                )
        except Exception as e:
            logger.warning(f"[position_tracker] short-alarm dedup failed: {e}")

        total_mv = sum(d.get("market_value", 0) for d in shorts.values())
        total_pnl = sum(d.get("unrealized_pnl", 0) for d in shorts.values())
        lines = [
            "🔴 SHORT POSITIONS DETECTED — TRADING PAUSED",
            f"This bot is long-only. {len(shorts)} short position(s) are open:",
            "",
        ]
        for t, d in sorted(shorts.items(), key=lambda kv: kv[1].get("market_value", 0)):
            lines.append(
                f"  {t}: {d['shares']:+.0f}sh  ${d.get('market_value', 0):,.0f}  "
                f"P&L ${d.get('unrealized_pnl', 0):,.0f}"
            )
        lines += [
            "",
            f"Total short exposure: ${total_mv:,.0f}",
            f"Unrealized: ${total_pnl:,.0f}",
            "",
            "🎯 Action: close these manually in TWS. Orders stay paused until you /resume.",
        ]
        try:
            from src.telegram_notifier import TelegramNotifier
            TelegramNotifier().send_message("\n".join(lines))
        except Exception as e:
            logger.error(f"[position_tracker] short alarm Telegram failed: {e}")

    def get_current_exposure(self, ticker: str) -> float:
        """Return market_value of the LONG position for ticker, or 0.0.

        Long-only semantics on purpose. A short has a negative market_value, and
        the Layer -1 SELL veto treats "exposure != 0" as "there is something to
        sell" — so reporting a short here would let a SELL through and deepen it.
        Callers that need the true signed value should read ibkr_positions.
        """
        with get_connection() as conn:
            row = conn.execute(
                "SELECT shares, market_value FROM ibkr_positions WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        if not row or (row["shares"] or 0) <= 0:
            return 0.0
        return float(row["market_value"])

    def get_portfolio_value(self) -> float:
        """Return net_liquidation — live from IBKR, fallback to DB.

        IBKR returns net_liquidation=0 pre-market or briefly after reconnect
        (the tag is absent from accountSummary until the session initialises).
        Treat 0 the same as an exception — fall through to the DB fallback so
        the execution engine never vetoes all trades due to a stale 0.0 value.
        """
        try:
            value = self.ibkr.get_account_summary()["net_liquidation"]
            if value > 0:
                return value
        except Exception:
            pass
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT net_liquidation FROM daily_pnl ORDER BY date DESC LIMIT 1"
                ).fetchone()
                if row:
                    return float(row["net_liquidation"])
        except Exception as e:
            logger.error(f"[position_tracker] get_portfolio_value failed: {e}")
        return 0.0

    def get_daily_pnl(self) -> float:
        """Return today's P&L — live from IBKR, fallback to DB."""
        try:
            return self.ibkr.get_daily_pnl()
        except Exception:
            pass
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT day_pnl FROM daily_pnl WHERE date = ?", (today,)
                ).fetchone()
                if row:
                    return float(row["day_pnl"])
        except Exception as e:
            logger.error(f"[position_tracker] get_daily_pnl failed: {e}")
        return 0.0

    @retry_on_busy()
    def record_daily_pnl(self) -> bool:
        """Write/update today's P&L row. Uses INSERT OR REPLACE so the last call of the day wins.

        Gated to after 09:30 ET — IB paper DailyPnL resets to 0 at midnight ET and
        doesn't start accumulating until the market opens. Recording before 09:30
        would persist 0 and the INSERT OR IGNORE would block all subsequent updates.

        Root cause of day_pnl always being 0.0 (audited 2026-07-03): ``DailyPnL`` is
        NOT reliably populated by ``ib.accountSummary()`` / ``ib.accountValues()`` —
        those snapshot APIs commonly return the tag as missing or "0" for paper
        accounts; the value only updates in real time via a dedicated ``ib.reqPnL()``
        subscription (see ``src/ibkr_realtime.py get_account_summary()``, which reads
        the tag as part of ``accountSummary()``). Evidence: 32/32 daily_pnl rows had
        day_pnl==0.0 while net_liquidation moved $100-$2600/day and order_log shows
        real fills (CALM, YORW, SPSC, EXPE, UBSI, EXC) across the same period.

        Fixable from this module: when the IBKR-reported day_pnl is 0/None/unavailable,
        fall back to computing it as the delta of today's net_liquidation vs. the most
        recent prior day's stored net_liquidation. This is an approximation (it
        includes the effect of any cash deposits/withdrawals, which paper accounts
        don't have) but is far more informative than a hardcoded 0 and keeps the
        Layer-0 daily-loss-limit veto from being permanently blind.

        NOT fixable here — flagged for src/ibkr_realtime.py: IBKRConnection should
        subscribe via ``ib.reqPnL(account)`` (once, at connect time) and expose the
        live ``PnL.dailyPnL`` value, e.g. a ``get_live_daily_pnl()`` method or by
        populating ``day_pnl`` in ``get_account_summary()`` from the PnL subscription
        object instead of (or in addition to) the ``DailyPnL`` accountSummary tag.
        """
        from datetime import time as _dtime
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.time() < _dtime(9, 30):
            logger.debug("[position_tracker] record_daily_pnl: before 09:30 ET, skipping")
            return False

        today = now_et.strftime("%Y-%m-%d")

        try:
            summary = self.ibkr.get_account_summary()
        except Exception as e:
            logger.error(f"[position_tracker] record_daily_pnl failed: {e}")
            return False

        day_pnl = summary.get("day_pnl") or 0.0
        net_liq = summary["net_liquidation"]
        source = "ibkr"

        if not day_pnl:
            fallback = self._compute_fallback_day_pnl(today, net_liq)
            if fallback is not None:
                # Sanity check: a very large fallback delta means the prior DB
                # row is from a different account (paper → live transition).
                # A delta > 50% of current NLV is almost certainly wrong.
                if net_liq > 0 and abs(fallback) > net_liq * 0.50:
                    logger.warning(
                        f"[position_tracker] fallback day_pnl={fallback:.0f} "
                        f"is >50% of NLV={net_liq:.0f} — prior DB row may be from "
                        f"a different account (paper→live transition?). Using 0."
                    )
                else:
                    day_pnl = fallback
                    source = "fallback_delta"

        now_iso = datetime.now().isoformat()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_pnl (date, day_pnl, net_liquidation, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (today, day_pnl, net_liq, now_iso),
            )

        logger.info(
            f"[position_tracker] daily_pnl updated: date={today} "
            f"pnl={day_pnl:.2f} nlv={net_liq:.2f} source={source}"
        )
        return True

    @staticmethod
    def _compute_fallback_day_pnl(today: str, net_liq: float) -> float | None:
        """Return net_liq - previous day's net_liquidation, or None if no prior row.

        Used when the IBKR-reported DailyPnL is 0/missing (see record_daily_pnl
        docstring for root cause). Looks up the most recent daily_pnl row with a
        date strictly before *today*.
        """
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT net_liquidation FROM daily_pnl WHERE date < ? "
                    "ORDER BY date DESC LIMIT 1",
                    (today,),
                ).fetchone()
        except Exception as e:
            logger.warning(f"[position_tracker] fallback day_pnl lookup failed: {e}")
            return None
        if not row or row["net_liquidation"] is None:
            return None
        return net_liq - float(row["net_liquidation"])
