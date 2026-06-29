"""
Position Tracker — syncs IBKR positions to DB and exposes exposure queries.

Called every 5 min from ibkr_worker.run_once() to keep ibkr_positions
and daily_pnl tables current.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.database import get_connection, retry_on_busy

logger = logging.getLogger(__name__)


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

    def get_current_exposure(self, ticker: str) -> float:
        """Return market_value of open position for ticker, or 0.0."""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT market_value FROM ibkr_positions WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        return float(row["market_value"]) if row else 0.0

    def get_portfolio_value(self) -> float:
        """Return net_liquidation — live from IBKR, fallback to DB."""
        try:
            return self.ibkr.get_account_summary()["net_liquidation"]
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

        now_iso = datetime.now().isoformat()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_pnl (date, day_pnl, net_liquidation, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (today, summary["day_pnl"], summary["net_liquidation"], now_iso),
            )

        logger.info(
            f"[position_tracker] daily_pnl updated: date={today} "
            f"pnl={summary['day_pnl']:.2f} nlv={summary['net_liquidation']:.2f}"
        )
        return True
