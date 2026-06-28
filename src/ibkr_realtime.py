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
from contextlib import contextmanager
from typing import Iterator, Literal

import pandas as pd
from ib_async import IB, Stock, LimitOrder, StopOrder, util

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
PAPER_PORT = 4002
LIVE_PORT = 4001

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
    ) -> int:
        """Place a bracket order (LMT entry + STP stop + LMT target).

        Returns the parent order_id.
        """
        contract = Stock(ticker, "SMART", "USD")
        self.ib.qualifyContracts(contract)

        parent = LimitOrder(action, shares, entry_price)
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False

        stop_action = "SELL" if action == "BUY" else "BUY"

        stop_order = StopOrder(stop_action, shares, stop_price)
        stop_order.orderId = self.ib.client.getReqId()
        stop_order.parentId = parent.orderId
        stop_order.transmit = False

        target_order = LimitOrder(stop_action, shares, target_price)
        target_order.orderId = self.ib.client.getReqId()
        target_order.parentId = parent.orderId
        target_order.transmit = True  # transmit the whole bracket

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
            f"parent_id={parent.orderId}"
        )
        return parent.orderId

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
        """Return all open orders as a list of dicts."""
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
            })
        return result

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
