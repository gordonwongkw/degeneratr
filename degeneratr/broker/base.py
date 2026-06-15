"""Broker/execution provider contract and shared order/position records."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class OrderRequest:
    """A normalized order to send to a broker."""

    symbol: str                 # provider-normalized symbol (e.g. "US.SPY" handled inside impl)
    side: OrderSide
    quantity: int               # contracts
    order_type: OrderType = OrderType.LIMIT
    limit_price: Optional[float] = None
    # Optional bracket levels for risk exits.
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    client_tag: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class Order:
    """The broker's view of a submitted order."""

    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    status: OrderStatus
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    limit_price: Optional[float] = None
    created_at: datetime = field(default_factory=datetime.now)
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: int                # signed: negative = short
    avg_price: float
    market_price: float = 0.0
    unrealized_pnl: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class AccountInfo:
    cash: float
    buying_power: float
    net_liquidation: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    raw: dict = field(default_factory=dict)


class BrokerProvider(ABC):
    """Order placement and account/position queries."""

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order:
        """Submit an order and return its broker record."""

    @abstractmethod
    async def modify_order(
        self, order_id: str, *, quantity: Optional[int] = None, limit_price: Optional[float] = None
    ) -> Order:
        """Modify a working order's quantity and/or limit price."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a working order. Returns True on success."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return current open positions."""

    @abstractmethod
    async def get_account_info(self) -> AccountInfo:
        """Return cash / buying power / P&L for the trading account."""

    async def close(self) -> None:
        """Release resources. Default no-op."""
        return None
