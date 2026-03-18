"""Broker layer — BaseBroker ABC + AlpacaBroker (default, free paper trading).

Other brokers can be added by subclassing BaseBroker and passing the instance
to NEXUSEngine(broker=your_broker).

Alpaca setup (free):
  1. Sign up at https://alpaca.markets
  2. Go to Paper Trading → API Keys
  3. Add to .env: ALPACA_API_KEY=... ALPACA_SECRET_KEY=...

v3 changes:
  - Position.side: "LONG" | "SHORT"
  - Position.unrealized_pnl: direction-aware P&L math
  - AlpacaBroker.open_short() / close_short() — sell short, cover short
  - get_positions() detects side from Alpaca qty sign
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from nexus.config import AlpacaConfig
from nexus.logger import get_logger

log = get_logger("broker")


# ── Shared data types ─────────────────────────────────────────────────────────


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class OptionsContract:
    """A single options contract."""

    ticker: str  # underlying: "AAPL"
    strike: float  # strike price
    expiration: str  # ISO date: "2025-03-21"
    right: str  # "CALL" | "PUT"
    code: str = ""  # broker-specific code (e.g. "US.AAPL250321C00150000")


@dataclass
class OptionsQuote:
    """Quote data for an options contract including Greeks."""

    contract: OptionsContract
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_vol: float
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class Quote:
    ticker: str
    bid: float
    ask: float
    last: float
    volume: int
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def change_pct(self) -> float:
        return (self.last - self.prev_close) / self.prev_close * 100 if self.prev_close else 0.0


@dataclass
class Position:
    ticker: str
    shares: float  # always positive — direction expressed via side
    avg_cost: float
    current_price: float
    broker: str
    side: str = "LONG"  # "LONG" | "SHORT"
    instrument_type: str = "EQUITY"  # "EQUITY" | "CALL" | "PUT"
    strike: float = 0.0
    expiration: str = ""
    option_code: str = ""
    contract_multiplier: int = 100

    @property
    def is_option(self) -> bool:
        return self.instrument_type in ("CALL", "PUT")

    @property
    def market_value(self) -> float:
        if self.is_option:
            return self.shares * self.current_price * self.contract_multiplier
        return self.shares * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        mult = self.contract_multiplier if self.is_option else 1
        if self.side == "SHORT":
            return (self.avg_cost - self.current_price) * self.shares * mult
        return (self.current_price - self.avg_cost) * self.shares * mult

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.avg_cost == 0:
            return 0.0
        if self.side == "SHORT":
            return (self.avg_cost - self.current_price) / self.avg_cost * 100
        return (self.current_price - self.avg_cost) / self.avg_cost * 100


@dataclass
class OrderResult:
    order_id: str
    ticker: str
    side: OrderSide
    requested_qty: float
    filled_qty: float
    avg_fill_price: float
    status: OrderStatus
    broker: str
    message: str = ""


@dataclass
class AccountInfo:
    broker: str
    cash: float
    portfolio_value: float
    buying_power: float
    day_pnl: float
    total_pnl: float
    paper: bool


# ── Abstract base ─────────────────────────────────────────────────────────────


class BaseBroker(ABC):
    name: str = "base"
    paper: bool = True

    @abstractmethod
    async def connect(self) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    async def get_quote(self, ticker: str) -> Optional[Quote]: ...

    @abstractmethod
    async def get_batch_quotes(self, tickers: List[str]) -> Dict[str, Quote]: ...

    @abstractmethod
    async def get_positions(self) -> List[Position]: ...

    @abstractmethod
    async def get_account_info(self) -> AccountInfo: ...

    @abstractmethod
    async def place_order(
        self,
        ticker: str,
        side: OrderSide,
        qty: float,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: Optional[float] = None,
    ) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderResult: ...

    async def open_short(
        self, ticker: str, shares: float, limit_price: Optional[float] = None
    ) -> OrderResult:
        """Sell short (unowned stock). Default: delegates to place_order(SELL)."""
        return await self.place_order(
            ticker,
            OrderSide.SELL,
            shares,
            OrderType.LIMIT if limit_price else OrderType.MARKET,
            limit_price,
        )

    async def close_short(
        self, ticker: str, shares: float, limit_price: Optional[float] = None
    ) -> OrderResult:
        """Buy to cover a short position."""
        return await self.place_order(
            ticker,
            OrderSide.BUY,
            shares,
            OrderType.LIMIT if limit_price else OrderType.MARKET,
            limit_price,
        )

    async def is_market_open(self) -> bool:
        return True

    # ── Options (default no-ops — override in brokers that support options) ───

    async def get_option_expirations(self, ticker: str) -> List[str]:
        """Return available expiration dates (ISO format) for the underlying."""
        return []

    async def get_option_chain(
        self, ticker: str, expiration: str
    ) -> List[OptionsQuote]:
        """Return option quotes for all strikes at a given expiration."""
        return []

    async def place_options_order(
        self,
        contract: OptionsContract,
        side: OrderSide,
        qty: int,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """Place an options order. Override in brokers that support options."""
        return OrderResult(
            "", contract.ticker, side, qty, 0, 0,
            OrderStatus.REJECTED, self.name, f"{self.name} does not support options",
        )


# ── Alpaca ────────────────────────────────────────────────────────────────────


async def _run(fn, *args, **kwargs):
    """Wrap synchronous alpaca-py calls for async execution."""
    return await asyncio.to_thread(fn, *args, **kwargs)


class AlpacaBroker(BaseBroker):
    """Alpaca Markets broker — free paper trading, official alpaca-py SDK.

    Paper trading: https://paper-api.alpaca.markets (no real money)
    Sign up free:  https://alpaca.markets

    Short selling: Alpaca automatically treats SELL orders on unowned stock
    as short sales. Covering is a BUY order on the shorted symbol.
    """

    name = "alpaca"

    def __init__(self, config: AlpacaConfig) -> None:
        self._cfg = config
        self.paper = config.paper
        self._client = None
        self._data_client = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient

            self._client = TradingClient(
                api_key=self._cfg.api_key,
                secret_key=self._cfg.secret_key,
                paper=self._cfg.paper,
            )
            self._data_client = StockHistoricalDataClient(
                api_key=self._cfg.api_key,
                secret_key=self._cfg.secret_key,
            )
            await _run(self._client.get_account)
            self._connected = True
            log.info("Alpaca connected", paper=self.paper)
            return True
        except ImportError:
            log.warning("alpaca-py not installed — run: pip install alpaca-py")
            return False
        except Exception as e:
            log.error("Alpaca connect failed", error=str(e))
            return False

    async def disconnect(self) -> None:
        self._connected = False
        log.info("Alpaca disconnected")

    async def is_market_open(self) -> bool:
        try:
            clock = await _run(self._client.get_clock)
            return clock.is_open
        except Exception:
            return False

    async def get_quote(self, ticker: str) -> Optional[Quote]:
        if not self.is_connected:
            return None
        try:
            from alpaca.data.requests import StockLatestQuoteRequest

            req = StockLatestQuoteRequest(symbol_or_symbols=[ticker])
            quotes = await _run(self._data_client.get_stock_latest_quote, req)
            q = quotes.get(ticker)
            if not q:
                return None
            mid = (q.bid_price + q.ask_price) / 2
            return Quote(
                ticker=ticker,
                bid=float(q.bid_price or mid),
                ask=float(q.ask_price or mid),
                last=float(q.ask_price or mid),
                volume=int(q.bid_size + q.ask_size) if q.bid_size else 0,
            )
        except Exception as e:
            log.error("Alpaca get_quote failed", ticker=ticker, error=str(e))
            return None

    async def get_batch_quotes(self, tickers: List[str]) -> Dict[str, Quote]:
        if not self.is_connected or not tickers:
            return {}
        try:
            from alpaca.data.requests import StockLatestQuoteRequest

            req = StockLatestQuoteRequest(symbol_or_symbols=tickers)
            raw = await _run(self._data_client.get_stock_latest_quote, req)
            result = {}
            for ticker, q in raw.items():
                mid = (q.bid_price + q.ask_price) / 2
                result[ticker] = Quote(
                    ticker=ticker,
                    bid=float(q.bid_price or mid),
                    ask=float(q.ask_price or mid),
                    last=float(q.ask_price or mid),
                    volume=0,
                )
            return result
        except Exception as e:
            log.error("Alpaca batch quotes failed", error=str(e))
            return {}

    async def get_positions(self) -> List[Position]:
        """Fetch all positions. Alpaca uses negative qty for shorts."""
        if not self.is_connected:
            return []
        try:
            positions = await _run(self._client.get_all_positions)
            result = []
            for p in positions:
                qty = float(p.qty)
                side = "SHORT" if qty < 0 else "LONG"
                shares = abs(qty)
                result.append(
                    Position(
                        ticker=p.symbol,
                        shares=shares,
                        avg_cost=float(p.avg_entry_price),
                        current_price=float(p.current_price),
                        broker=self.name,
                        side=side,
                    )
                )
            return result
        except Exception as e:
            log.error("Alpaca get_positions failed", error=str(e))
            return []

    async def get_account_info(self) -> AccountInfo:
        try:
            acct = await _run(self._client.get_account)
            return AccountInfo(
                broker=self.name,
                cash=float(acct.cash),
                portfolio_value=float(acct.portfolio_value),
                buying_power=float(acct.buying_power),
                day_pnl=float(acct.unrealized_intraday_pl or 0),
                total_pnl=float(acct.unrealized_pl or 0),
                paper=self.paper,
            )
        except Exception as e:
            log.error("Alpaca account info failed", error=str(e))
            return AccountInfo(self.name, 0, 0, 0, 0, 0, self.paper)

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
            from alpaca.trading.enums import OrderSide as ASide
            from alpaca.trading.enums import TimeInForce
            from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

            alpaca_side = ASide.BUY if side == OrderSide.BUY else ASide.SELL

            if order_type == OrderType.LIMIT and limit_price:
                req = LimitOrderRequest(
                    symbol=ticker,
                    qty=int(qty),
                    side=alpaca_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(limit_price, 2),
                )
            else:
                req = MarketOrderRequest(
                    symbol=ticker,
                    qty=int(qty),
                    side=alpaca_side,
                    time_in_force=TimeInForce.DAY,
                )

            order = await _run(self._client.submit_order, req)
            _status_map = {
                "new": OrderStatus.SUBMITTED,
                "partially_filled": OrderStatus.PARTIAL,
                "filled": OrderStatus.FILLED,
                "canceled": OrderStatus.CANCELLED,
                "rejected": OrderStatus.REJECTED,
            }
            return OrderResult(
                order_id=str(order.id),
                ticker=ticker,
                side=side,
                requested_qty=qty,
                filled_qty=float(order.filled_qty or 0),
                avg_fill_price=float(order.filled_avg_price or limit_price or 0),
                status=_status_map.get(str(order.status), OrderStatus.SUBMITTED),
                broker=self.name,
            )
        except Exception as e:
            log.error("Alpaca place_order failed", ticker=ticker, error=str(e))
            return OrderResult("", ticker, side, qty, 0, 0, OrderStatus.REJECTED, self.name, str(e))

    async def open_short(
        self, ticker: str, shares: float, limit_price: Optional[float] = None
    ) -> OrderResult:
        """Short sell: SELL unowned stock. Alpaca handles this automatically."""
        log.info(
            "Opening short",
            ticker=ticker,
            shares=shares,
            limit=f"${limit_price:.2f}" if limit_price else "market",
        )
        return await self.place_order(
            ticker,
            OrderSide.SELL,
            shares,
            OrderType.LIMIT if limit_price else OrderType.MARKET,
            limit_price,
        )

    async def close_short(
        self, ticker: str, shares: float, limit_price: Optional[float] = None
    ) -> OrderResult:
        """Buy to cover a short position."""
        log.info(
            "Covering short",
            ticker=ticker,
            shares=shares,
            limit=f"${limit_price:.2f}" if limit_price else "market",
        )
        return await self.place_order(
            ticker,
            OrderSide.BUY,
            shares,
            OrderType.LIMIT if limit_price else OrderType.MARKET,
            limit_price,
        )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            import uuid as _uuid

            await _run(self._client.cancel_order_by_id, _uuid.UUID(order_id))
            return True
        except Exception as e:
            log.error("Alpaca cancel_order failed", error=str(e))
            return False

    async def get_order_status(self, order_id: str) -> OrderResult:
        try:
            import uuid as _uuid

            order = await _run(self._client.get_order_by_id, _uuid.UUID(order_id))
            _status_map = {
                "new": OrderStatus.SUBMITTED,
                "partially_filled": OrderStatus.PARTIAL,
                "filled": OrderStatus.FILLED,
                "canceled": OrderStatus.CANCELLED,
                "rejected": OrderStatus.REJECTED,
            }
            return OrderResult(
                order_id=order_id,
                ticker=order.symbol,
                side=OrderSide.BUY if str(order.side) == "buy" else OrderSide.SELL,
                requested_qty=float(order.qty or 0),
                filled_qty=float(order.filled_qty or 0),
                avg_fill_price=float(order.filled_avg_price or 0),
                status=_status_map.get(str(order.status), OrderStatus.SUBMITTED),
                broker=self.name,
            )
        except Exception as e:
            log.error("Alpaca get_order_status failed", error=str(e))
            return OrderResult(
                order_id, "", OrderSide.BUY, 0, 0, 0, OrderStatus.REJECTED, self.name
            )
