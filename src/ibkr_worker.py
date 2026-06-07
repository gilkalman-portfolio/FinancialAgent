"""
IBKR Worker — Python 3.13 daemon that polls Supertrend on the monitoring queue.

Runs as a separate process (uses .venv313 because ib_async needs Python <= 3.13):

    .venv313\\Scripts\\python.exe -m src.ibkr_worker

Loop (every POLL_INTERVAL_SECS):
  1. Build monitoring queue from DB (scanner + watchlist + liquidity gate).
  2. For each ticker, pull 1H bars from IBKR.
  3. Run Supertrend (ATR=10, mult=3.0).
  4. If signal is BUY or SELL on the LAST closed bar → forward to
     src.signal_combiner.evaluate(). It handles cap, dedup, and persistence.
  5. If combined alert fires, send to Telegram (if env enabled).

The worker writes to the same SQLite DB as the main scheduler — they are
fully decoupled.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as _dtime, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _is_signal_hours() -> bool:
    """Only fire signals during extended US market hours (04:00–20:00 ET)."""
    now_et = datetime.now(_ET).time()
    return _dtime(4, 0) <= now_et <= _dtime(20, 0)
from typing import Optional

from src.database import get_connection, init_db, retry_on_busy
from src.forward_signals import record_fill
from src.ibkr_realtime import IBKRConnection, PAPER_PORT
from src.monitoring_queue import build_queue
from src.position_tracker import PositionTracker
from src.signal_combiner import SupertrendEvent, evaluate
from src.supertrend import supertrend

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 5 * 60          # 5 minutes
FILL_SWEEP_INTERVAL_SECS = 30 * 60  # 30 minutes
FILL_SWEEP_MIN_AGE_SECS = 5 * 60    # only sweep SUBMITTED rows older than 5 min

_last_fill_sweep_ts: datetime | None = None
BAR_SIZE = "1 hour"
HISTORICAL_DURATION = "10 D"
ATR_PERIOD = 10
ATR_MULTIPLIER = 3.0
CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "1"))
PORT = int(os.environ.get("IBKR_PORT", str(PAPER_PORT)))


def _send_telegram(message: str) -> None:
    """Best-effort Telegram send — failures are logged, never block the loop."""
    try:
        from src.telegram_notifier import TelegramNotifier
        TelegramNotifier().send_message(message)
    except Exception as e:
        logger.warning(f"[worker] telegram send failed: {e}")


@retry_on_busy()
def _update_order_log(ibkr_order_id: int, status: str,
                      fill_price: float | None = None,
                      notes: str | None = None) -> None:
    """Update order_log row when IBKR reports a status change."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE order_log SET status = ?, fill_price = COALESCE(?, fill_price), "
            "updated_at = ?, notes = COALESCE(?, notes) "
            "WHERE ibkr_order_id = ? AND status NOT IN ('FILLED', 'CANCELLED', 'ERROR')",
            (status, fill_price, now, notes, ibkr_order_id),
        )


def _on_order_status(trade) -> None:
    """ib_async orderStatusEvent callback — routes fills and cancellations."""
    try:
        status_str = trade.orderStatus.status
        order_id = trade.order.orderId
        ticker = trade.contract.symbol
        fill_price = trade.orderStatus.avgFillPrice

        if status_str == "Filled":
            _update_order_log(order_id, "FILLED", fill_price=fill_price)
            record_fill(ticker, fill_price, order_id)
            _send_telegram(
                f"💰 ORDER FILLED — {ticker}\n"
                f"Fill price: ${fill_price:.2f} | Order ID: {order_id}"
            )
            logger.info(f"[worker] order FILLED: {ticker} @ ${fill_price:.2f} id={order_id}")

        elif status_str == "Cancelled":
            _update_order_log(order_id, "CANCELLED")
            logger.info(f"[worker] order CANCELLED: {ticker} id={order_id}")

        elif status_str in ("Inactive", "ApiCancelled"):
            _update_order_log(order_id, "ERROR", notes=status_str)
            logger.warning(f"[worker] order {status_str}: {ticker} id={order_id}")

    except Exception as e:
        logger.error(f"[worker] _on_order_status error: {e}")


@retry_on_busy()
def _reconcile_orders_on_startup(conn: IBKRConnection) -> None:
    """Reconcile order_log SUBMITTED rows against live IBKR open orders.

    Called once after initial connect, before the main loop.
    Marks SUBMITTED rows as ERROR if the order no longer exists on IBKR.
    """
    try:
        live_orders = conn.get_open_orders()
    except Exception as e:
        logger.warning(f"[worker] startup reconciliation skipped — get_open_orders failed: {e}")
        return

    live_order_ids = {o["order_id"] for o in live_orders}

    with get_connection() as db:
        rows = db.execute(
            "SELECT id, ticker, ibkr_order_id FROM order_log WHERE status = 'SUBMITTED'"
        ).fetchall()

    if not rows:
        logger.info("[worker] startup reconciliation: no SUBMITTED rows to reconcile")
        return

    reconciled = 0
    now = datetime.now().isoformat()
    with get_connection() as db:
        for row in rows:
            ibkr_id = row["ibkr_order_id"]
            if ibkr_id is not None and ibkr_id not in live_order_ids:
                db.execute(
                    "UPDATE order_log SET status = 'ERROR', notes = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'SUBMITTED'",
                    ("Not found on reconnect", now, row["id"]),
                )
                reconciled += 1
                logger.info(
                    f"[worker] reconciled order_log id={row['id']} "
                    f"ticker={row['ticker']} ibkr_id={ibkr_id} → ERROR"
                )

    still_live = len(rows) - reconciled
    logger.info(
        f"[worker] startup reconciliation complete: "
        f"{reconciled} marked ERROR, {still_live} still live, "
        f"{len(live_order_ids)} open orders on IBKR"
    )


def _periodic_fill_sweep(conn: IBKRConnection) -> None:
    """Sweep SUBMITTED order_log rows to catch fills missed by callbacks.

    Runs at most every FILL_SWEEP_INTERVAL_SECS. Checks SUBMITTED rows
    older than FILL_SWEEP_MIN_AGE_SECS against IBKR open orders and fills.
    """
    global _last_fill_sweep_ts

    now = datetime.now()
    if _last_fill_sweep_ts is not None:
        elapsed = (now - _last_fill_sweep_ts).total_seconds()
        if elapsed < FILL_SWEEP_INTERVAL_SECS:
            return
    _last_fill_sweep_ts = now

    logger.info("[worker] periodic fill sweep starting")

    # 1. Find SUBMITTED rows older than 5 min
    age_cutoff = (now - timedelta(seconds=FILL_SWEEP_MIN_AGE_SECS)).isoformat()
    with get_connection() as db:
        rows = db.execute(
            "SELECT id, ticker, ibkr_order_id FROM order_log "
            "WHERE status = 'SUBMITTED' AND created_at <= ?",
            (age_cutoff,),
        ).fetchall()

    if not rows:
        logger.info("[worker] fill sweep: no stale SUBMITTED rows")
        return

    # 2. Get IBKR open orders
    try:
        live_orders = conn.get_open_orders()
    except Exception as e:
        logger.warning(f"[worker] fill sweep aborted — get_open_orders failed: {e}")
        return
    live_order_ids = {o["order_id"] for o in live_orders}

    # 3. Get session fills from IBKR
    fill_map: dict[int, float] = {}  # order_id → avg fill price
    try:
        for fill in conn.ib.fills():
            oid = fill.execution.orderId
            fill_map[oid] = float(fill.execution.avgPrice)
    except Exception as e:
        logger.warning(f"[worker] fill sweep — ib.fills() failed: {e}")
        # Continue — we can still detect orders gone from open list

    updated = 0
    filled = 0
    errored = 0
    update_ts = now.isoformat()

    with get_connection() as db:
        for row in rows:
            ibkr_id = row["ibkr_order_id"]
            if ibkr_id is None:
                continue

            if ibkr_id in live_order_ids:
                continue  # still open — leave as SUBMITTED

            # Order is no longer open — check if it was filled
            if ibkr_id in fill_map:
                fp = fill_map[ibkr_id]
                db.execute(
                    "UPDATE order_log SET status = 'FILLED', fill_price = ?, "
                    "updated_at = ?, notes = ? "
                    "WHERE id = ? AND status = 'SUBMITTED'",
                    (fp, update_ts, "Caught by periodic fill sweep", row["id"]),
                )
                record_fill(row["ticker"], fp, ibkr_id)
                filled += 1
                logger.info(
                    f"[worker] fill sweep: order id={row['id']} ticker={row['ticker']} "
                    f"FILLED @ ${fp:.2f} (ibkr_id={ibkr_id})"
                )
            else:
                # Not open, not in fills → mark ERROR
                db.execute(
                    "UPDATE order_log SET status = 'ERROR', updated_at = ?, notes = ? "
                    "WHERE id = ? AND status = 'SUBMITTED'",
                    (update_ts, "Gone from open orders (fill sweep)", row["id"]),
                )
                errored += 1
                logger.info(
                    f"[worker] fill sweep: order id={row['id']} ticker={row['ticker']} "
                    f"→ ERROR (ibkr_id={ibkr_id} not in open or fills)"
                )

            updated += 1

    logger.info(
        f"[worker] fill sweep complete: checked {len(rows)} stale rows, "
        f"{filled} filled, {errored} errored, {len(rows) - updated} unchanged"
    )


def _check_ticker(conn: IBKRConnection, ticker: str) -> Optional[SupertrendEvent]:
    """Fetch bars, compute Supertrend, return a flip event or None."""
    try:
        df = conn.historical_bars(ticker, bar_size=BAR_SIZE, duration=HISTORICAL_DURATION)
    except Exception as e:
        logger.warning(f"[worker] {ticker}: historical_bars failed: {e}")
        return None

    if df.empty or len(df) < ATR_PERIOD + 2:
        return None

    result = supertrend(df, period=ATR_PERIOD, multiplier=ATR_MULTIPLIER)
    signal = result.get("signal")
    if signal not in ("BUY", "SELL"):
        return None

    # Reject stale flips — only fire on the bar that actually flipped (bars_ago == 1).
    # bars_ago > 1 means the flip happened in a previous bar; firing it again each cycle
    # is the primary source of duplicate BUY/SELL alerts.
    bars_ago = int(result.get("bars_ago", 0))
    if bars_ago != 1:
        logger.debug(f"[worker] {ticker}: stale flip (bars_ago={bars_ago}) — skipping")
        return None

    if not _is_signal_hours():
        logger.debug(f"[worker] {ticker}: outside signal hours (ET) — skipping")
        return None

    last_price = float(df["Close"].iloc[-1])
    return SupertrendEvent(
        ticker=ticker,
        direction=result["direction"],
        signal=signal,
        level=float(result["level"]),
        last_price=last_price,
        bars_ago=int(result.get("bars_ago", 0)),
    )


def run_once(conn: IBKRConnection) -> int:
    """One pass over the monitoring queue. Returns number of alerts fired."""
    queue = build_queue()
    if not queue:
        logger.info("[worker] queue empty — nothing to check")
        return 0

    tracker = PositionTracker(conn)

    # Sync positions BEFORE processing signals so Layer -1 veto checks use fresh data.
    # Without this, get_current_exposure() reads from the previous cycle (up to 5 min stale),
    # causing SELL orders to bypass the "no open position" veto when a position was closed
    # between cycles (e.g. stop hit, order cancelled by IBKR).
    try:
        tracker.sync_positions()
    except Exception as e:
        logger.warning(f"[worker] position pre-sync failed: {e}")

    fired = 0
    for entry in queue:
        event = _check_ticker(conn, entry.ticker)
        if event is None:
            continue
        alert = evaluate(event)
        if alert is None:
            continue
        logger.info(f"[worker] ALERT {alert.alert_type} {alert.ticker} @ ${alert.entry_price:.2f}")
        _send_telegram(alert.message)

        # Order submission (Phase 1)
        try:
            result = _submit_order(conn, alert, tracker)
            if result["status"] == "VETOED":
                _send_telegram(
                    f"⛔ ORDER VETOED — {alert.ticker}\n"
                    f"Reason: {result.get('reason', 'unknown')}"
                )
            elif result["status"] == "SUBMITTED":
                _send_telegram(result.get("message", f"✅ ORDER SUBMITTED — {alert.ticker}"))
            elif result["status"] == "PAUSED":
                _send_telegram(
                    f"⏸ ORDER PAUSED — {alert.ticker}\n"
                    f"Trading is paused via Telegram. Send /resume to re-enable."
                )
            elif result["status"] == "ERROR":
                logger.error(f"[worker] order error for {alert.ticker}: {result.get('reason')}")
        except Exception as oe:
            logger.error(f"[worker] order submission failed for {alert.ticker}: {oe}")

        fired += 1

    try:
        tracker.record_daily_pnl()
    except Exception as e:
        logger.warning(f"[worker] daily pnl record failed: {e}")

    # Periodic fill sweep — catch fills missed by orderStatusEvent callbacks
    try:
        _periodic_fill_sweep(conn)
    except Exception as e:
        logger.warning(f"[worker] fill sweep failed: {e}")

    return fired


def _submit_order(conn: IBKRConnection, alert, tracker: PositionTracker | None = None) -> dict:
    """Submit order via OrderManager — lazy-initialized per cycle."""
    import src.execution_engine as engine
    from src.order_manager import OrderManager

    paper = os.environ.get("IBKR_LIVE", "").lower() != "true"
    mgr = OrderManager(
        ibkr_client=conn,
        execution_engine_module=engine,
        paper_mode=paper,
        position_tracker=tracker,
    )
    return mgr.submit(alert)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    init_db()
    logger.info(f"[worker] starting — port={PORT} clientId={CLIENT_ID} interval={POLL_INTERVAL_SECS}s")

    # Start Telegram command handler
    cmd_handler = None
    try:
        from src.telegram_command_handler import TelegramCommandHandler
        from src.telegram_notifier import TelegramNotifier
        import src.order_manager as order_manager_mod

        _placeholder_conn = IBKRConnection(port=PORT, client_id=CLIENT_ID + 10)
        _placeholder_tracker = PositionTracker(_placeholder_conn)
        cmd_handler = TelegramCommandHandler(
            telegram_notifier=TelegramNotifier(),
            order_manager_module=order_manager_mod,
            position_tracker=_placeholder_tracker,
            ibkr_client=_placeholder_conn,
        )
        cmd_handler.start()
    except Exception as e:
        logger.warning(f"[worker] telegram command handler init failed: {e}")

    # ── Startup reconciliation: check SUBMITTED orders against IBKR ────
    try:
        startup_conn = IBKRConnection(port=PORT, client_id=CLIENT_ID)
        startup_conn.connect(timeout=15.0)
        try:
            _reconcile_orders_on_startup(startup_conn)
        finally:
            startup_conn.disconnect()
    except Exception as e:
        logger.warning(f"[worker] startup reconciliation failed: {e}")

    try:
        while True:
            cycle_start = datetime.now()
            try:
                conn = IBKRConnection(port=PORT, client_id=CLIENT_ID)
                conn.connect(timeout=15.0)

                # Subscribe to order status events for fill/cancel callbacks
                conn.ib.orderStatusEvent += _on_order_status

                # Update command handler with live connection each cycle
                if cmd_handler is not None:
                    cmd_handler._ibkr = conn
                    cmd_handler._tracker = PositionTracker(conn)

                try:
                    fired = run_once(conn)
                    logger.info(f"[worker] cycle done: {fired} alert(s) fired")
                finally:
                    conn.ib.orderStatusEvent -= _on_order_status
                    conn.disconnect()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"[worker] cycle failed: {type(e).__name__}: {e}")

            elapsed = (datetime.now() - cycle_start).total_seconds()
            sleep_for = max(0, POLL_INTERVAL_SECS - elapsed)
            logger.info(f"[worker] sleeping {sleep_for:.0f}s...")
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        logger.info("[worker] interrupted — exiting")
    finally:
        if cmd_handler is not None:
            cmd_handler.stop()

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
