"""
IBKR Real-Time Connector — bridges IB Gateway to the FinancialAgent project.

Provides historical and live bar data from Interactive Brokers via the
ib_insync library. Returns pandas DataFrames in the same shape as yfinance
(columns: Open, High, Low, Close, Volume) so existing modules like
src/supertrend.py work unchanged.

Connection target: Docker IB Gateway running on localhost.
  - Paper trading: port 4002
  - Live trading:  port 4001
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator, Literal

import pandas as pd

try:
    from ib_async import IB, Stock, LimitOrder, StopOrder, util
except ImportError:
    # ib_async is only available in .venv313 (Python 3.13).
    # Stub out just enough for the module to import in Python 3.14 (tests, dashboard).
    IB = None  # type: ignore[assignment,misc]
    Stock = LimitOrder = StopOrder = util = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
PAPER_PORT = 4002
LIVE_PORT = 4001

# Upper bound on how long modify_stop_order() waits for the broker to echo a
# modification. Normal acks arrive in ~0.2s; even one wait per held position
# stays far below ibkr_worker's 720s stall guard.
MODIFY_CONFIRM_TIMEOUT_SECS = 5.0
# Order-related codes that are informational, not a rejection of the modify
# (399 = "order will not be placed at the exchange until ..." for out-of-RTH
# stops; 2100-2199 are connection/farm notices).
_BENIGN_ORDER_CODES = frozenset({399}) | frozenset(range(2100, 2200))

BarSize = Literal["1 min", "5 mins", "15 mins", "30 mins", "1 hour", "1 day"]
WhatToShow = Literal["TRADES", "MIDPOINT", "BID", "ASK"]


class IBKRConnection:
    """Thin wrapper around ib_insync.IB with project-specific helpers."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = PAPER_PORT, client_id: int = 1):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()

    def connect(self, timeout: float = 10.0) -> None:
        self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=timeout)
        logger.info(f"[ibkr] connected to {self.host}:{self.port} clientId={self.client_id}")

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("[ibkr] disconnected")

    def is_connected(self) -> bool:
        return self.ib.isConnected()

    def historical_bars(
        self,
        ticker: str,
        bar_size: BarSize = "1 hour",
        duration: str = "10 D",
        what_to_show: WhatToShow = "TRADES",
        use_rth: bool = True,
    ) -> pd.DataFrame:
        """
        Fetch historical bars for a US stock.

        Returns a DataFrame with capitalized OHLCV columns and DatetimeIndex,
        matching the shape used elsewhere in the project.
        """
        contract = Stock(ticker, "SMART", "USD")
        self.ib.qualifyContracts(contract)

        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
        )

        if not bars:
            logger.warning(f"[ibkr] no historical bars returned for {ticker}")
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        df = util.df(bars)
        df = df.rename(columns={
            "date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        })
        df = df.set_index("Date")
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def live_price(self, ticker: str, timeout: float = 5.0) -> float | None:
        """Snapshot last traded price for a US stock."""
        contract = Stock(ticker, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        ticker_data = self.ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
        self.ib.sleep(timeout)
        price = ticker_data.last if ticker_data.last == ticker_data.last else ticker_data.close
        self.ib.cancelMktData(contract)
        return float(price) if price and price == price else None

    # ── Order placement (Phase 1) ────────────────────────────────────────

    def place_bracket_order(
        self,
        ticker: str,
        action: str,
        shares: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> dict[str, int]:
        """Place a bracket order (LMT entry + STP stop + LMT target).

        Returns {"order_id": parent_id, "stop_order_id": stop_leg_id}. The STP
        leg's id lets a caller record it (order_log.stop_order_id) so
        modify_stop_order() can later target that exact order instead of
        scanning openTrades() by ticker/type/action — ambiguous if more than
        one resting STP SELL order ends up open for the same ticker.
        """
        contract = Stock(ticker, "SMART", "USD")
        self.ib.qualifyContracts(contract)

        parent = LimitOrder(action, shares, entry_price)
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False
        parent.tif = "GTC"  # GTC so entry/stop/target persist across sessions

        stop_action = "SELL" if action == "BUY" else "BUY"

        stop_order = StopOrder(stop_action, shares, stop_price)
        stop_order.orderId = self.ib.client.getReqId()
        stop_order.parentId = parent.orderId
        stop_order.transmit = False
        stop_order.tif = "GTC"

        target_order = LimitOrder(stop_action, shares, target_price)
        target_order.orderId = self.ib.client.getReqId()
        target_order.parentId = parent.orderId
        target_order.transmit = True  # transmit the whole bracket
        target_order.tif = "GTC"

        placed = []
        try:
            for order in [parent, stop_order, target_order]:
                self.ib.placeOrder(contract, order)
                placed.append(order)
        except Exception as exc:
            # Cancel whatever was submitted before the crash so IB doesn't
            # get a dangling parent with no protective child orders.
            for submitted in placed:
                try:
                    self.ib.cancelOrder(submitted)
                except Exception:
                    pass
            raise RuntimeError(
                f"[ibkr] bracket order failed mid-submission for {ticker} "
                f"(placed {len(placed)}/3 legs) — rolled back: {exc}"
            ) from exc

        logger.info(
            f"[ibkr] bracket order placed: {action} {shares} {ticker} "
            f"entry=${entry_price:.2f} stop=${stop_price:.2f} target=${target_price:.2f} "
            f"parent_id={parent.orderId} stop_id={stop_order.orderId}"
        )
        return {"order_id": parent.orderId, "stop_order_id": stop_order.orderId}

    def place_limit_order(
        self,
        ticker: str,
        action: str,
        shares: int,
        limit_price: float,
    ) -> int:
        """Place a single plain LMT order (no bracket children).

        Used for SELL exits: a SELL closes an existing long, so there is no
        stop/target bracket to attach — that geometry only makes sense for a
        BUY entry. Returns the order_id.
        """
        contract = Stock(ticker, "SMART", "USD")
        self.ib.qualifyContracts(contract)

        order = LimitOrder(action, shares, limit_price)
        order.orderId = self.ib.client.getReqId()
        order.transmit = True
        order.tif = "DAY"  # explicit TIF avoids IB Error 10349 noise

        self.ib.placeOrder(contract, order)
        logger.info(
            f"[ibkr] limit order placed: {action} {shares} {ticker} "
            f"limit=${limit_price:.2f} order_id={order.orderId}"
        )
        return order.orderId

    def cancel_order(self, order_id: int) -> bool:
        """Cancel an order by order_id."""
        for trade in self.ib.openTrades():
            if trade.order.orderId == order_id:
                self.ib.cancelOrder(trade.order)
                logger.info(f"[ibkr] cancel requested for order_id={order_id}")
                return True
        logger.warning(f"[ibkr] order_id={order_id} not found in open trades")
        return False

    def get_open_orders(self) -> list[dict]:
        """Return all open orders as a list of dicts.

        aux_price is set for STP orders (the stop trigger price).
        lmt_price is set for LMT orders.
        """
        result = []
        for trade in self.ib.openTrades():
            o = trade.order
            result.append({
                "order_id": o.orderId,
                "ticker": trade.contract.symbol,
                "action": o.action,
                "qty": int(o.totalQuantity),
                "order_type": o.orderType,
                "status": trade.orderStatus.status,
                "lmt_price": getattr(o, "lmtPrice", None),
                "aux_price": getattr(o, "auxPrice", None),  # stop trigger price for STP orders
            })
        return result

    def get_executions(self, lookback_days: int = 2, timeout: float = 15.0) -> dict[int, float]:
        """Return {ibkr_order_id: avg_fill_price} for recent executions.

        ``ib.fills()`` only holds executions received during the CURRENT API
        session — after a Gateway restart or a worker reconnect it is empty for
        orders placed earlier. That made the periodic fill sweep classify real
        fills as ERROR (30 such rows as of 2026-08-05, 14 of them for tickers
        that are still held). ``reqExecutions()`` asks the broker instead, so it
        survives reconnects.

        IBKR only serves executions for roughly the last 24h via this API, so
        lookback_days is a best-effort hint, not a guarantee. Merges the
        in-session ``fills()`` on top since those are always authoritative.
        """
        import asyncio

        from ib_async import ExecutionFilter, util

        result: dict[int, float] = {}
        try:
            cutoff = datetime.now() - timedelta(days=lookback_days)
            efilter = ExecutionFilter(time=cutoff.strftime("%Y%m%d-%H:%M:%S"))
            # MUST have a timeout. IB.reqExecutions() has none, and after a paper
            # account reset the Gateway accepts the connection but answers data
            # requests with "Positions info is not available yet" (warning 2151)
            # and never replies. On 2026-08-05 that hung the worker for 25 minutes
            # inside startup reconciliation — connected, silent, and doing nothing.
            # A missed execution sweep is recoverable; a hung worker is not.
            fills = util.run(
                asyncio.wait_for(self.ib.reqExecutionsAsync(efilter), timeout=timeout)
            )
            for fill in fills:
                try:
                    result[fill.execution.orderId] = float(fill.execution.avgPrice)
                except Exception:
                    continue
        except asyncio.TimeoutError:
            logger.warning(
                f"[ibkr] reqExecutions timed out after {timeout}s — account data "
                f"may not be ready (post-reset/reconnect). Using session fills only."
            )
        except Exception as e:
            logger.warning(f"[ibkr] reqExecutions failed: {e} — falling back to session fills")

        # In-session fills win over the broker snapshot (fresher price).
        try:
            for fill in self.ib.fills():
                try:
                    result[fill.execution.orderId] = float(fill.execution.avgPrice)
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"[ibkr] ib.fills() failed: {e}")

        return result

    def get_execution_details(self, lookback_days: int = 2, timeout: float = 15.0) -> dict[int, dict]:
        """{order_id: {ticker, side, shares, avg_price, time}} aggregated per order.

        Same sources and timeout discipline as get_executions(), with the detail
        needed to log a fill that has no order_log row — bracket stop/target legs
        are child orders with no row of their own. side is 'BOT' or 'SLD'.
        """
        import asyncio

        from ib_async import ExecutionFilter, util

        fills: list = []
        try:
            cutoff = datetime.now() - timedelta(days=lookback_days)
            efilter = ExecutionFilter(time=cutoff.strftime("%Y%m%d-%H:%M:%S"))
            fills = list(util.run(
                asyncio.wait_for(self.ib.reqExecutionsAsync(efilter), timeout=timeout)
            ))
        except asyncio.TimeoutError:
            logger.warning(f"[ibkr] reqExecutions (details) timed out after {timeout}s")
        except Exception as e:
            logger.warning(f"[ibkr] reqExecutions (details) failed: {e}")
        try:
            fills += list(self.ib.fills())
        except Exception as e:
            logger.warning(f"[ibkr] ib.fills() failed: {e}")

        seen: set = set()
        result: dict[int, dict] = {}
        for fill in fills:
            try:
                ex = fill.execution
                if ex.execId in seen:  # broker snapshot and session fills overlap
                    continue
                seen.add(ex.execId)
                d = result.setdefault(ex.orderId, {
                    "ticker": fill.contract.symbol, "side": ex.side,
                    "shares": 0.0, "notional": 0.0, "time": ex.time,
                })
                d["shares"] += float(ex.shares)
                d["notional"] += float(ex.shares) * float(ex.price)
                if ex.time and (d["time"] is None or ex.time < d["time"]):
                    d["time"] = ex.time
            except Exception:
                continue
        for d in result.values():
            d["avg_price"] = d.pop("notional") / d["shares"] if d["shares"] else None
        return result

    def modify_stop_order(
        self,
        ticker: str,
        new_stop_price: float,
        stop_order_id: int | None = None,
        confirm_timeout: float = MODIFY_CONFIRM_TIMEOUT_SECS,
    ) -> bool:
        """Raise the trailing stop for an open STP order on ticker.

        When stop_order_id is given (the STP leg id recorded on order_log at
        bracket-submission time), matches that exact IBKR order — precise even
        if more than one resting STP SELL order exists for ticker — and does
        NOT fall back to the ticker scan if it isn't found live, so a stale id
        can only no-op, never modify the wrong order. When omitted (a caller
        not yet updated, or a pre-migration order_log row with no recorded
        id), falls back to the original ticker/type/action scan unchanged —
        first match wins, same as before this parameter existed.

        Uses placeOrder() again — IBKR treats a re-submitted order with the
        same orderId as a modification — and then WAITS (bounded by
        confirm_timeout) for the broker to acknowledge it. Returns True only
        when the broker echoed the order back (openOrder) with the new
        trigger price; False if no order was found, the broker rejected the
        modify, or no confirmation arrived in time.

        Why the wait exists: this used to return True the instant placeOrder()
        queued the message. The broker's answer arrives ~200ms later, and the
        worker disconnects at the end of the cycle — sometimes within 10ms of
        the last modify — so a rejection was either logged long after the
        worker had already reported "trailing stop raised", or never seen at
        all. On 2026-09-09..11 MUR and CRC were rejected 183 times in a row
        ("Error 201 ... Cannot modify suspended order") while the worker kept
        logging a successful raise every pass. Waiting here also guarantees
        the cycle cannot disconnect before the broker has answered.
        """
        new_stop_price = round(new_stop_price, 2)
        target = None
        candidates = []
        for trade in self.ib.openTrades():
            o = trade.order
            if stop_order_id is not None:
                if o.orderId == stop_order_id:
                    target = trade
                    break
            elif (
                trade.contract.symbol == ticker
                and o.orderType == "STP"
                and o.action == "SELL"
            ):
                candidates.append(trade)
        if stop_order_id is None and candidates:
            target = candidates[0]
            if len(candidates) > 1:
                logger.warning(
                    f"[ibkr] {ticker}: {len(candidates)} resting STP SELL orders "
                    f"({[t.order.orderId for t in candidates]}) and no stop_order_id "
                    f"— modifying the first (order_id={target.order.orderId})"
                )
        if target is None:
            logger.debug(
                f"[ibkr] no active STP SELL order found for {ticker}"
                + (f" matching order_id={stop_order_id}" if stop_order_id is not None else "")
            )
            return False
        if target.isDone():
            logger.warning(
                f"[ibkr] {ticker}: order_id={target.order.orderId} is "
                f"{target.orderStatus.status} — cannot modify"
            )
            return False

        o = target.order
        order_id = o.orderId
        old_price = round(float(getattr(o, "auxPrice", 0.0) or 0.0), 2)
        echoed: list[float] = []
        errors: list[tuple[int, str]] = []

        def _on_open_order(trade) -> None:
            if trade.order.orderId == order_id:
                echoed.append(round(float(trade.order.auxPrice), 2))

        def _on_error(req_id, code, msg, *args) -> None:
            if req_id == order_id:
                errors.append((int(code), str(msg)))

        self.ib.openOrderEvent += _on_open_order
        self.ib.errorEvent += _on_error
        try:
            o.auxPrice = new_stop_price
            self.ib.placeOrder(target.contract, o)
            deadline = time.monotonic() + confirm_timeout
            while time.monotonic() < deadline:
                if any(abs(p - new_stop_price) < 0.005 for p in echoed):
                    break
                if any(c not in _BENIGN_ORDER_CODES for c, _ in errors):
                    break
                self.ib.sleep(0.05)
        finally:
            self.ib.openOrderEvent -= _on_open_order
            self.ib.errorEvent -= _on_error

        if any(abs(p - new_stop_price) < 0.005 for p in echoed):
            logger.info(
                f"[ibkr] trailing stop updated: {ticker} "
                f"${old_price:.2f} -> ${new_stop_price:.2f} (order_id={order_id}, confirmed)"
            )
            return True

        # Not confirmed: the broker still holds the old price (rejected) or we
        # don't know. Put the local copy back so anything later in this cycle
        # reads the conservative value, not the one we wished for.
        if not echoed:
            o.auxPrice = old_price
        if errors:
            reason = "; ".join(f"{c}: {m}" for c, m in errors)
        elif echoed:
            reason = f"broker echoed ${echoed[-1]:.2f} instead"
        else:
            reason = f"no broker acknowledgement within {confirm_timeout:.1f}s"
        logger.warning(
            f"[ibkr] trailing stop NOT confirmed: {ticker} order_id={order_id} "
            f"${old_price:.2f} -> ${new_stop_price:.2f} — {reason}"
        )
        return False

    def resize_sell_orders(self, ticker: str, max_qty: int) -> list[int]:
        """Cap every resting SELL order for ticker at max_qty shares.

        A bracket's STP stop and LMT target legs are both written for the
        full entry size. A partial exit (tiered take-profit) shrinks the
        held position but leaves both legs at the old, larger quantity —
        whichever fills first then sells more than is held, opening a short
        for exactly the difference. Resizing in place (same technique as
        modify_stop_order: re-submit with the same orderId) keeps the
        order's protection/target at the correct smaller quantity instead of
        cancelling it outright or leaving it oversized.

        Returns the order_ids that were resized.
        """
        resized = []
        for trade in self.ib.openTrades():
            o = trade.order
            if (trade.contract.symbol == ticker
                    and o.action == "SELL"
                    and o.orderType in ("STP", "LMT")
                    and int(o.totalQuantity) > max_qty):
                old_qty = int(o.totalQuantity)
                o.totalQuantity = max_qty
                self.ib.placeOrder(trade.contract, o)
                logger.info(
                    f"[ibkr] resized resting SELL: {ticker} {old_qty} -> {max_qty} "
                    f"(order_id={o.orderId}, type={o.orderType})"
                )
                resized.append(o.orderId)
        return resized

    # ── Position & Account queries (Phase 2) ────────────────────────────

    def get_positions(self) -> dict[str, dict]:
        """Return current positions keyed by ticker.

        Returns: {ticker: {shares, avg_cost, unrealized_pnl, market_value}}
        """
        positions = self.ib.positions()
        result: dict[str, dict] = {}
        for pos in positions:
            ticker = pos.contract.symbol
            shares = float(pos.position)
            avg_cost = float(pos.avgCost)
            # Initial estimate: cost_basis (avgCost × position). Overwritten by portfolio()
            # enrichment below when available. Sufficient for the SELL veto (non-zero = position open).
            market_value = abs(float(pos.position)) * abs(float(pos.avgCost))
            result[ticker] = {
                "shares": shares,
                "avg_cost": avg_cost,
                "unrealized_pnl": 0.0,
                "market_value": market_value,
            }

        # Enrich with live P&L from portfolio items
        try:
            portfolio_items = self.ib.portfolio()
            for item in portfolio_items:
                ticker = item.contract.symbol
                if ticker in result:
                    result[ticker]["unrealized_pnl"] = float(item.unrealizedPNL or 0.0)
                    result[ticker]["market_value"] = float(item.marketValue or result[ticker]["market_value"])
        except Exception as e:
            logger.warning(f"[ibkr] portfolio enrichment failed: {e}")

        return result

    def get_account_summary(self) -> dict:
        """Return account summary fields.

        Returns: {net_liquidation, cash, day_pnl, buying_power}
        """
        tags = "NetLiquidation,TotalCashValue,DailyPnL,BuyingPower"
        summaries = self.ib.accountSummary()

        parsed: dict[str, float] = {}
        for item in summaries:
            if item.tag in ("NetLiquidation", "TotalCashValue", "DailyPnL", "BuyingPower"):
                try:
                    parsed[item.tag] = float(item.value)
                except (ValueError, TypeError):
                    parsed[item.tag] = 0.0

        # If accountSummary() returned nothing useful, try accountValues()
        if not parsed:
            logger.debug("[ibkr] accountSummary empty, falling back to accountValues")
            for av in self.ib.accountValues():
                if av.tag in ("NetLiquidation", "TotalCashValue", "DailyPnL", "BuyingPower") and av.currency == "USD":
                    try:
                        parsed[av.tag] = float(av.value)
                    except (ValueError, TypeError):
                        pass

        return {
            "net_liquidation": parsed.get("NetLiquidation", 0.0),
            "cash": parsed.get("TotalCashValue", 0.0),
            "day_pnl": parsed.get("DailyPnL", 0.0),
            "buying_power": parsed.get("BuyingPower", 0.0),
        }

    def get_daily_pnl(self) -> float:
        """Convenience wrapper — returns today's P&L as a float."""
        return self.get_account_summary()["day_pnl"]


@contextmanager
def ibkr_session(
    host: str = DEFAULT_HOST,
    port: int = PAPER_PORT,
    client_id: int = 1,
) -> Iterator[IBKRConnection]:
    """Context manager that connects/disconnects cleanly."""
    conn = IBKRConnection(host=host, port=port, client_id=client_id)
    try:
        conn.connect()
        yield conn
    finally:
        conn.disconnect()
