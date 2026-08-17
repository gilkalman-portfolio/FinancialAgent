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


def _pending_sell_shares_for(ticker: str) -> int:
    """Shares already committed to in-flight SELL orders (module-level helper)."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(shares), 0) AS n FROM order_log "
                "WHERE ticker = ? AND action = 'SELL' AND status = 'SUBMITTED'",
                (ticker,),
            ).fetchone()
        if row and row["n"] is not None:
            return max(0, int(row["n"]))
    except Exception as e:
        logger.warning(f"[order_manager] _pending_sell_shares_for({ticker}) failed: {e}")
    return 0


_POSITION_STALENESS_WARN_MINUTES = 10


def _held_shares_for(ticker: str) -> int:
    """Every software-driven exit sizes itself off this — submit_exit() is
    the single funnel they all route through (see its docstring). There is
    deliberately no freshness gate here: exits must never be blocked (see
    CLAUDE.md — the exit layer runs regardless of position-sync health,
    unlike BUY signals which do gate on `positions_fresh`). This only logs a
    warning when the underlying row is stale, so a genuinely out-of-date
    read is at least visible instead of silently trusted. See CLAUDE.md
    Incident Archive 2026-08-17."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT shares, last_synced FROM ibkr_positions WHERE ticker = ?", (ticker,)
            ).fetchone()
        if row and row["shares"] is not None:
            if row["last_synced"]:
                try:
                    age_min = (datetime.now() - datetime.fromisoformat(row["last_synced"])).total_seconds() / 60
                    if age_min > _POSITION_STALENESS_WARN_MINUTES:
                        logger.warning(
                            f"[order_manager] _held_shares_for({ticker}): position data is "
                            f"{age_min:.0f} min stale (last_synced={row['last_synced']})"
                        )
                except (ValueError, TypeError):
                    pass
            return int(float(row["shares"]))
    except Exception as e:
        logger.warning(f"[order_manager] _held_shares_for({ticker}) failed: {e}")
    return 0


def submit_exit(
    ibkr_client,
    ticker: str,
    shares: int,
    limit_price: float,
    reason: str,
) -> dict[str, Any]:
    """Guarded SELL for the worker's internal exit paths.

    Time stops, tiered exits and score-deterioration exits used to call
    ``place_limit_order()`` directly, bypassing both the trading pause and the
    long-only share arithmetic. That bypass is what allowed the 2026-08-03
    incident: ``_check_tiered_exits`` re-submitted the same 40% partial hundreds
    of times (BMY 34sh x 224, IT 14sh x 242) and sold straight through zero into
    a short, because nothing between it and the broker checked anything.

    Every exit now goes through here, which enforces, in order:
      1. the trading pause (a paused bot must not sell either — the broker-side
         GTC bracket stop still protects the position, so blocking software exits
         while halted costs nothing and stops runaway loops cold)
      2. shares <= held - already-working, so an exit can never cross zero
      3. order_log written BEFORE the broker call, so a crash mid-flight leaves a
         trackable row rather than an invisible order

    Returns {"status": PAUSED|VETOED|SUBMITTED|ERROR, ...}.
    """
    now = datetime.now().isoformat()

    if _trading_paused:
        _write_order_log(
            ticker=ticker, action="SELL", shares=0, entry_price=limit_price,
            stop_price=0, target_price=0, status="PAUSED",
            notes=f"{reason} — blocked: trading paused",
        )
        logger.warning(f"[order_manager] exit {ticker} PAUSED ({reason})")
        return {"status": "PAUSED", "ticker": ticker, "reason": "trading paused"}

    held = _held_shares_for(ticker)
    pending = _pending_sell_shares_for(ticker)
    sellable = min(int(shares), held - pending)

    if sellable <= 0:
        note = (f"{reason} — blocked: long-only guard "
                f"(held={held}, working={pending}, requested={shares})")
        _write_order_log(
            ticker=ticker, action="SELL", shares=0, entry_price=limit_price,
            stop_price=0, target_price=0, status="VETOED", notes=note,
        )
        logger.warning(f"[order_manager] exit {ticker} VETOED: {note}")
        return {"status": "VETOED", "ticker": ticker, "reason": note}

    if sellable < shares:
        logger.info(
            f"[order_manager] exit {ticker}: reducing {shares} -> {sellable} "
            f"(held={held}, working={pending})"
        )

    log_id = _write_order_log(
        ticker=ticker, action="SELL", shares=sellable, entry_price=limit_price,
        stop_price=0, target_price=0, status="SUBMITTED", notes=reason,
    )

    try:
        order_id = ibkr_client.place_limit_order(
            ticker=ticker, action="SELL", shares=sellable, limit_price=limit_price
        )
    except Exception as e:
        with get_connection() as conn:
            conn.execute(
                "UPDATE order_log SET status = 'ERROR', notes = ?, updated_at = ? WHERE id = ?",
                (f"{reason} — place failed: {e}", now, log_id),
            )
        logger.error(f"[order_manager] exit {ticker} ERROR: {e}")
        return {"status": "ERROR", "ticker": ticker, "reason": str(e)}

    with get_connection() as conn:
        conn.execute(
            "UPDATE order_log SET ibkr_order_id = ?, updated_at = ? WHERE id = ?",
            (order_id, now, log_id),
        )

    logger.info(
        f"[order_manager] exit {ticker} SUBMITTED: SELL {sellable}sh "
        f"@ ${limit_price:.2f} order_id={order_id} ({reason})"
    )
    return {"status": "SUBMITTED", "ticker": ticker, "shares": sellable,
            "order_id": order_id, "reason": reason}


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

        # Fetch actual account value so sizing reflects the real portfolio, not
        # the hardcoded $100k fallback in evaluate_trade().
        portfolio_value = 100_000.0
        if self.position_tracker is not None:
            try:
                v = self.position_tracker.get_portfolio_value()
                if v > 0:
                    portfolio_value = v
            except Exception as _pv_e:
                logger.warning(f"[order_manager] get_portfolio_value failed: {_pv_e}")

        veto_reasons: list[str] = []
        decision = self.engine.evaluate_trade(
            ticker, score_data, signal_type=action,
            portfolio_value=portfolio_value,
            portfolio_tickers=portfolio_tickers, reasons_out=veto_reasons,
        )

        if decision is None:
            reason = (
                "; ".join(veto_reasons)
                if veto_reasons
                else "execution engine vetoed (hard veto, confluence, or R:R)"
            )
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

        # SELL closes the full long position. Risk-based sizing (shares from
        # evaluate_trade) is a BUY-entry concept — for exits we always close
        # the entire held position. Layer -1 already vetoes when held == 0.
        if action == "SELL":
            held = self._held_shares(ticker)
            if held <= 0:
                reason = "No open position to sell (held shares == 0)"
                _write_order_log(
                    ticker=ticker, action=action, shares=0,
                    entry_price=price, stop_price=0, target_price=0,
                    status="VETOED", notes=reason,
                )
                logger.info(f"[order_manager] {ticker} VETOED: {reason}")
                return {"status": "VETOED", "reason": reason, "ticker": ticker}

            # LONG-ONLY INVARIANT. ibkr_positions is up to 5 min stale and does not
            # know about SELLs already working, so "sell everything held" can stack:
            # a signal SELL on top of a pending time-stop/tier SELL, or a bracket
            # child leg. Observed in paper — KSS sold 309 shares against 287 bought,
            # GTY 146/143, OMC 63/61. On a live margin account that opens a short.
            # Never send more than (held - already working).
            pending = self._pending_sell_shares(ticker)
            sellable = held - pending
            if sellable <= 0:
                reason = (
                    f"Long-only guard: {pending}sh SELL already working vs {held}sh held "
                    f"— refusing to sell more (would short)"
                )
                _write_order_log(
                    ticker=ticker, action=action, shares=0,
                    entry_price=price, stop_price=0, target_price=0,
                    status="VETOED", notes=reason,
                )
                logger.warning(f"[order_manager] {ticker} VETOED: {reason}")
                return {"status": "VETOED", "reason": reason, "ticker": ticker}

            if pending:
                logger.info(
                    f"[order_manager] {ticker}: {pending}sh SELL already working — "
                    f"reducing this exit from {held}sh to {sellable}sh"
                )
            shares = sellable  # close what is left, never more

        try:
            if action == "SELL":
                # Plain LMT SELL — no bracket. A SELL exits an existing long;
                # there is no stop/target geometry (that is BUY-entry only).
                order_id = self.ibkr.place_limit_order(
                    ticker=ticker,
                    action=action,
                    shares=shares,
                    limit_price=price,
                )
                log_stop = 0.0
                log_target = 0.0
            else:
                order_id = self.ibkr.place_bracket_order(
                    ticker=ticker,
                    action=action,
                    shares=shares,
                    entry_price=price,
                    stop_price=stop_price,
                    target_price=target_price,
                )
                log_stop = stop_price
                log_target = target_price
            _write_order_log(
                ticker=ticker,
                action=action,
                shares=shares,
                entry_price=price,
                stop_price=log_stop,
                target_price=log_target,
                status="SUBMITTED",
                ibkr_order_id=order_id,
            )
            logger.info(
                f"[order_manager] {ticker} SUBMITTED: {action} {shares} shares "
                f"entry=${price:.2f} stop=${log_stop:.2f} target=${log_target:.2f} "
                f"order_id={order_id}"
            )
            message = _format_submitted_message(
                action=action, ticker=ticker, shares=shares,
                entry_price=price, stop_price=log_stop,
                target_price=log_target, order_id=order_id,
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
            # SELL has no bracket geometry; BUY logs its intended stop/target.
            err_stop = 0.0 if action == "SELL" else stop_price
            err_target = 0.0 if action == "SELL" else target_price
            _write_order_log(
                ticker=ticker,
                action=action,
                shares=shares,
                entry_price=price,
                stop_price=err_stop,
                target_price=err_target,
                status="ERROR",
                notes=str(e),
            )
            logger.error(f"[order_manager] {ticker} ERROR: {e}")
            return {"status": "ERROR", "reason": str(e), "ticker": ticker}

    def _held_shares(self, ticker: str) -> int:
        """Currently-held share count for a ticker from the ibkr_positions DB.

        position_tracker.sync_positions() refreshes this table at the start of
        each worker cycle, so it reflects fresh IBKR positions. Reuses the same
        ibkr_positions read pattern already used for portfolio_tickers above.
        Returns 0 on any error (Layer -1 veto is the primary no-position guard).
        """
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT shares FROM ibkr_positions WHERE ticker = ?",
                    (ticker,),
                ).fetchone()
            if row and row["shares"] is not None:
                return max(0, int(float(row["shares"])))
        except Exception as e:
            logger.warning(f"[order_manager] _held_shares({ticker}) failed: {e}")
        return 0

    def _pending_sell_shares(self, ticker: str) -> int:
        """Shares already committed to in-flight SELL orders for this ticker.

        Counts SUBMITTED rows in order_log — signal exits, time stops, tier
        exits and score-deterioration exits all write one before calling IBKR,
        so this covers every path that can reduce the position. Returns 0 on
        error, which degrades to the previous (unclamped) behaviour rather than
        blocking a legitimate exit.
        """
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(shares), 0) AS n FROM order_log "
                    "WHERE ticker = ? AND action = 'SELL' AND status = 'SUBMITTED'",
                    (ticker,),
                ).fetchone()
            if row and row["n"] is not None:
                return max(0, int(row["n"]))
        except Exception as e:
            logger.warning(f"[order_manager] _pending_sell_shares({ticker}) failed: {e}")
        return 0

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

        # combined_buy has no composite-score gate by design (2026-06-03,
        # Supertrend flip is the sole trigger) — a ticker discovered ONLY
        # through that path, never independently scanned by run_scan(), and
        # with no composite_score on the alert either, reaches here as
        # base == {"price": price} and nothing else. normalize_score_data()
        # below then fills every other field with a plausible-looking
        # default (fundamentals_score=5, etc.) rather than "unknown". This is
        # currently intentional (the Supertrend-only path is meant to trade
        # without composite data) and today's Track A confluence constants
        # happen to reject a fully data-free profile — but that's an
        # arithmetic coincidence, not a deliberate floor. If those constants
        # are ever retuned, a completely data-free ticker could size a real
        # bracket order on fabricated inputs with no explicit gate catching
        # it. Flagged, not fixed — see CLAUDE.md Incident Archive 2026-08-17.
        if base == {"price": price}:
            logger.debug(
                f"[order_manager] {ticker}: no scan_results at all — score_data will be "
                "fully synthetic after normalize_score_data() defaults"
            )

        return self.engine.normalize_score_data(base)
