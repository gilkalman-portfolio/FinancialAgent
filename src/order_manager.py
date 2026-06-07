"""
Order Manager — bridges signal_combiner alerts to IBKR bracket orders.

Safety defaults:
  - paper_mode=True always unless IBKR_LIVE=true env var is explicitly set
  - All submissions go through execution_engine.evaluate_trade() veto checks
  - Every attempt is logged to the order_log DB table
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from src.database import get_connection, retry_on_busy
from src.signal_combiner import CombinedAlert

logger = logging.getLogger(__name__)

# ── Manual pause via Telegram /pause command ─────────────────────────────
_trading_paused: bool = False


def set_paused(paused: bool) -> None:
    global _trading_paused
    _trading_paused = paused
    logger.info(f"[order_manager] trading_paused set to {paused}")


def is_paused() -> bool:
    return _trading_paused


@retry_on_busy()
def _write_order_log(
    ticker: str,
    action: str,
    shares: int,
    entry_price: float,
    stop_price: float,
    target_price: float,
    status: str,
    fill_price: float | None = None,
    ibkr_order_id: int | None = None,
    notes: str | None = None,
) -> int:
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO order_log (
                ticker, action, shares, entry_price, stop_price, target_price,
                status, fill_price, ibkr_order_id, created_at, updated_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker, action, shares, entry_price, stop_price, target_price,
                status, fill_price, ibkr_order_id, now, now, notes,
            ),
        )
        return cur.lastrowid


def _format_submitted_message(
    action: str,
    ticker: str,
    shares: int,
    entry_price: float,
    stop_price: float,
    target_price: float,
    order_id: int,
) -> str:
    """Build a Telegram-ready SUBMITTED message."""
    if action == "BUY":
        return (
            f"✅ FinancialAgent — BUY {ticker}\n"
            f"💰 Entry: ${entry_price:.2f} | Shares: {shares}\n"
            f"🎯 Stop: ${stop_price:.2f} | Target: ${target_price:.2f}\n"
            f"📊 Cost basis: ${entry_price * shares:,.2f}\n"
            f"🔑 Order ID: {order_id}"
        )

    # SELL — look up position for P&L
    avg_cost = 0.0
    current_shares = 0.0
    position_found = False
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT shares, avg_cost FROM ibkr_positions WHERE ticker = ?",
                (ticker,),
            ).fetchone()
            if row:
                avg_cost = float(row["avg_cost"])
                current_shares = float(row["shares"])
                position_found = True
    except Exception:
        pass

    if position_found and avg_cost > 0:
        pnl = (entry_price - avg_cost) * shares
        pnl_pct = (entry_price - avg_cost) / avg_cost * 100
        sign = "+" if pnl >= 0 else ""
        pnl_str = f"{sign}${pnl:,.2f} ({sign}{pnl_pct:.1f}%)"
    else:
        pnl_str = "N/A (no open position)"
        avg_cost = 0.0
        current_shares = 0.0

    remaining = max(0, int(current_shares - shares))

    lines = [
        f"✅ FinancialAgent — SELL {ticker}",
        f"💰 Exit: ${entry_price:.2f} | Shares: {shares}",
    ]
    if position_found:
        lines.append(f"📊 P&L: {pnl_str} (vs avg cost ${avg_cost:.2f})")
        lines.append(f"📉 Position after: {remaining} shares remaining")
    else:
        lines.append(f"📊 P&L: {pnl_str}")
    lines.append(f"🔑 Order ID: {order_id}")
    return "\n".join(lines)


class OrderManager:
    def __init__(
        self,
        ibkr_client,
        execution_engine_module,
        paper_mode: bool = True,
        position_tracker=None,
    ):
        self.ibkr = ibkr_client
        self.engine = execution_engine_module
        self.paper_mode = paper_mode
        self.position_tracker = position_tracker

        # Inject position_tracker into execution engine for daily loss limit
        if position_tracker is not None:
            self.engine.set_position_tracker(position_tracker)

    def submit(self, combined_alert: CombinedAlert) -> dict[str, Any]:
        """Evaluate trade via execution engine, then submit bracket order if approved."""
        ticker = combined_alert.ticker
        price = combined_alert.entry_price

        if _trading_paused:
            action = "BUY" if combined_alert.alert_type == "combined_buy" else "SELL"
            _write_order_log(
                ticker=ticker, action=action, shares=0,
                entry_price=price, stop_price=0, target_price=0,
                status="PAUSED", notes="Manual pause via Telegram",
            )
            logger.info(f"[order_manager] {ticker} PAUSED: trading paused via Telegram")
            return {"status": "PAUSED", "reason": "Manual pause via Telegram", "ticker": ticker}

        action = "BUY" if combined_alert.alert_type == "combined_buy" else "SELL"

        # Build score_data from the latest scan context
        score_data = self._build_score_data(ticker, price, combined_alert.composite_score)

        # Fetch active positions for sector concentration check
        portfolio_tickers = []
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT ticker FROM ibkr_positions WHERE shares > 0"
                ).fetchall()
                portfolio_tickers = [r["ticker"] for r in rows]
        except Exception as e:
            logger.warning(f"[order_manager] could not fetch portfolio_tickers: {e}")

        decision = self.engine.evaluate_trade(ticker, score_data, signal_type=action, portfolio_tickers=portfolio_tickers)

        if decision is None:
            reason = "execution engine vetoed (hard veto, confluence, or R:R)"
            _write_order_log(
                ticker=ticker,
                action=action,
                shares=0,
                entry_price=price,
                stop_price=0,
                target_price=0,
                status="VETOED",
                notes=reason,
            )
            logger.info(f"[order_manager] {ticker} VETOED: {reason}")
            return {"status": "VETOED", "reason": reason, "ticker": ticker}

        # Live-mode safety gate
        if not self.paper_mode:
            if os.environ.get("IBKR_LIVE", "").lower() != "true":
                raise RuntimeError(
                    "paper_mode=False but IBKR_LIVE env var is not 'true'. "
                    "Refusing to submit live orders without explicit env flag."
                )

        sizing = decision["sizing"]
        shares = sizing["shares"]
        stop_price = sizing["stop_price"]
        target_price = sizing["target_price"]

        try:
            order_id = self.ibkr.place_bracket_order(
                ticker=ticker,
                action=action,
                shares=shares,
                entry_price=price,
                stop_price=stop_price,
                target_price=target_price,
            )
            _write_order_log(
                ticker=ticker,
                action=action,
                shares=shares,
                entry_price=price,
                stop_price=stop_price,
                target_price=target_price,
                status="SUBMITTED",
                ibkr_order_id=order_id,
            )
            logger.info(
                f"[order_manager] {ticker} SUBMITTED: {action} {shares} shares "
                f"entry=${price:.2f} stop=${stop_price:.2f} target=${target_price:.2f} "
                f"order_id={order_id}"
            )
            message = _format_submitted_message(
                action=action, ticker=ticker, shares=shares,
                entry_price=price, stop_price=stop_price,
                target_price=target_price, order_id=order_id,
            )
            return {
                "status": "SUBMITTED",
                "order_id": order_id,
                "ticker": ticker,
                "shares": shares,
                "action": action,
                "message": message,
            }
        except Exception as e:
            _write_order_log(
                ticker=ticker,
                action=action,
                shares=shares,
                entry_price=price,
                stop_price=stop_price,
                target_price=target_price,
                status="ERROR",
                notes=str(e),
            )
            logger.error(f"[order_manager] {ticker} ERROR: {e}")
            return {"status": "ERROR", "reason": str(e), "ticker": ticker}

    def _build_score_data(
        self, ticker: str, price: float, composite_score: float | None
    ) -> dict[str, Any]:
        """Pull the latest scan_results raw_data and merge with alert context."""
        import json

        with get_connection() as conn:
            row = conn.execute(
                "SELECT raw_data, explosion_score FROM scan_results "
                "WHERE ticker = ? ORDER BY scanned_at DESC LIMIT 1",
                (ticker,),
            ).fetchone()

        base: dict[str, Any] = {"price": price}
        if row and row["raw_data"]:
            try:
                base = {**json.loads(row["raw_data"]), "price": price}
            except json.JSONDecodeError:
                pass

        if composite_score is not None:
            base["score"] = composite_score
            base["explosion_score"] = composite_score

        return self.engine.normalize_score_data(base)
