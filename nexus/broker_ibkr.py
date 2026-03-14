"""Interactive Brokers broker adapter — via ib_insync.

Setup (one-time)
────────────────
1. pip install ib_insync
2. Download TWS (Trader Workstation) or IB Gateway from interactivebrokers.com
3. Log in to TWS/IB Gateway with your IBKR account
4. Enable API access:
   TWS:        File → Global Config → API → Settings
               ✓ Enable ActiveX and Socket Clients
               ✓ Read-Only API: OFF (needs write for order placement)
               Socket port: 7497 (paper) / 7496 (live)
   IB Gateway: Configure → Settings → API → Enable
               Socket port: 4002 (paper) / 4001 (live)
5. Add to .env:
   IBKR_HOST=127.0.0.1
   IBKR_PORT=7497        # paper trading port
   IBKR_CLIENT_ID=1      # unique integer per simultaneous connection

Short selling on IBKR
─────────────────────
IBKR supports short selling on margin accounts. A SELL order on a stock you
don't own is automatically treated as a short sale. Covering is a BUY on the
same symbol. Margin account required (not cash accounts).

IBKR advantages over Alpaca
────────────────────────────
- Global markets: US, EU, HK, AU, CA, JP — not just US equities
- Fractional shares on 50+ ETFs/stocks
- Direct market access with smart order routing
- No PDT rule on non-US accounts
- Much lower margin rates than Alpaca
- Options, futures, forex, bonds — not just equities
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from nexus.broker import (
    AccountInfo, BaseBroker, OrderResult, OrderSide, OrderStatus,
    OrderType, Position, Quote,
)
from nexus.logger import get_logger

log = get_logger("broker.ibkr")


class IBKRBroker(BaseBroker):
    """Interactive Brokers via ib_insync (community async wrapper for TWS API).

    Usage:
        from nexus.broker_ibkr import IBKRBroker
        broker = IBKRBroker(port=7497, paper=True)   # paper trading
        engine = NEXUSEngine(broker=broker)
        await engine.start()
    """

    name = "ibkr"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,          # 7497=TWS paper, 7496=TWS live, 4002=IB GW paper
        client_id: int = 1,
        paper: bool = True,
        timeout: int = 20,
    ) -> None:
        self.host      = host
        self.port      = port
        self.client_id = client_id
        self.paper     = paper
        self.timeout   = timeout
        self._ib       = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ib is not None and self._ib.isConnected()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            import ib_insync as ibs
            self._ib = ibs.IB()
            await self._ib.connectAsync(
                self.host, self.port,
                clientId=self.client_id,
                timeout=self.timeout,
            )
            self._connected = True
            log.info("IBKR connected", host=self.host, port=self.port,
                     paper=self.paper, account=self._account_id())
            return True
        except ImportError:
            log.warning("ib_insync not installed",
                        hint="pip install ib_insync")
            return False
        except Exception as e:
            log.error("IBKR connect failed", error=str(e),
                      hint="Is TWS/IB Gateway running with API enabled?")
            return False

    async def disconnect(self) -> None:
        if self._ib:
            self._ib.disconnect()
        self._connected = False
        log.info("IBKR disconnected")

    def _account_id(self) -> str:
        try:
            return self._ib.managedAccounts()[0] if self._ib else "?"
        except Exception:
            return "?"

    # ── Market hours ───────────────────────────────────────────────────────────

    async def is_market_open(self) -> bool:
        try:
            # ib_insync doesn't have a direct market-hours call; use reqCurrentTime
            # and compare to NYSE hours (9:30–16:00 ET Mon–Fri)
            import datetime, zoneinfo
            et = zoneinfo.ZoneInfo("America/New_York")
            now = datetime.datetime.now(tz=et)
            if now.weekday() >= 5:          # Sat/Sun
                return False
            market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
            market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
            return market_open <= now <= market_close
        except Exception:
            return True   # assume open on error

    # ── Quotes ─────────────────────────────────────────────────────────────────

    async def get_quote(self, ticker: str) -> Optional[Quote]:
        if not self.is_connected:
            return None
        try:
            import ib_insync as ibs
            contract = ibs.Stock(ticker, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            ticker_data = self._ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(1)          # allow data snapshot to arrive
            bid  = float(ticker_data.bid  or 0)
            ask  = float(ticker_data.ask  or 0)
            last = float(ticker_data.last or (bid + ask) / 2)
            self._ib.cancelMktData(contract)
            return Quote(
                ticker=ticker, bid=bid, ask=ask, last=last,
                volume=int(ticker_data.volume or 0),
                open=float(ticker_data.open or 0),
                high=float(ticker_data.high or 0),
                low=float(ticker_data.low  or 0),
                prev_close=float(ticker_data.close or 0),
            )
        except Exception as e:
            log.error("IBKR get_quote failed", ticker=ticker, error=str(e))
            return None

    async def get_batch_quotes(self, tickers: List[str]) -> Dict[str, Quote]:
        results = {}
        for ticker in tickers:
            q = await self.get_quote(ticker)
            if q:
                results[ticker] = q
        return results

    # ── Account & positions ────────────────────────────────────────────────────

    async def get_positions(self) -> List[Position]:
        if not self.is_connected:
            return []
        try:
            await asyncio.to_thread(self._ib.reqPositions)
            await asyncio.sleep(0.5)
            positions = self._ib.positions()
            result = []
            for p in positions:
                if p.contract.secType != "STK":
                    continue
                qty = float(p.position)
                side = "SHORT" if qty < 0 else "LONG"
                result.append(Position(
                    ticker=p.contract.symbol,
                    shares=abs(qty),
                    avg_cost=float(p.avgCost),
                    current_price=float(p.avgCost),   # refreshed on quote fetch
                    broker=self.name,
                    side=side,
                ))
            return result
        except Exception as e:
            log.error("IBKR get_positions failed", error=str(e))
            return []

    async def get_account_info(self) -> AccountInfo:
        try:
            account = self._account_id()
            summary = {
                s.tag: s.value
                for s in self._ib.accountSummary(account)
            }
            cash     = float(summary.get("TotalCashValue", 0))
            net_liq  = float(summary.get("NetLiquidation", 0))
            buy_pwr  = float(summary.get("BuyingPower", 0))
            day_pnl  = float(summary.get("DayTradesRemaining", 0))  # proxy
            total_pnl = float(summary.get("UnrealizedPnL", 0))
            return AccountInfo(
                broker=self.name,
                cash=cash,
                portfolio_value=net_liq,
                buying_power=buy_pwr,
                day_pnl=day_pnl,
                total_pnl=total_pnl,
                paper=self.paper,
            )
        except Exception as e:
            log.error("IBKR account info failed", error=str(e))
            return AccountInfo(self.name, 0, 0, 0, 0, 0, self.paper)

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def place_order(
        self, ticker: str, side: OrderSide, qty: float,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        if not self.is_connected:
            return OrderResult("", ticker, side, qty, 0, 0,
                               OrderStatus.REJECTED, self.name, "Not connected")
        try:
            import ib_insync as ibs
            contract = ibs.Stock(ticker, "SMART", "USD")
            action   = "BUY" if side == OrderSide.BUY else "SELL"

            if order_type == OrderType.LIMIT and limit_price:
                order = ibs.LimitOrder(action, int(qty), round(limit_price, 2))
            else:
                order = ibs.MarketOrder(action, int(qty))

            trade = self._ib.placeOrder(contract, order)
            await asyncio.sleep(0.5)   # allow initial fill status

            status = self._map_status(str(trade.orderStatus.status))
            return OrderResult(
                order_id=str(trade.order.orderId),
                ticker=ticker,
                side=side,
                requested_qty=qty,
                filled_qty=float(trade.orderStatus.filled),
                avg_fill_price=float(trade.orderStatus.avgFillPrice or limit_price or 0),
                status=status,
                broker=self.name,
            )
        except Exception as e:
            log.error("IBKR place_order failed", ticker=ticker, error=str(e))
            return OrderResult("", ticker, side, qty, 0, 0,
                               OrderStatus.REJECTED, self.name, str(e))

    async def cancel_order(self, order_id: str) -> bool:
        try:
            order = next(
                (t.order for t in self._ib.trades()
                 if str(t.order.orderId) == order_id),
                None,
            )
            if order:
                self._ib.cancelOrder(order)
            return True
        except Exception as e:
            log.error("IBKR cancel_order failed", order_id=order_id, error=str(e))
            return False

    async def get_order_status(self, order_id: str) -> OrderResult:
        try:
            trade = next(
                (t for t in self._ib.trades()
                 if str(t.order.orderId) == order_id),
                None,
            )
            if not trade:
                return OrderResult(order_id, "", OrderSide.BUY, 0, 0, 0,
                                   OrderStatus.CANCELLED, self.name)
            status = self._map_status(str(trade.orderStatus.status))
            return OrderResult(
                order_id=order_id,
                ticker=trade.contract.symbol,
                side=OrderSide.BUY if trade.order.action == "BUY" else OrderSide.SELL,
                requested_qty=float(trade.order.totalQuantity),
                filled_qty=float(trade.orderStatus.filled),
                avg_fill_price=float(trade.orderStatus.avgFillPrice or 0),
                status=status,
                broker=self.name,
            )
        except Exception as e:
            log.error("IBKR get_order_status failed", error=str(e))
            return OrderResult(order_id, "", OrderSide.BUY, 0, 0, 0,
                               OrderStatus.REJECTED, self.name)

    @staticmethod
    def _map_status(ibkr_status: str) -> OrderStatus:
        return {
            "Submitted":       OrderStatus.SUBMITTED,
            "PreSubmitted":    OrderStatus.SUBMITTED,
            "PartiallyFilled": OrderStatus.PARTIAL,
            "Filled":          OrderStatus.FILLED,
            "Cancelled":       OrderStatus.CANCELLED,
            "ApiCancelled":    OrderStatus.CANCELLED,
            "Inactive":        OrderStatus.CANCELLED,
        }.get(ibkr_status, OrderStatus.PENDING)
