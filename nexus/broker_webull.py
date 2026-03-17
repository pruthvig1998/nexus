"""Webull broker adapter — via unofficial webull Python library.

⚠️  WARNING: UNOFFICIAL API
────────────────────────────
This adapter uses a reverse-engineered, community-maintained Python library
(`webull`) that is NOT affiliated with or endorsed by Webull Financial LLC.
Use at your own risk. The API may break without notice when Webull updates
their platform. For production trading consider a supported broker like
Alpaca or IBKR.

Recommended use: paper trading only.

Setup (one-time)
────────────────
1. pip install webull
2. Create a Webull account at webull.com and enable paper trading
3. Add to .env:
   WEBULL_EMAIL=your@email.com
   WEBULL_PASSWORD=your_password
   WEBULL_DEVICE_ID=            # auto-generated on first login, store for reuse
   WEBULL_TRADING_PIN=123456    # 6-digit PIN set in Webull app
   WEBULL_PAPER=true            # set false for live (not recommended)

Two-factor authentication
─────────────────────────
Webull requires a 2FA code (sent to email/phone) on first login.
NEXUS will prompt for the code interactively during `connect()`.
Subsequent connects reuse the stored `device_id` to skip 2FA.

Short selling
─────────────
Webull supports short selling on margin accounts. A SELL on an unowned
stock places a short order. Paper trading account supports shorts.
"""

from __future__ import annotations

import asyncio
import os
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

log = get_logger("broker.webull")

_PAPER_ACCOUNT_TYPE = "paper"
_LIVE_ACCOUNT_TYPE = "live"


class WebullBroker(BaseBroker):
    """Webull broker via unofficial webull Python library.

    Usage:
        from nexus.broker_webull import WebullBroker
        broker = WebullBroker(paper=True)
        engine = NEXUSEngine(broker=broker)
        await engine.start()

    Note:
        On first run, connect() will prompt for a 2FA code sent to your
        registered email/phone. Subsequent runs reuse device_id.
    """

    name = "webull"

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        trading_pin: Optional[str] = None,
        device_id: Optional[str] = None,
        paper: bool = True,
    ) -> None:
        self.email = email or os.getenv("WEBULL_EMAIL", "")
        self.password = password or os.getenv("WEBULL_PASSWORD", "")
        self.trading_pin = trading_pin or os.getenv("WEBULL_TRADING_PIN", "")
        self.device_id = device_id or os.getenv("WEBULL_DEVICE_ID", "")
        self.paper = paper
        self._wb = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._wb is not None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            from webull import paper_webull, webull

            self._wb = paper_webull() if self.paper else webull()

            # Restore device_id to skip 2FA on repeated connects
            if self.device_id:
                self._wb.device_id = self.device_id

            # Login
            result = self._wb.login(
                username=self.email,
                password=self.password,
                device_name="NEXUS",
                mfa="",  # filled below if required
                save_token=True,
            )

            # Handle 2FA challenge
            if isinstance(result, dict) and result.get("success") is False:
                mfa_code = input("[NEXUS] Webull 2FA code (check email/SMS): ").strip()
                result = self._wb.login(
                    username=self.email,
                    password=self.password,
                    device_name="NEXUS",
                    mfa=mfa_code,
                    save_token=True,
                )

            if not result or (isinstance(result, dict) and not result.get("accessToken")):
                log.error("Webull login failed", hint="Check email/password in .env")
                return False

            # Store device_id for future logins (prevents 2FA repeat)
            if hasattr(self._wb, "device_id") and self._wb.device_id:
                self.device_id = self._wb.device_id
                log.info("Webull device_id stored — add to .env to skip 2FA")

            self._connected = True
            mode = "paper" if self.paper else "live"
            log.info("Webull connected", mode=mode)
            return True

        except ImportError:
            log.warning("webull library not installed", hint="pip install webull")
            return False
        except Exception as e:
            log.error("Webull connect failed", error=str(e))
            return False

    async def disconnect(self) -> None:
        if self._wb:
            try:
                self._wb.logout()
            except Exception:
                pass
        self._connected = False
        log.info("Webull disconnected")

    # ── Market hours ───────────────────────────────────────────────────────────

    async def is_market_open(self) -> bool:
        try:
            import datetime
            import zoneinfo

            et = zoneinfo.ZoneInfo("America/New_York")
            now = datetime.datetime.now(tz=et)
            if now.weekday() >= 5:
                return False
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
            return market_open <= now <= market_close
        except Exception:
            return True

    # ── Quotes ─────────────────────────────────────────────────────────────────

    async def get_quote(self, ticker: str) -> Optional[Quote]:
        if not self.is_connected:
            return None
        try:
            data = await asyncio.to_thread(self._wb.get_quote, stock=ticker)
            if not data:
                return None
            bid = float(data.get("bidList", [{}])[0].get("price", 0) if data.get("bidList") else 0)
            ask = float(data.get("askList", [{}])[0].get("price", 0) if data.get("askList") else 0)
            last = float(data.get("close", 0) or 0)
            if last == 0:
                last = (bid + ask) / 2
            return Quote(
                ticker=ticker,
                bid=bid,
                ask=ask,
                last=last,
                volume=int(data.get("volume", 0) or 0),
                open=float(data.get("open", 0) or 0),
                high=float(data.get("high", 0) or 0),
                low=float(data.get("low", 0) or 0),
                prev_close=float(data.get("pPrice", 0) or 0),
            )
        except Exception as e:
            log.error("Webull get_quote failed", ticker=ticker, error=str(e))
            return None

    async def get_batch_quotes(self, tickers: List[str]) -> Dict[str, Quote]:
        results: Dict[str, Quote] = {}
        # Webull doesn't have a true batch quote endpoint in the library;
        # run individually with async gather for modest concurrency.
        tasks = [self.get_quote(t) for t in tickers]
        quotes = await asyncio.gather(*tasks, return_exceptions=True)
        for ticker, q in zip(tickers, quotes):
            if isinstance(q, Quote):
                results[ticker] = q
        return results

    # ── Account & positions ────────────────────────────────────────────────────

    async def get_positions(self) -> List[Position]:
        if not self.is_connected:
            return []
        try:
            raw = await asyncio.to_thread(self._wb.get_positions)
            result = []
            for p in raw or []:
                qty = float(p.get("position", 0) or 0)
                if qty == 0:
                    continue
                side = "SHORT" if qty < 0 else "LONG"
                result.append(
                    Position(
                        ticker=str(p.get("ticker", {}).get("symbol", "") or ""),
                        shares=abs(qty),
                        avg_cost=float(p.get("costPrice", 0) or 0),
                        current_price=float(p.get("lastPrice", 0) or 0),
                        broker=self.name,
                        side=side,
                    )
                )
            return result
        except Exception as e:
            log.error("Webull get_positions failed", error=str(e))
            return []

    async def get_account_info(self) -> AccountInfo:
        try:
            data = await asyncio.to_thread(self._wb.get_account)
            if not data:
                raise ValueError("Empty account response")
            net_liq = float(data.get("netLiquidation", 0) or 0)
            cash = float(data.get("cashBalance", 0) or 0)
            buy_pwr = float(data.get("buyingPower", 0) or 0)
            day_pnl = float(data.get("dayProfitLoss", 0) or 0)
            total_pnl = float(data.get("unrealizedProfitLoss", 0) or 0)
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
            log.error("Webull account info failed", error=str(e))
            return AccountInfo(self.name, 0, 0, 0, 0, 0, self.paper)

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: OrderSide,
        qty: float,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        if not self.is_connected:
            return OrderResult(
                "", ticker, side, qty, 0, 0, OrderStatus.REJECTED, self.name, "Not connected"
            )
        try:
            action = "BUY" if side == OrderSide.BUY else "SELL"

            if order_type == OrderType.MARKET:
                result = await asyncio.to_thread(
                    self._wb.place_order,
                    stock=ticker,
                    action=action,
                    orderType="MKT",
                    quant=int(qty),
                )
            else:
                result = await asyncio.to_thread(
                    self._wb.place_order,
                    stock=ticker,
                    action=action,
                    orderType="LMT",
                    price=round(limit_price or 0.0, 2),
                    quant=int(qty),
                )

            if not result or not isinstance(result, dict):
                return OrderResult(
                    "", ticker, side, qty, 0, 0, OrderStatus.REJECTED, self.name, str(result)
                )

            order_id = str(result.get("orderId", ""))
            return OrderResult(
                order_id=order_id,
                ticker=ticker,
                side=side,
                requested_qty=qty,
                filled_qty=0.0,
                avg_fill_price=limit_price or 0.0,
                status=OrderStatus.SUBMITTED,
                broker=self.name,
            )
        except Exception as e:
            log.error("Webull place_order failed", ticker=ticker, error=str(e))
            return OrderResult("", ticker, side, qty, 0, 0, OrderStatus.REJECTED, self.name, str(e))

    async def cancel_order(self, order_id: str) -> bool:
        try:
            result = await asyncio.to_thread(self._wb.cancel_order, order_id=order_id)
            return bool(result)
        except Exception as e:
            log.error("Webull cancel_order failed", order_id=order_id, error=str(e))
            return False

    async def get_order_status(self, order_id: str) -> OrderResult:
        try:
            orders = await asyncio.to_thread(self._wb.get_history_orders) or []
            for o in orders:
                if str(o.get("orderId", "")) == order_id:
                    status_str = str(o.get("statusStr", "")).upper()
                    status = self._map_status(status_str)
                    action = str(o.get("action", "BUY")).upper()
                    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL
                    ticker = str(o.get("ticker", {}).get("symbol", "") or "")
                    return OrderResult(
                        order_id=order_id,
                        ticker=ticker,
                        side=side,
                        requested_qty=float(o.get("totalQuantity", 0) or 0),
                        filled_qty=float(o.get("filledQuantity", 0) or 0),
                        avg_fill_price=float(o.get("avgFilledPrice", 0) or 0),
                        status=status,
                        broker=self.name,
                    )
            return OrderResult(
                order_id, "", OrderSide.BUY, 0, 0, 0, OrderStatus.CANCELLED, self.name
            )
        except Exception as e:
            log.error("Webull get_order_status failed", error=str(e))
            return OrderResult(
                order_id, "", OrderSide.BUY, 0, 0, 0, OrderStatus.REJECTED, self.name
            )

    @staticmethod
    def _map_status(webull_status: str) -> OrderStatus:
        if "FILLED" in webull_status:
            return OrderStatus.FILLED
        if "PARTIAL" in webull_status:
            return OrderStatus.PARTIAL
        if "PENDING" in webull_status or "WORKING" in webull_status:
            return OrderStatus.SUBMITTED
        if (
            "CANCELLED" in webull_status
            or "REJECTED" in webull_status
            or "EXPIRED" in webull_status
        ):
            return OrderStatus.CANCELLED
        return OrderStatus.PENDING
