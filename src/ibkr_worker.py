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


def _is_signal_hours(signal_type: str = "BUY") -> bool:
    """Only fire signals during US market hours on a weekday.

    BUY entries: 09:30–20:00 ET — regular session open onwards. Pre-market
    liquidity on most names is too thin for reliable LMT fills, and the ATR
    stop is calibrated on prior-session bars (not pre-market volatility).

    SELL exits: 04:00–20:00 ET — exits are allowed in pre-market/AH so an
    exit signal is never suppressed while a position is being squeezed.

    The weekday check prevents weekend stale-flip duplicates — IBKR serves
    the same frozen Friday-close bar Sat/Sun, which otherwise re-fires the
    same combined_buy/sell every poll (real incident: EPAM Fri/Sat/Sun/Mon).
    """
    now = datetime.now(_ET)
    if now.weekday() >= 5:   # 5 = Saturday, 6 = Sunday
        return False
    t = now.time()
    if signal_type == "SELL":
        return _dtime(4, 0) <= t <= _dtime(20, 0)
    return _dtime(9, 30) <= t <= _dtime(20, 0)
from typing import Optional

from src.database import get_connection, init_db, retry_on_busy
from src.forward_signals import record_fill
from src.ibkr_realtime import IBKRConnection, PAPER_PORT
from src.monitoring_queue import build_queue
from src.order_manager import submit_exit
from src.position_tracker import PositionTracker
from src.signal_combiner import SupertrendEvent, evaluate
from src.supertrend import supertrend

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 5 * 60          # 5 minutes
FILL_SWEEP_INTERVAL_SECS = 30 * 60  # 30 minutes
FILL_SWEEP_MIN_AGE_SECS = 5 * 60    # only sweep SUBMITTED rows older than 5 min
# Grace period before a SUBMITTED row with no matching execution is called ERROR.
# IBKR serves ~24h of executions via reqExecutions(), so anything younger than this
# may simply not be visible yet — calling it ERROR early corrupts fill tracking.
FILL_SWEEP_UNKNOWN_GRACE_SECS = 24 * 60 * 60
PER_CYCLE_BUY_CAP = 3               # max combined_buy signals per run_once() cycle

_last_fill_sweep_ts: datetime | None = None
_last_trailing_stop_ts: datetime | None = None
_last_time_stop_ts: datetime | None = None
BAR_SIZE = "1 hour"
HISTORICAL_DURATION = "10 D"
ATR_PERIOD = 10
ATR_MULTIPLIER = 3.0
TRAILING_ATR_MULTIPLIER = 2.5   # trailing stop = price - 2.5 * ATR_1h
TRAILING_STOP_INTERVAL_SECS = 15 * 60   # update trailing stops every 15 min
TIME_STOP_DAYS = 15                      # exit zombie if held > 15 trading days
TIME_STOP_BAND_PCT = 3.0                 # zombie: -3% < pnl < +3%
TIME_STOP_INTERVAL_SECS = 4 * 60 * 60   # check time stops every 4 hours
TIER1_PCT = 7.0   # +7%: sell 40%, move stop to breakeven
TIER2_PCT = 14.0  # +14%: sell 30% of original, trail rest
CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "1"))
PORT = int(os.environ.get("IBKR_PORT", str(PAPER_PORT)))


def _atr(df, period: int = ATR_PERIOD) -> float:
    """Wilder's ATR (last value) — matches supertrend.py convention."""
    import pandas as pd
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])


def _send_telegram(message: str) -> Optional[bool]:
    """Best-effort Telegram send — failures are logged, never block the loop.

    Returns a tri-state so callers can distinguish a genuine send FAILURE from a
    disabled channel:
      - True  → message was sent successfully
      - False → send was ATTEMPTED but failed (network error / HTTP 400 from a
                malformed Markdown message) — the caller may roll back state
      - None  → Telegram is disabled or the notifier could not init (no-op; do
                NOT treat as a failure, otherwise a disabled channel would cause
                infinite retries)
    """
    try:
        from src.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier()
        if not getattr(notifier, "enabled", True):
            return None
        return notifier.send_message(message)
    except Exception as e:
        logger.warning(f"[worker] telegram send failed: {e}")
        return False


@retry_on_busy()
def _update_order_log(ibkr_order_id: int, status: str,
                      fill_price: float | None = None,
                      notes: str | None = None) -> None:
    """Update order_log row when IBKR reports a status change.

    WHERE clause is status-dependent to handle the bracket-order race condition:
    TWS sometimes fires Cancelled for the parent order (on reconnect/reconcile)
    before (or concurrently with) the Filled callback, causing CANCELLED to block
    the subsequent FILLED update. Fix:
      - FILLED overrides CANCELLED (fill always wins)
      - CANCELLED only transitions from SUBMITTED (never overwrites FILLED)
    """
    now = datetime.now().isoformat()
    if status == "FILLED":
        where_clause = "WHERE ibkr_order_id = ? AND status NOT IN ('FILLED', 'ERROR')"
    elif status == "CANCELLED":
        where_clause = "WHERE ibkr_order_id = ? AND status = 'SUBMITTED'"
    else:
        where_clause = "WHERE ibkr_order_id = ? AND status NOT IN ('FILLED', 'CANCELLED', 'ERROR')"
    with get_connection() as conn:
        conn.execute(
            f"UPDATE order_log SET status = ?, fill_price = COALESCE(?, fill_price), "
            f"updated_at = ?, notes = COALESCE(?, notes) {where_clause}",
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

    Recovers fills that completed while the worker was down: an order missing from
    IBKR's open-order list is looked up in the broker's execution history and
    marked FILLED when found. Anything still unexplained is LEFT as SUBMITTED for
    _periodic_fill_sweep to age out through its grace window.

    This function used to mark every missing order ERROR outright. Because the
    watchdog restarts the worker on every clean exit, that ran often and was the
    main producer of the 30 bogus ERROR rows found on 2026-08-05 — 14 of which
    were real fills for tickers still held. Never destroy fill state here.
    """
    try:
        live_orders = conn.get_open_orders()
    except Exception as e:
        logger.warning(f"[worker] startup reconciliation skipped — get_open_orders failed: {e}")
        return

    live_order_ids = {o["order_id"] for o in live_orders}

    try:
        fill_map = conn.get_executions()
    except Exception as e:
        logger.warning(f"[worker] startup reconciliation — get_executions failed: {e}")
        fill_map = {}

    with get_connection() as db:
        rows = db.execute(
            "SELECT id, ticker, ibkr_order_id FROM order_log WHERE status = 'SUBMITTED'"
        ).fetchall()

    if not rows:
        logger.info("[worker] startup reconciliation: no SUBMITTED rows to reconcile")
        return

    recovered = 0
    pending = 0
    now = datetime.now().isoformat()
    with get_connection() as db:
        for row in rows:
            ibkr_id = row["ibkr_order_id"]
            if ibkr_id is None or ibkr_id in live_order_ids:
                continue

            if ibkr_id in fill_map:
                fp = fill_map[ibkr_id]
                db.execute(
                    "UPDATE order_log SET status = 'FILLED', fill_price = ?, "
                    "notes = ?, updated_at = ? WHERE id = ? AND status = 'SUBMITTED'",
                    (fp, "Recovered by startup reconciliation", now, row["id"]),
                )
                record_fill(row["ticker"], fp, ibkr_id)
                recovered += 1
                logger.info(
                    f"[worker] reconciled order_log id={row['id']} ticker={row['ticker']} "
                    f"ibkr_id={ibkr_id} → FILLED @ ${fp:.2f}"
                )
            else:
                pending += 1
                logger.info(
                    f"[worker] reconciliation: id={row['id']} ticker={row['ticker']} "
                    f"ibkr_id={ibkr_id} not open and not in executions — leaving SUBMITTED"
                )

    logger.info(
        f"[worker] startup reconciliation complete: {recovered} recovered as FILLED, "
        f"{pending} left SUBMITTED for the fill sweep, "
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
            "SELECT id, ticker, ibkr_order_id, created_at FROM order_log "
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

    # 3. Get executions from IBKR. Uses reqExecutions() (broker-side, survives
    # reconnects) merged with in-session fills — ib.fills() alone is empty after
    # a Gateway restart, which is what caused real fills to be marked ERROR.
    fill_map: dict[int, float] = {}  # order_id → avg fill price
    executions_available = True
    try:
        fill_map = conn.get_executions()
    except Exception as e:
        executions_available = False
        logger.warning(f"[worker] fill sweep — get_executions() failed: {e}")
        # Continue — but without execution data we must NOT conclude "errored"

    updated = 0
    filled = 0
    errored = 0
    unconfirmed = 0
    update_ts = now.isoformat()
    unknown_cutoff = (now - timedelta(seconds=FILL_SWEEP_UNKNOWN_GRACE_SECS)).isoformat()

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
                # Gone from open orders and not in executions. That is NOT proof
                # of failure — IBKR only serves ~24h of executions, and a Gateway
                # restart can hide an older fill entirely. Marking ERROR here is
                # what corrupted 30 rows (14 of them for still-held tickers) and
                # suppressed record_fill(), leaving forward_signals.fill_price NULL.
                # Only conclude ERROR once the row is older than the grace window
                # AND we actually had execution data to search.
                if not executions_available:
                    unconfirmed += 1
                    logger.info(
                        f"[worker] fill sweep: order id={row['id']} ticker={row['ticker']} "
                        f"left SUBMITTED — no execution data this pass"
                    )
                    continue

                if row["created_at"] > unknown_cutoff:
                    unconfirmed += 1
                    logger.info(
                        f"[worker] fill sweep: order id={row['id']} ticker={row['ticker']} "
                        f"unconfirmed (ibkr_id={ibkr_id}) — within grace window, leaving SUBMITTED"
                    )
                    continue

                db.execute(
                    "UPDATE order_log SET status = 'ERROR', updated_at = ?, notes = ? "
                    "WHERE id = ? AND status = 'SUBMITTED'",
                    (update_ts, "Gone from open orders (fill sweep)", row["id"]),
                )
                errored += 1
                logger.info(
                    f"[worker] fill sweep: order id={row['id']} ticker={row['ticker']} "
                    f"→ ERROR (ibkr_id={ibkr_id} not in open or executions after grace)"
                )

            updated += 1

    logger.info(
        f"[worker] fill sweep complete: checked {len(rows)} stale rows, "
        f"{filled} filled, {errored} errored, {unconfirmed} unconfirmed, "
        f"{len(rows) - updated - unconfirmed} unchanged"
    )


def _check_ticker(conn: IBKRConnection, ticker: str) -> Optional[SupertrendEvent]:
    """Fetch bars, compute Supertrend, return a flip event or None."""
    try:
        df = conn.historical_bars(ticker, bar_size=BAR_SIZE, duration=HISTORICAL_DURATION)
    except Exception as e:
        logger.warning(f"[worker] {ticker}: historical_bars failed: {e}")
        return None

    if df.empty:
        return None

    # Drop the in-progress (unclosed) last bar. IBKR reqHistoricalData with
    # endDateTime="" returns the forming bar as the last row; computing Supertrend
    # on it would flip mid-bar and un-flip before close, firing premature/ghost
    # signals. Match price_alert_monitor.py which does hist.iloc[:-1] for the same
    # reason. After the drop, a flip on the last CLOSED bar has bars_ago == 1 and
    # fires exactly once (guarded below); the 24h dedup in signal_combiner is the
    # second line of defense against re-firing on subsequent polls.
    df = df.iloc[:-1]

    if len(df) < ATR_PERIOD + 2:
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

    if not _is_signal_hours(signal_type=signal):
        logger.debug(f"[worker] {ticker}: outside signal hours for {signal} (ET) — skipping")
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


def _update_trailing_stops(conn: IBKRConnection) -> None:
    """Raise ATR-based trailing stops for all open positions.

    For each position that has an active STP SELL order in IBKR, computes
    price - TRAILING_ATR_MULTIPLIER * ATR_1h. If the result is above the current
    stop price, modifies the order upward. Never lowers a stop.
    Runs at most every TRAILING_STOP_INTERVAL_SECS.
    """
    global _last_trailing_stop_ts
    now = datetime.now()
    if _last_trailing_stop_ts is not None:
        elapsed = (now - _last_trailing_stop_ts).total_seconds()
        if elapsed < TRAILING_STOP_INTERVAL_SECS:
            return
    _last_trailing_stop_ts = now

    try:
        open_orders = conn.get_open_orders()
    except Exception as e:
        logger.warning(f"[worker] trailing stop: get_open_orders failed: {e}")
        return

    # Build map: ticker -> current stop price (STP SELL orders only)
    stop_map: dict[str, float] = {}
    for o in open_orders:
        if o["order_type"] == "STP" and o["action"] == "SELL":
            aux = o.get("aux_price")
            if aux is not None:
                stop_map[o["ticker"]] = float(aux)

    if not stop_map:
        logger.debug("[worker] trailing stop: no active STP SELL orders found")
        return

    updated = 0
    for ticker, current_stop in stop_map.items():
        try:
            df = conn.historical_bars(ticker, bar_size=BAR_SIZE, duration="5 D")
            if df.empty or len(df) < ATR_PERIOD + 2:
                continue
            df = df.iloc[:-1]  # drop in-progress bar
            current_price = float(df["Close"].iloc[-1])
            atr_val = _atr(df)
            new_stop = round(current_price - TRAILING_ATR_MULTIPLIER * atr_val, 2)

            if new_stop <= current_stop:
                logger.debug(
                    f"[worker] {ticker}: trailing stop unchanged "
                    f"(new={new_stop:.2f} <= current={current_stop:.2f})"
                )
                continue

            if conn.modify_stop_order(ticker, new_stop):
                logger.info(
                    f"[worker] {ticker}: trailing stop raised "
                    f"${current_stop:.2f} -> ${new_stop:.2f} "
                    f"(price=${current_price:.2f}, ATR={atr_val:.2f})"
                )
                updated += 1
        except Exception as e:
            logger.warning(f"[worker] trailing stop update failed for {ticker}: {e}")

    if updated:
        logger.info(f"[worker] trailing stops updated: {updated} positions raised")


def _has_pending_sell(ticker: str) -> bool:
    """True if any SELL order for this ticker is still SUBMITTED.

    LONG-ONLY INVARIANT. Every exit path (signal SELL, time stop, tier exit,
    score deterioration) writes an order_log row before calling IBKR, so this one
    check covers them all. Without it, two exit paths firing in the same window
    each size against the full ibkr_positions share count — which is how paper
    produced KSS 309 shares sold against 287 bought. On live margin that is a short.
    """
    try:
        with get_connection() as db:
            row = db.execute(
                "SELECT 1 FROM order_log WHERE ticker = ? AND action = 'SELL' "
                "AND status = 'SUBMITTED' LIMIT 1",
                (ticker,),
            ).fetchone()
        return row is not None
    except Exception as e:
        logger.warning(f"[worker] _has_pending_sell({ticker}) failed: {e}")
        return False


def _trading_days_since(entry_date_str: str) -> int:
    """Count trading days (Mon-Fri) between entry_date and today."""
    try:
        entry = datetime.fromisoformat(entry_date_str).date()
    except Exception:
        return 0
    today = datetime.now().date()
    if entry >= today:
        return 0
    count = 0
    d = entry
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            count += 1
    return count


def _check_time_stops(conn: IBKRConnection) -> None:
    """Exit zombie positions held > TIME_STOP_DAYS trading days with no direction.

    A zombie is a position where abs(unrealized_pnl_pct) < TIME_STOP_BAND_PCT
    after TIME_STOP_DAYS trading days. Capital is recycled by submitting a
    market-priced limit SELL. Runs at most every TIME_STOP_INTERVAL_SECS.
    """
    global _last_time_stop_ts
    now = datetime.now()
    if _last_time_stop_ts is not None:
        elapsed = (now - _last_time_stop_ts).total_seconds()
        if elapsed < TIME_STOP_INTERVAL_SECS:
            return
    _last_time_stop_ts = now

    with get_connection() as db:
        positions = db.execute(
            "SELECT ticker, shares, avg_cost, unrealized_pnl, market_value "
            "FROM ibkr_positions WHERE shares > 0"
        ).fetchall()

    if not positions:
        return

    for pos in positions:
        ticker = pos["ticker"]
        try:
            # Entry date = the EARLIEST filled BUY that opened this position.
            # Using the most recent BUY (the old behaviour) restarted the clock on
            # every add, so any pyramided position could never age into a time stop:
            # CALM first bought 2026-06-26 but last added 2026-07-22 read as 9
            # trading days against a 15-day threshold and was skipped forever.
            with get_connection() as db:
                row = db.execute(
                    "SELECT created_at FROM order_log "
                    "WHERE ticker = ? AND action = 'BUY' AND status = 'FILLED' "
                    "ORDER BY created_at ASC LIMIT 1",
                    (ticker,),
                ).fetchone()
                if not row:
                    # No FILLED BUY row. This is usually corrupted history rather
                    # than a phantom position (the fill sweep used to mark real
                    # fills ERROR — RRR/TRS/LCII were all invisible here). Fall
                    # back to the earliest BUY attempt of any status so a genuinely
                    # held position still ages into its time stop.
                    row = db.execute(
                        "SELECT created_at FROM order_log "
                        "WHERE ticker = ? AND action = 'BUY' "
                        "ORDER BY created_at ASC LIMIT 1",
                        (ticker,),
                    ).fetchone()
                    if row:
                        logger.info(
                            f"[worker] time stop: {ticker} has no FILLED BUY row — "
                            f"using earliest BUY attempt {row['created_at'][:10]} as entry date"
                        )
            if not row:
                continue

            days_held = _trading_days_since(row["created_at"])
            if days_held < TIME_STOP_DAYS:
                continue

            cost_basis = (pos["avg_cost"] or 0) * (pos["shares"] or 0)
            if cost_basis <= 0:
                continue
            pnl_pct = (pos["unrealized_pnl"] or 0) / cost_basis * 100

            if abs(pnl_pct) >= TIME_STOP_BAND_PCT:
                continue  # has direction — not a zombie

            # Zombie detected — exit at market via LMT slightly below current price
            price = (pos["market_value"] or 0) / pos["shares"]
            if price <= 0:
                continue

            # Dedup — skip if a SELL is already pending to avoid submitting a
            # second full-position SELL every 4 hours until the first fills.
            with get_connection() as db:
                has_pending = db.execute(
                    "SELECT 1 FROM order_log WHERE ticker = ? AND action = 'SELL' "
                    "AND status = 'SUBMITTED'",
                    (ticker,),
                ).fetchone()
            if has_pending:
                logger.debug(f"[worker] time stop: {ticker} already has pending SELL — skipping")
                continue

            exit_price = round(price * 0.999, 2)  # 0.1% below last for quick fill
            shares_to_exit = int(pos["shares"])

            # Routed through order_manager.submit_exit — pause check, long-only
            # clamp and order-log-before-place all live there now.
            result = submit_exit(
                conn, ticker, shares_to_exit, exit_price,
                f"TIME STOP: {days_held}d zombie",
            )
            if result["status"] != "SUBMITTED":
                logger.info(f"[worker] time stop {ticker}: {result['status']} — {result.get('reason')}")
                continue

            logger.info(
                f"[worker] TIME STOP: {ticker} held {days_held}d "
                f"pnl={pnl_pct:+.1f}% — SELL {result['shares']}sh @ ${exit_price:.2f}"
            )
            _send_telegram(
                f"⏱ TIME STOP — {ticker}\n"
                f"Held {days_held} trading days, P&L={pnl_pct:+.1f}% (zombie)\n"
                f"Exiting {result['shares']} shares @ ${exit_price:.2f}"
            )
        except Exception as e:
            logger.warning(f"[worker] time stop check failed for {ticker}: {e}")


def _check_tiered_exits(conn: IBKRConnection) -> None:
    """Partial profit-taking: T1 (+7%) sell 40%, T2 (+14%) sell 30%.

    Uses exit_tier column in ibkr_positions to track state:
      0 = no partial exit taken yet
      1 = T1 taken (40% sold), stop moved to breakeven
      2 = T2 taken (30% sold), remainder trails Supertrend
    """
    with get_connection() as db:
        positions = db.execute(
            "SELECT ticker, shares, avg_cost, unrealized_pnl, market_value, "
            "COALESCE(exit_tier, 0) AS exit_tier "
            "FROM ibkr_positions WHERE shares > 0"
        ).fetchall()

    if not positions:
        return

    now = datetime.now()
    for pos in positions:
        ticker = pos["ticker"]
        tier = int(pos["exit_tier"] or 0)

        try:
            cost_basis = (pos["avg_cost"] or 0) * (pos["shares"] or 0)
            if cost_basis <= 0:
                continue
            pnl_pct = (pos["unrealized_pnl"] or 0) / cost_basis * 100
            price = (pos["market_value"] or 0) / pos["shares"] if pos["shares"] else 0
            if price <= 0:
                continue

            # Dedup: skip if ANY SELL is still working for this ticker — not just a
            # partial one. ibkr_positions still shows the pre-exit share count while
            # an order is in flight, so sizing T1/T2 against it on top of a pending
            # time-stop or signal SELL oversells the position.
            if _has_pending_sell(ticker):
                logger.debug(f"[worker] tiered exit: {ticker} has a pending SELL — skipping")
                continue

            if tier == 0 and pnl_pct >= TIER1_PCT:
                # T1: sell 40%, move stop to avg_cost (breakeven)
                shares_to_sell = max(1, int(pos["shares"] * 0.40))
                exit_price = round(price * 0.999, 2)
                breakeven = round(float(pos["avg_cost"]), 2)

                # exit_tier is advanced BEFORE the broker call so a re-entry on
                # the next cycle cannot re-fire T1. On 2026-08-03 this ordering
                # was reversed and T1 re-submitted 224 times on BMY alone.
                with get_connection() as db:
                    db.execute(
                        "UPDATE ibkr_positions SET exit_tier = 1 WHERE ticker = ?",
                        (ticker,),
                    )

                result = submit_exit(
                    conn, ticker, shares_to_sell, exit_price,
                    f"T1 partial exit ({pnl_pct:+.1f}%)",
                )
                if result["status"] != "SUBMITTED":
                    logger.info(f"[worker] T1 {ticker}: {result['status']} — {result.get('reason')}")
                    continue
                conn.modify_stop_order(ticker, breakeven)

                logger.info(
                    f"[worker] TIER-1 exit: {ticker} selling {result['shares']}sh "
                    f"@ ${exit_price:.2f} pnl={pnl_pct:+.1f}% stop→breakeven ${breakeven:.2f}"
                )
                _send_telegram(
                    f"🏦 T1 PARTIAL EXIT — {ticker}\n"
                    f"Sold {result['shares']} shares (40%) @ ${exit_price:.2f}\n"
                    f"P&L={pnl_pct:+.1f}% | Stop moved to breakeven ${breakeven:.2f}"
                )

            elif tier == 1 and pnl_pct >= TIER2_PCT:
                # T2: sell 50% of remaining (≈ 30% of original position)
                shares_to_sell = max(1, int(pos["shares"] * 0.50))
                exit_price = round(price * 0.999, 2)

                with get_connection() as db:
                    db.execute(
                        "UPDATE ibkr_positions SET exit_tier = 2 WHERE ticker = ?",
                        (ticker,),
                    )

                result = submit_exit(
                    conn, ticker, shares_to_sell, exit_price,
                    f"T2 partial exit ({pnl_pct:+.1f}%)",
                )
                if result["status"] != "SUBMITTED":
                    logger.info(f"[worker] T2 {ticker}: {result['status']} — {result.get('reason')}")
                    continue

                logger.info(
                    f"[worker] TIER-2 exit: {ticker} selling {result['shares']}sh "
                    f"@ ${exit_price:.2f} pnl={pnl_pct:+.1f}%"
                )
                _send_telegram(
                    f"🏦 T2 PARTIAL EXIT — {ticker}\n"
                    f"Sold {result['shares']} shares (50% of remaining) @ ${exit_price:.2f}\n"
                    f"P&L={pnl_pct:+.1f}% | Remainder trails Supertrend"
                )

        except Exception as e:
            logger.warning(f"[worker] tiered exit check failed for {ticker}: {e}")


def _check_score_deterioration(conn: IBKRConnection) -> None:
    """Exit positions where fundamental score has deteriorated sharply.

    Conditions (all must be true):
      - Score dropped >= 15 pts from entry signal score
      - Current score < 55 (NEUTRAL/SKIP — stock fundamentally weakened)
      - Unrealized P&L > -3% (exit before big loss, not after)
    """
    with get_connection() as db:
        positions = db.execute(
            "SELECT ticker, shares, avg_cost, unrealized_pnl, market_value "
            "FROM ibkr_positions WHERE shares > 0"
        ).fetchall()

    if not positions:
        return

    now = datetime.now()
    for pos in positions:
        ticker = pos["ticker"]
        try:
            # Entry score from forward_signals at time of BUY
            with get_connection() as db:
                entry_row = db.execute(
                    "SELECT composite_score FROM forward_signals "
                    "WHERE ticker = ? AND signal_type = 'BUY' "
                    "ORDER BY signal_ts DESC LIMIT 1",
                    (ticker,),
                ).fetchone()
                if not entry_row or entry_row["composite_score"] is None:
                    continue
                entry_score = float(entry_row["composite_score"])

                # Current score from latest scheduled scan
                curr_row = db.execute(
                    "SELECT sr.explosion_score FROM scan_results sr "
                    "INNER JOIN scan_runs run ON sr.run_id = run.id "
                    "WHERE sr.ticker = ? AND run.scan_type = 'scheduled' "
                    "ORDER BY sr.scanned_at DESC LIMIT 1",
                    (ticker,),
                ).fetchone()
                if not curr_row or curr_row["explosion_score"] is None:
                    continue
                current_score = float(curr_row["explosion_score"])

            delta = entry_score - current_score
            if delta < 15 or current_score >= 55:
                continue  # not deteriorated enough, or still fundamentally strong

            # Long-only invariant: this path submits a FULL-position SELL and had
            # no in-flight check at all, so it could stack on a time-stop or signal
            # SELL and oversell the position.
            if _has_pending_sell(ticker):
                logger.debug(
                    f"[worker] score deterioration: {ticker} has a pending SELL — skipping"
                )
                continue

            cost_basis = (pos["avg_cost"] or 0) * (pos["shares"] or 0)
            if cost_basis <= 0:
                continue
            pnl_pct = (pos["unrealized_pnl"] or 0) / cost_basis * 100
            if pnl_pct < -3.0:
                continue  # already in significant loss — let stop handle it

            price = (pos["market_value"] or 0) / pos["shares"] if pos["shares"] else 0
            if price <= 0:
                continue

            # Check dedup — don't fire score_deterioration exit more than once per 24h
            with get_connection() as db:
                cutoff = (now - timedelta(hours=24)).isoformat()
                dup = db.execute(
                    "SELECT 1 FROM watchlist_alerts "
                    "WHERE ticker = ? AND alert_type = 'score_deterioration_exit' "
                    "AND sent_at >= ? LIMIT 1",
                    (ticker, cutoff),
                ).fetchone()
                if dup:
                    continue

                db.execute(
                    "INSERT INTO watchlist_alerts "
                    "(ticker, alert_type, message, sent_at, score, price) "
                    "VALUES (?, 'score_deterioration_exit', ?, ?, ?, ?)",
                    (ticker, f"Score drop exit: {entry_score:.0f}->{current_score:.0f}",
                     now.isoformat(), current_score, price),
                )

            exit_price = round(price * 0.999, 2)
            shares_to_exit = int(pos["shares"])

            result = submit_exit(
                conn, ticker, shares_to_exit, exit_price,
                f"Score deterioration exit: {entry_score:.0f}->{current_score:.0f}",
            )
            if result["status"] != "SUBMITTED":
                logger.info(
                    f"[worker] score exit {ticker}: {result['status']} — {result.get('reason')}"
                )
                continue

            logger.info(
                f"[worker] SCORE DETERIORATION EXIT: {ticker} "
                f"score {entry_score:.0f}->{current_score:.0f} (delta={delta:.0f}) "
                f"pnl={pnl_pct:+.1f}%"
            )
            _send_telegram(
                f"📉 SCORE EXIT — {ticker}\n"
                f"Score: {entry_score:.0f} -> {current_score:.0f} (drop={delta:.0f}pts)\n"
                f"P&L={pnl_pct:+.1f}% | Exiting before further deterioration\n"
                f"Selling {result['shares']} shares @ ${exit_price:.2f}"
            )
        except Exception as e:
            logger.warning(f"[worker] score deterioration check failed for {ticker}: {e}")


def run_once(conn: IBKRConnection) -> int:
    """One pass over the monitoring queue. Returns number of alerts fired."""
    queue = build_queue()
    if not queue:
        # An empty queue means there is nothing new to BUY. It must NOT skip the
        # exit layer below — trailing stops, time stops, tiered exits and the fill
        # sweep all protect money that is ALREADY at risk. The old early return
        # made position management conditional on there being fresh entry
        # candidates, so a quiet scan silently disarmed every stop.
        logger.info("[worker] queue empty — no entry candidates; running exit layer only")

    tracker = PositionTracker(conn)

    # Sync positions BEFORE processing signals so Layer -1 veto checks use fresh data.
    # Without this, get_current_exposure() reads from the previous cycle (up to 5 min stale),
    # causing SELL orders to bypass the "no open position" veto when a position was closed
    # between cycles (e.g. stop hit, order cancelled by IBKR).
    positions_fresh = True
    try:
        tracker.sync_positions()
    except Exception as e:
        positions_fresh = False
        logger.error(f"[worker] position pre-sync FAILED: {e} — entries blocked this cycle")

    fired = 0
    buy_signals_this_cycle = 0
    # Every position-aware guard — the already-long veto, the long-only share
    # arithmetic, the short alarm — reads ibkr_positions. If the sync failed, that
    # table is stale or empty and those guards silently evaluate against fiction.
    # On 2026-08-05 the Gateway held a stale session and answered every request
    # with a timeout for 25 minutes; trading must not proceed blind through that.
    # Exits below still run: they protect capital already at risk.
    if not positions_fresh:
        logger.error("[worker] skipping entry signals — position data is not fresh")
        queue = []
    for entry in queue:
        event = _check_ticker(conn, entry.ticker)
        if event is None:
            continue
        alert = evaluate(event)
        if alert is None:
            continue

        # Per-cycle BUY cap — prevent a single cycle from consuming the entire daily
        # budget when many tickers flip simultaneously (e.g. broad market move).
        # SELL signals are never capped — exits are safety-critical.
        if alert.alert_type == "combined_buy" and buy_signals_this_cycle >= PER_CYCLE_BUY_CAP:
            logger.info(
                f"[worker] per-cycle BUY cap ({PER_CYCLE_BUY_CAP}) reached — "
                f"skipping {alert.ticker}, releasing dedup for retry next cycle"
            )
            from src.signal_combiner import release_dedup
            release_dedup(alert.ticker, alert.alert_type)
            continue

        logger.info(f"[worker] ALERT {alert.alert_type} {alert.ticker} @ ${alert.entry_price:.2f}")

        # evaluate() has already claimed the 24h dedup slot. If the Telegram send
        # genuinely FAILS (returns False — e.g. HTTP 400 on a malformed message, or
        # a transient network error), roll back the dedup claim so the signal can
        # retry next cycle instead of being silently suppressed for 24h. A disabled
        # channel (None) is NOT a failure — the signal is still recorded and we do
        # not roll back.
        send_result = _send_telegram(alert.message)
        if send_result is False:
            from src.signal_combiner import release_dedup
            release_dedup(alert.ticker, alert.alert_type)
            logger.warning(
                f"[worker] {alert.ticker}: Telegram send failed — dedup claim rolled back, "
                f"will retry next cycle"
            )
            continue

        # Order submission (Phase 1)
        try:
            result = _submit_order(conn, alert, tracker)
            if result["status"] == "VETOED":
                _send_telegram(
                    f"⛔ ORDER VETOED — {alert.ticker}\n"
                    f"Reason: {result.get('reason', 'unknown')}"
                )
            elif result["status"] == "SUBMITTED":
                ibkr_id = result.get("order_id", "?")
                shares = result.get("shares", "?")
                _send_telegram(f"✅ {alert.ticker} {shares}sh @ ${alert.entry_price:.2f} | #{ibkr_id}")
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
        if alert.alert_type == "combined_buy":
            buy_signals_this_cycle += 1

    try:
        tracker.record_daily_pnl()
    except Exception as e:
        logger.warning(f"[worker] daily pnl record failed: {e}")

    # Periodic fill sweep — catch fills missed by orderStatusEvent callbacks
    try:
        _periodic_fill_sweep(conn)
    except Exception as e:
        logger.warning(f"[worker] fill sweep failed: {e}")

    # Exit strategy layer — runs after signals, never blocks the main loop
    try:
        _update_trailing_stops(conn)
    except Exception as e:
        logger.warning(f"[worker] trailing stop update failed: {e}")

    try:
        _check_tiered_exits(conn)
    except Exception as e:
        logger.warning(f"[worker] tiered exit check failed: {e}")

    try:
        _check_time_stops(conn)
    except Exception as e:
        logger.warning(f"[worker] time stop check failed: {e}")

    try:
        _check_score_deterioration(conn)
    except Exception as e:
        logger.warning(f"[worker] score deterioration check failed: {e}")

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


_MUTEX_NAME = "Global\\FinancialAgent_IBKRWorker_Singleton"
_mutex_handle = None   # kept alive to maintain mutex ownership


def _acquire_singleton_lock() -> bool:
    """Create a named Windows mutex. Returns False if another instance already owns it.

    Named mutexes are released by the OS when the owning process exits (even on crash),
    so no cleanup is needed after a hard kill. This is truly atomic — no race window.
    """
    global _mutex_handle
    import ctypes, os

    ERROR_ALREADY_EXISTS = 183
    # CREATE_MUTEX_INITIAL_OWNER = bInitialOwner=True claims ownership on creation
    handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if not handle:
        logger.warning("[worker] CreateMutex failed — OS error, proceeding without singleton guard")
        return True
    last_err = ctypes.windll.kernel32.GetLastError()
    if last_err == ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        logger.warning(
            "[worker] another ibkr_worker is already running (named mutex exists). Exiting."
        )
        return False
    # We own the mutex — keep the handle open for the lifetime of this process
    _mutex_handle = handle
    # Also write PID to a file for human inspection (best-effort, not relied upon for locking)
    try:
        lock_path = __import__("pathlib").Path(__file__).parent.parent / "ibkr_worker_running.lock"
        lock_path.write_text(str(os.getpid()))
    except Exception:
        pass
    return True


def _release_singleton_lock() -> None:
    global _mutex_handle
    if _mutex_handle:
        import ctypes
        ctypes.windll.kernel32.ReleaseMutex(_mutex_handle)
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
    try:
        lock_path = __import__("pathlib").Path(__file__).parent.parent / "ibkr_worker_running.lock"
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def _quiet_third_party_logs() -> None:
    """Stop third-party chatter from burying our own log lines.

    Measured on the 1.16 GB log accumulated 2026-05-19 .. 2026-08-05
    (3.32M lines):

        ib_async.wrapper   2,134,900 lines   1,046 MB   90.0%
        src.supertrend       884,740 lines     116 MB   10.0%
        ALL our own code     ~300,000 lines    ~14 MB    1.2%

    ib_async logs every portfolio item at INFO on every account update, and
    src.supertrend logs one loguru DEBUG line per ticker per 5-minute cycle. The
    result was a file too large to grep during an incident — which is the only
    time anyone reads it. Everything at WARNING and above is still kept, so real
    problems (order rejections, disconnects, API warnings) survive.
    """
    for name in ("ib_async", "ib_async.wrapper", "ib_async.client",
                 "ib_async.ib", "ib_insync"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # src.supertrend uses loguru, which has its own sink and ignores the stdlib
    # level above. Raising the threshold to INFO drops the per-ticker DEBUG line
    # while keeping its warnings.
    try:
        from loguru import logger as _loguru
        _loguru.remove()
        _loguru.add(sys.stderr, level="INFO")
    except Exception as e:  # loguru absent or already configured
        logging.getLogger(__name__).debug(f"loguru not reconfigured: {e}")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _quiet_third_party_logs()
    if not _acquire_singleton_lock():
        return 1
    try:
        return _main_body()
    finally:
        _release_singleton_lock()


def _main_body() -> int:
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
    import multiprocessing
    multiprocessing.freeze_support()   # prevents spawn-mode double-execution on Windows
    sys.exit(main())
