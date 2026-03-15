"""Moomoo / Futu broker adapter — via moomoo-api.

Setup (one-time)
────────────────
1. pip install moomoo-api
2. Download OpenD gateway from: https://www.moomoo.com/download/OpenAPI
3. Launch OpenD — it must be running before connecting
4. Log in to OpenD with your Moomoo account
5. Add to .env:
   MOOMOO_HOST=127.0.0.1
   MOOMOO_PORT=11111       # OpenD default
   MOOMOO_TRADE_ENV=SIMULATE   # SIMULATE or REAL

OpenD gateway ports
───────────────────
- Default: 11111 (quotes + trade)
- OpenD must be running locally (or accessible via LAN/VPN)

Short selling on Moomoo
───────────────────────
Moomoo supports short selling on margin accounts. Open a short via SELL
on a stock you don't own; cover via BUY. Account must have margin/options
enabled — not available in basic cash accounts.

Security codes
──────────────
US equities use format "US.AAPL", "US.MSFT", etc.
This adapter handles the conversion automatically (ticker → "US.TICKER").
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from nexus.broker import (
    AccountInfo, BaseBroker, OrderResult, OrderSide, OrderStatus,
    OrderType, Position, Quote,
)
from nexus.logger import get_logger

log = get_logger("broker.moomoo")


def _us(ticker: str) -> str:
    """Convert bare ticker to Futu US code, e.g. AAPL → US.AAPL."""
    return ticker if ticker.startswith("US.") else f"US.{ticker}"


def _bare(code: str) -> str:
    """Strip US. prefix, e.g. US.AAPL → AAPL."""
    return code.replace("US.", "") if code.startswith("US.") else code


class MoomooTrdEnv:
    SIMULATE = "SIMULATE"
    REAL = "REAL"


class MoomooBroker(BaseBroker):
    """Moomoo / Futu broker via futu-openapi + OpenD gateway.

    Usage:
        from nexus.broker_moomoo import MoomooBroker
        broker = MoomooBroker(trade_env="SIMULATE")   # paper trading
        engine = NEXUSEngine(broker=broker)
        await engine.start()
    """

    name = "moomoo"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        trade_env: str = MoomooTrdEnv.SIMULATE,
        market: str = "US",
    ) -> None:
        self.host       = host
        self.port       = port
        self.trade_env  = trade_env
        self.market     = market
        self._quote_ctx = None
        self._trade_ctx = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._quote_ctx is not None and self._trade_ctx is not None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            import moomoo as ft
            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL

            self._quote_ctx = ft.OpenQuoteContext(host=self.host, port=self.port)
            self._trade_ctx = ft.OpenSecTradeContext(
                filter_trdmarket=ft.TrdMarket.US,
                host=self.host,
                port=self.port,
                security_firm=ft.SecurityFirm.FUTUSECURITIES,
            )
            # Verify connectivity with a ping-style call
            ret, _ = self._quote_ctx.get_global_state()
            if ret != ft.RET_OK:
                raise ConnectionError("OpenD not reachable")

            self._connected = True
            log.info("Moomoo connected",
                     host=self.host, port=self.port,
                     env=self.trade_env)
            return True
        except ImportError:
            log.warning("moomoo-api not installed",
                        hint="pip install moomoo-api")
            return False
        except Exception as e:
            log.error("Moomoo connect failed", error=str(e),
                      hint="Is OpenD gateway running?")
            return False

    async def disconnect(self) -> None:
        if self._quote_ctx:
            self._quote_ctx.close()
        if self._trade_ctx:
            self._trade_ctx.close()
        self._connected = False
        log.info("Moomoo disconnected")

    # ── Market hours ───────────────────────────────────────────────────────────

    async def is_market_open(self) -> bool:
        try:
            import moomoo as ft
            ret, data = self._quote_ctx.get_market_state(["US.AAPL"])
            if ret != ft.RET_OK or data.empty:
                return True   # fallback: assume open
            state = data["market_state"].iloc[0]
            # MORNING_PRE, MORNING, AFTERNOON = open; CLOSED, AFTER_HOURS = closed for trading
            return state in ("MORNING", "AFTERNOON")
        except Exception:
            return True

    # ── Quotes ─────────────────────────────────────────────────────────────────

    async def get_quote(self, ticker: str) -> Optional[Quote]:
        if not self.is_connected:
            return None
        try:
            import moomoo as ft
            code = _us(ticker)
            ret, data = self._quote_ctx.get_stock_quote([code])
            if ret != ft.RET_OK or data.empty:
                return None
            row = data.iloc[0]
            bid   = float(row.get("bid_price", 0) or 0)
            ask   = float(row.get("ask_price", 0) or 0)
            last  = float(row.get("last_done", 0) or 0)
            if last == 0:
                last = (bid + ask) / 2
            return Quote(
                ticker=ticker,
                bid=bid,
                ask=ask,
                last=last,
                volume=int(row.get("volume", 0) or 0),
                open=float(row.get("open_price", 0) or 0),
                high=float(row.get("high_price", 0) or 0),
                low=float(row.get("low_price",  0) or 0),
                prev_close=float(row.get("prev_close_price", 0) or 0),
            )
        except Exception as e:
            log.error("Moomoo get_quote failed", ticker=ticker, error=str(e))
            return None

    async def get_batch_quotes(self, tickers: List[str]) -> Dict[str, Quote]:
        if not self.is_connected:
            return {}
        try:
            import moomoo as ft
            codes = [_us(t) for t in tickers]
            ret, data = self._quote_ctx.get_stock_quote(codes)
            if ret != ft.RET_OK or data.empty:
                return {}
            result = {}
            for _, row in data.iterrows():
                t = _bare(str(row["code"]))
                bid   = float(row.get("bid_price", 0) or 0)
                ask   = float(row.get("ask_price", 0) or 0)
                last  = float(row.get("last_done", 0) or 0)
                if last == 0:
                    last = (bid + ask) / 2
                result[t] = Quote(
                    ticker=t, bid=bid, ask=ask, last=last,
                    volume=int(row.get("volume", 0) or 0),
                    open=float(row.get("open_price", 0) or 0),
                    high=float(row.get("high_price", 0) or 0),
                    low=float(row.get("low_price",  0) or 0),
                    prev_close=float(row.get("prev_close_price", 0) or 0),
                )
            return result
        except Exception as e:
            log.error("Moomoo get_batch_quotes failed", error=str(e))
            return {}

    # ── Account & positions ────────────────────────────────────────────────────

    async def get_positions(self) -> List[Position]:
        if not self.is_connected:
            return []
        try:
            import moomoo as ft
            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            ret, data = self._trade_ctx.position_list_query(trd_env=env)
            if ret != ft.RET_OK or data.empty:
                return []
            result = []
            for _, row in data.iterrows():
                qty = float(row.get("qty", 0) or 0)
                if qty == 0:
                    continue
                side = "SHORT" if qty < 0 else "LONG"
                ticker = _bare(str(row.get("code", "")))
                result.append(Position(
                    ticker=ticker,
                    shares=abs(qty),
                    avg_cost=float(row.get("cost_price", 0) or 0),
                    current_price=float(row.get("market_val", 0) or 0) / max(abs(qty), 1),
                    broker=self.name,
                    side=side,
                ))
            return result
        except Exception as e:
            log.error("Moomoo get_positions failed", error=str(e))
            return []

    @staticmethod
    def _safe_float(val, default: float = 0.0) -> float:
        """Convert a value to float, returning default for N/A or None."""
        if val is None or val == "N/A" or val == "":
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    async def get_account_info(self) -> AccountInfo:
        try:
            import moomoo as ft
            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            ret, data = self._trade_ctx.accinfo_query(trd_env=env)
            if ret != ft.RET_OK or data.empty:
                raise ValueError(f"accinfo_query failed: {data}")
            row = data.iloc[0]
            cash      = self._safe_float(row.get("cash", 0))
            net_liq   = self._safe_float(row.get("total_assets", 0))
            buy_pwr   = self._safe_float(row.get("max_power_short", 0))
            total_pnl = self._safe_float(row.get("total_profit_val", 0))
            return AccountInfo(
                broker=self.name,
                cash=cash,
                portfolio_value=net_liq,
                buying_power=buy_pwr,
                day_pnl=0.0,
                total_pnl=total_pnl,
                paper=(self.trade_env == MoomooTrdEnv.SIMULATE),
            )
        except Exception as e:
            log.error("Moomoo account info failed", error=str(e))
            paper = self.trade_env == MoomooTrdEnv.SIMULATE
            return AccountInfo(self.name, 0, 0, 0, 0, 0, paper)

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
            import moomoo as ft
            env  = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            code = _us(ticker)
            trd_side = ft.TrdSide.BUY if side == OrderSide.BUY else ft.TrdSide.SELL

            if order_type == OrderType.MARKET:
                price    = 0.0
                ord_type = ft.OrderType.MARKET
            else:
                price    = round(limit_price or 0.0, 2)
                ord_type = ft.OrderType.NORMAL   # LIMIT

            ret, data = self._trade_ctx.place_order(
                price=price,
                qty=int(qty),
                code=code,
                trd_side=trd_side,
                order_type=ord_type,
                trd_env=env,
            )
            if ret != ft.RET_OK or data.empty:
                return OrderResult("", ticker, side, qty, 0, 0,
                                   OrderStatus.REJECTED, self.name, str(data))
            order_id = str(data.iloc[0].get("order_id", ""))
            return OrderResult(
                order_id=order_id,
                ticker=ticker,
                side=side,
                requested_qty=qty,
                filled_qty=0.0,    # fill confirmed via get_order_status
                avg_fill_price=price,
                status=OrderStatus.SUBMITTED,
                broker=self.name,
            )
        except Exception as e:
            log.error("Moomoo place_order failed", ticker=ticker, error=str(e))
            return OrderResult("", ticker, side, qty, 0, 0,
                               OrderStatus.REJECTED, self.name, str(e))

    async def cancel_order(self, order_id: str) -> bool:
        try:
            import moomoo as ft
            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            ret, data = self._trade_ctx.modify_order(
                modify_order_op=ft.ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=env,
            )
            return ret == ft.RET_OK
        except Exception as e:
            log.error("Moomoo cancel_order failed", order_id=order_id, error=str(e))
            return False

    async def get_order_status(self, order_id: str) -> OrderResult:
        try:
            import moomoo as ft
            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            ret, data = self._trade_ctx.order_list_query(
                order_id=order_id, trd_env=env
            )
            if ret != ft.RET_OK or data.empty:
                return OrderResult(order_id, "", OrderSide.BUY, 0, 0, 0,
                                   OrderStatus.CANCELLED, self.name)
            row = data.iloc[0]
            status = self._map_status(str(row.get("order_status", "")))
            trd_side_str = str(row.get("trd_side", "BUY")).upper()
            side = OrderSide.BUY if "BUY" in trd_side_str else OrderSide.SELL
            ticker = _bare(str(row.get("code", "")))
            return OrderResult(
                order_id=order_id,
                ticker=ticker,
                side=side,
                requested_qty=float(row.get("qty", 0) or 0),
                filled_qty=float(row.get("dealt_qty", 0) or 0),
                avg_fill_price=float(row.get("dealt_avg_price", 0) or 0),
                status=status,
                broker=self.name,
            )
        except Exception as e:
            log.error("Moomoo get_order_status failed", error=str(e))
            return OrderResult(order_id, "", OrderSide.BUY, 0, 0, 0,
                               OrderStatus.REJECTED, self.name)

    @staticmethod
    def _map_status(futu_status: str) -> OrderStatus:
        """Map Futu order status string → NEXUS OrderStatus."""
        s = futu_status.upper()
        if "FILLED_ALL" in s or "FILLED_PART_CANCELLED" in s:
            return OrderStatus.FILLED
        if "FILLED_PART" in s or "DEALING" in s:
            return OrderStatus.PARTIAL
        if "SUBMITTED" in s or "WAITING_SUBMIT" in s or "SUBMITTING" in s:
            return OrderStatus.SUBMITTED
        if "CANCELLED" in s or "DELETED" in s or "FAILED" in s:
            return OrderStatus.CANCELLED
        return OrderStatus.PENDING
