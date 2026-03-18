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
    AccountInfo,
    BaseBroker,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
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
        self.host = host
        self.port = port
        self.trade_env = trade_env
        self.market = market
        self._quote_ctx = None
        self._trade_ctx = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._quote_ctx is not None and self._trade_ctx is not None

    async def _ensure_connected(self) -> bool:
        """Auto-reconnect if connection was lost."""
        if self.is_connected:
            return True
        log.warning("Moomoo connection lost, attempting reconnect...")
        self._connected = False
        return await self.connect()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            import moomoo as ft

            # Run blocking constructors in thread to avoid blocking event loop
            self._quote_ctx = await asyncio.to_thread(
                ft.OpenQuoteContext, host=self.host, port=self.port
            )
            self._trade_ctx = await asyncio.to_thread(
                ft.OpenSecTradeContext,
                filter_trdmarket=ft.TrdMarket.US,
                host=self.host,
                port=self.port,
                security_firm=ft.SecurityFirm.FUTUSECURITIES,
            )
            # Verify connectivity with a ping-style call
            ret, _ = await asyncio.to_thread(self._quote_ctx.get_global_state)
            if ret != ft.RET_OK:
                raise ConnectionError("OpenD not reachable")

            self._connected = True
            log.info("Moomoo connected", host=self.host, port=self.port, env=self.trade_env)
            return True
        except ImportError:
            log.warning("moomoo-api not installed", hint="pip install moomoo-api")
            return False
        except Exception as e:
            log.error("Moomoo connect failed", error=str(e), hint="Is OpenD gateway running?")
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
        if not await self._ensure_connected():
            return True  # fallback: assume open
        try:
            import moomoo as ft

            ret, data = await asyncio.to_thread(self._quote_ctx.get_market_state, ["US.AAPL"])
            if ret != ft.RET_OK or data.empty:
                return True  # fallback: assume open
            state = data["market_state"].iloc[0]
            # MORNING_PRE, MORNING, AFTERNOON = open; CLOSED, AFTER_HOURS = closed for trading
            return state in ("MORNING", "AFTERNOON")
        except Exception:
            return True

    # ── Quotes ─────────────────────────────────────────────────────────────────

    async def get_quote(self, ticker: str) -> Optional[Quote]:
        if not await self._ensure_connected():
            return None
        try:
            import moomoo as ft

            code = _us(ticker)
            ret, data = await asyncio.to_thread(self._quote_ctx.get_stock_quote, [code])
            if ret != ft.RET_OK or data.empty:
                return None
            row = data.iloc[0]
            bid = float(row.get("bid_price", 0) or 0)
            ask = float(row.get("ask_price", 0) or 0)
            last = float(row.get("last_done", 0) or 0)
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
                low=float(row.get("low_price", 0) or 0),
                prev_close=float(row.get("prev_close_price", 0) or 0),
            )
        except Exception as e:
            log.error("Moomoo get_quote failed", ticker=ticker, error=str(e))
            return None

    async def get_batch_quotes(self, tickers: List[str]) -> Dict[str, Quote]:
        if not await self._ensure_connected():
            return {}
        try:
            import moomoo as ft

            codes = [_us(t) for t in tickers]
            ret, data = await asyncio.to_thread(self._quote_ctx.get_stock_quote, codes)
            if ret != ft.RET_OK or data.empty:
                return {}
            result = {}
            for _, row in data.iterrows():
                t = _bare(str(row["code"]))
                bid = float(row.get("bid_price", 0) or 0)
                ask = float(row.get("ask_price", 0) or 0)
                last = float(row.get("last_done", 0) or 0)
                if last == 0:
                    last = (bid + ask) / 2
                result[t] = Quote(
                    ticker=t,
                    bid=bid,
                    ask=ask,
                    last=last,
                    volume=int(row.get("volume", 0) or 0),
                    open=float(row.get("open_price", 0) or 0),
                    high=float(row.get("high_price", 0) or 0),
                    low=float(row.get("low_price", 0) or 0),
                    prev_close=float(row.get("prev_close_price", 0) or 0),
                )
            return result
        except Exception as e:
            log.error("Moomoo get_batch_quotes failed", error=str(e))
            return {}

    # ── Account & positions ────────────────────────────────────────────────────

    async def get_positions(self) -> List[Position]:
        if not await self._ensure_connected():
            return []
        try:
            import moomoo as ft

            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            ret, data = await asyncio.to_thread(self._trade_ctx.position_list_query, trd_env=env)
            if ret != ft.RET_OK or data.empty:
                return []
            result = []
            for _, row in data.iterrows():
                qty = float(row.get("qty", 0) or 0)
                if qty == 0:
                    continue
                side = "SHORT" if qty < 0 else "LONG"
                ticker = _bare(str(row.get("code", "")))
                result.append(
                    Position(
                        ticker=ticker,
                        shares=abs(qty),
                        avg_cost=float(row.get("cost_price", 0) or 0),
                        current_price=abs(float(row.get("market_val", 0) or 0)) / max(abs(qty), 1),
                        broker=self.name,
                        side=side,
                    )
                )
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
        if not await self._ensure_connected():
            paper = self.trade_env == MoomooTrdEnv.SIMULATE
            return AccountInfo(self.name, 0, 0, 0, 0, 0, paper)
        try:
            import moomoo as ft

            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            ret, data = await asyncio.to_thread(self._trade_ctx.accinfo_query, trd_env=env)
            if ret != ft.RET_OK or data.empty:
                raise ValueError(f"accinfo_query failed: {data}")
            row = data.iloc[0]
            cash = self._safe_float(row.get("cash", 0))
            net_liq = self._safe_float(row.get("total_assets", 0))
            buy_pwr = self._safe_float(row.get("max_power_short", 0))
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
        self,
        ticker: str,
        side: OrderSide,
        qty: float,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        if not await self._ensure_connected():
            return OrderResult(
                "", ticker, side, qty, 0, 0, OrderStatus.REJECTED, self.name, "Not connected"
            )
        try:
            import moomoo as ft

            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            code = _us(ticker)
            trd_side = ft.TrdSide.BUY if side == OrderSide.BUY else ft.TrdSide.SELL

            if order_type == OrderType.MARKET:
                price = 0.0
                ord_type = ft.OrderType.MARKET
            else:
                price = round(limit_price or 0.0, 2)
                ord_type = ft.OrderType.NORMAL  # LIMIT

            ret, data = await asyncio.to_thread(
                self._trade_ctx.place_order,
                price=price,
                qty=int(qty),
                code=code,
                trd_side=trd_side,
                order_type=ord_type,
                trd_env=env,
            )
            if ret != ft.RET_OK or data.empty:
                return OrderResult(
                    "", ticker, side, qty, 0, 0, OrderStatus.REJECTED, self.name, str(data)
                )
            order_id = str(data.iloc[0].get("order_id", ""))
            return OrderResult(
                order_id=order_id,
                ticker=ticker,
                side=side,
                requested_qty=qty,
                filled_qty=0.0,  # fill confirmed via get_order_status
                avg_fill_price=price,
                status=OrderStatus.SUBMITTED,
                broker=self.name,
            )
        except Exception as e:
            log.error("Moomoo place_order failed", ticker=ticker, error=str(e))
            return OrderResult("", ticker, side, qty, 0, 0, OrderStatus.REJECTED, self.name, str(e))

    async def cancel_order(self, order_id: str) -> bool:
        if not await self._ensure_connected():
            return False
        try:
            import moomoo as ft

            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            ret, data = await asyncio.to_thread(
                self._trade_ctx.modify_order,
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
        if not await self._ensure_connected():
            return OrderResult(
                order_id, "", OrderSide.BUY, 0, 0, 0, OrderStatus.CANCELLED, self.name
            )
        try:
            import moomoo as ft

            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            ret, data = await asyncio.to_thread(
                self._trade_ctx.order_list_query,
                order_id=order_id,
                trd_env=env,
            )
            if ret != ft.RET_OK or data.empty:
                return OrderResult(
                    order_id, "", OrderSide.BUY, 0, 0, 0, OrderStatus.CANCELLED, self.name
                )
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
            return OrderResult(
                order_id, "", OrderSide.BUY, 0, 0, 0, OrderStatus.REJECTED, self.name
            )

    # ── Options ────────────────────────────────────────────────────────────────

    async def get_option_expirations(self, ticker: str) -> list[str]:
        """Get available option expiration dates for a ticker."""
        if not await self._ensure_connected():
            return []
        try:
            import moomoo as ft

            code = _us(ticker)
            ret, data = await asyncio.to_thread(
                self._quote_ctx.get_option_expiration_date, code_list=[code]
            )
            if ret != ft.RET_OK or data.empty:
                return []
            # Column is 'strike_time' with datetime strings
            dates = sorted(data["strike_time"].unique().tolist())
            # Normalize to ISO date strings
            result = []
            for d in dates:
                try:
                    result.append(str(d)[:10])
                except Exception:
                    pass
            return result
        except Exception as e:
            log.error("Moomoo get_option_expirations failed", ticker=ticker, error=str(e))
            return []

    async def get_option_chain(self, ticker: str, expiration: str) -> list:
        """Get option chain for a ticker at a specific expiration."""
        if not await self._ensure_connected():
            return []
        try:
            import moomoo as ft

            from nexus.broker import OptionsContract, OptionsQuote

            code = _us(ticker)
            ret, data = await asyncio.to_thread(
                self._quote_ctx.get_option_chain,
                code=code,
                start=expiration,
                end=expiration,
            )
            if ret != ft.RET_OK or data.empty:
                log.warning("Option chain empty", ticker=ticker, expiration=expiration)
                return []

            # Get quotes for all option codes
            option_codes = data["code"].tolist()
            if not option_codes:
                return []

            # Fetch quotes in batches of 50
            quotes_data = {}
            for i in range(0, len(option_codes), 50):
                batch = option_codes[i : i + 50]
                qret, qdata = await asyncio.to_thread(
                    self._quote_ctx.get_stock_quote, batch
                )
                if qret == ft.RET_OK and not qdata.empty:
                    for _, qrow in qdata.iterrows():
                        quotes_data[str(qrow["code"])] = qrow

            result = []
            for _, row in data.iterrows():
                opt_code = str(row["code"])
                strike = float(row.get("strike_price", 0) or 0)
                opt_type_raw = str(row.get("option_type", "")).upper()
                right = "CALL" if "CALL" in opt_type_raw else "PUT"

                contract = OptionsContract(
                    ticker=ticker,
                    strike=strike,
                    expiration=expiration,
                    right=right,
                    code=opt_code,
                )

                # Merge quote data if available
                qrow = quotes_data.get(opt_code)
                if qrow is not None:
                    bid = float(qrow.get("bid_price", 0) or 0)
                    ask = float(qrow.get("ask_price", 0) or 0)
                    last = float(qrow.get("last_done", 0) or 0)
                    vol = int(qrow.get("volume", 0) or 0)
                    oi = int(qrow.get("open_interest", 0) or 0)
                else:
                    bid = ask = last = 0.0
                    vol = oi = 0

                result.append(
                    OptionsQuote(
                        contract=contract,
                        bid=bid,
                        ask=ask,
                        last=last,
                        volume=vol,
                        open_interest=oi,
                        implied_vol=0.0,  # populated if snapshot available
                    )
                )
            return result
        except Exception as e:
            log.error("Moomoo get_option_chain failed", ticker=ticker, error=str(e))
            return []

    async def place_options_order(
        self,
        contract,
        side,
        qty: int,
        order_type=None,
        limit_price=None,
    ) -> OrderResult:
        """Place an options order via the option contract code."""
        if not await self._ensure_connected():
            from nexus.broker import OrderResult, OrderSide, OrderStatus
            return OrderResult(
                "", contract.ticker, side, qty, 0, 0, OrderStatus.REJECTED, self.name, "Not connected"
            )
        try:
            import moomoo as ft

            from nexus.broker import OrderResult, OrderStatus
            from nexus.broker import OrderType as OT

            if order_type is None:
                from nexus.broker import OrderType as OT
                order_type = OT.LIMIT

            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            code = contract.code
            if not code:
                return OrderResult(
                    "", contract.ticker, side, qty, 0, 0,
                    OrderStatus.REJECTED, self.name, "No option code"
                )

            from nexus.broker import OrderSide
            trd_side = ft.TrdSide.BUY if side == OrderSide.BUY else ft.TrdSide.SELL

            if order_type == OT.MARKET:
                price = 0.0
                ord_type = ft.OrderType.MARKET
            else:
                price = round(limit_price or 0.0, 2)
                ord_type = ft.OrderType.NORMAL

            ret, data = await asyncio.to_thread(
                self._trade_ctx.place_order,
                price=price,
                qty=int(qty),
                code=code,
                trd_side=trd_side,
                order_type=ord_type,
                trd_env=env,
            )
            if ret != ft.RET_OK or data.empty:
                return OrderResult(
                    "", contract.ticker, side, qty, 0, 0,
                    OrderStatus.REJECTED, self.name, str(data),
                )
            order_id = str(data.iloc[0].get("order_id", ""))
            return OrderResult(
                order_id=order_id,
                ticker=contract.ticker,
                side=side,
                requested_qty=qty,
                filled_qty=0.0,
                avg_fill_price=price,
                status=OrderStatus.SUBMITTED,
                broker=self.name,
            )
        except Exception as e:
            log.error("Moomoo place_options_order failed", ticker=contract.ticker, error=str(e))
            from nexus.broker import OrderResult, OrderStatus
            return OrderResult(
                "", contract.ticker, side, qty, 0, 0,
                OrderStatus.REJECTED, self.name, str(e),
            )

    async def get_order_history(self, limit: int = 50) -> List[dict]:
        """Get order history from Moomoo (current session + history)."""
        if not await self._ensure_connected():
            return []
        try:
            import moomoo as ft

            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            result = []

            # Current session orders
            ret, data = await asyncio.to_thread(self._trade_ctx.order_list_query, trd_env=env)
            if ret == ft.RET_OK and not data.empty:
                for _, row in data.iterrows():
                    result.append(self._parse_order_row(row))

            # Historical orders (previous sessions)
            try:
                ret2, data2 = await asyncio.to_thread(
                    self._trade_ctx.history_order_list_query,
                    trd_env=env, status_filter_list=[], start="", end="",
                )
                if ret2 == ft.RET_OK and not data2.empty:
                    existing_ids = {o["order_id"] for o in result}
                    for _, row in data2.iterrows():
                        parsed = self._parse_order_row(row)
                        if parsed["order_id"] not in existing_ids:
                            result.append(parsed)
            except Exception:
                pass  # history_order_list_query may not be available

            # Sort by created_at descending
            result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return result[:limit]
        except Exception as e:
            log.error("Moomoo get_order_history failed", error=str(e))
            return []

    async def get_deal_history(self, limit: int = 50) -> List[dict]:
        """Get deal/execution history from Moomoo."""
        if not await self._ensure_connected():
            return []
        try:
            import moomoo as ft

            env = ft.TrdEnv.SIMULATE if self.trade_env == MoomooTrdEnv.SIMULATE else ft.TrdEnv.REAL
            result = []

            # Try current session deals first
            try:
                ret, data = await asyncio.to_thread(self._trade_ctx.deal_list_query, trd_env=env)
                if ret == ft.RET_OK and not data.empty:
                    for _, row in data.iterrows():
                        result.append(self._parse_deal_row(row))
            except Exception:
                pass  # Simulated trade does not support deal list

            # Try historical deals
            try:
                ret2, data2 = await asyncio.to_thread(
                    self._trade_ctx.history_deal_list_query, trd_env=env,
                )
                if ret2 == ft.RET_OK and not data2.empty:
                    existing_ids = {d["deal_id"] for d in result}
                    for _, row in data2.iterrows():
                        parsed = self._parse_deal_row(row)
                        if parsed["deal_id"] not in existing_ids:
                            result.append(parsed)
            except Exception:
                pass  # history_deal_list_query may not be available

            result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return result[:limit]
        except Exception as e:
            log.error("Moomoo get_deal_history failed", error=str(e))
            return []

    def _parse_order_row(self, row) -> dict:
        """Parse a Moomoo order row into a standardized dict."""
        trd_side_str = str(row.get("trd_side", "BUY")).upper()
        side = "BUY" if "BUY" in trd_side_str else "SELL"
        return {
            "order_id": str(row.get("order_id", "")),
            "ticker": _bare(str(row.get("code", ""))),
            "stock_name": str(row.get("stock_name", "")),
            "side": side,
            "qty": float(row.get("qty", 0) or 0),
            "filled_qty": float(row.get("dealt_qty", 0) or 0),
            "price": float(row.get("price", 0) or 0),
            "avg_fill_price": float(row.get("dealt_avg_price", 0) or 0),
            "status": self._map_status(str(row.get("order_status", ""))).value,
            "created_at": str(row.get("create_time", "")),
            "updated_at": str(row.get("updated_time", "")),
            "order_type": str(row.get("order_type", "")),
        }

    @staticmethod
    def _parse_deal_row(row) -> dict:
        """Parse a Moomoo deal row into a standardized dict."""
        trd_side_str = str(row.get("trd_side", "BUY")).upper()
        side = "BUY" if "BUY" in trd_side_str else "SELL"
        return {
            "deal_id": str(row.get("deal_id", "")),
            "ticker": _bare(str(row.get("code", ""))),
            "side": side,
            "qty": float(row.get("qty", 0) or 0),
            "price": float(row.get("price", 0) or 0),
            "created_at": str(row.get("create_time", "")),
        }

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
