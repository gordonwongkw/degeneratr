"""PaperBroker — fully local order simulation with no external calls.

Fills limit orders immediately at their limit price (or a supplied mark),
tracks positions and cash, and applies the configured per-contract commission.
Useful for dry-running the engine and for unit tests.
"""
from __future__ import annotations

import asyncio
import itertools
from typing import Optional

from .base import (
    AccountInfo,
    BrokerProvider,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Position,
)
from ..config import Settings, get_settings

_CONTRACT_MULTIPLIER = 100  # standard US equity option


class PaperBroker(BrokerProvider):
    """In-memory broker simulation."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._cash = float(self._settings.backtest_starting_cash)
        self._starting_cash = self._cash
        self._commission = float(self._settings.commission_per_contract)
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._ids = itertools.count(1)
        self._realized_pnl = 0.0
        self._lock = asyncio.Lock()

    async def place_order(self, request: OrderRequest) -> Order:
        async with self._lock:
            return self._fill(request)

    def _fill(self, request: OrderRequest) -> Order:
        order_id = f"paper-{next(self._ids)}"
        price = request.limit_price or request.meta.get("mark") or 0.0
        signed_qty = request.quantity if request.side == OrderSide.BUY else -request.quantity
        notional = price * abs(request.quantity) * _CONTRACT_MULTIPLIER
        commission = self._commission * abs(request.quantity)

        # Cash impact: pay for buys, receive for sells; commission always debited.
        self._cash -= signed_qty * price * _CONTRACT_MULTIPLIER
        self._cash -= commission

        self._apply_position(request.symbol, signed_qty, price)

        order = Order(
            order_id=order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            status=OrderStatus.FILLED,
            filled_quantity=request.quantity,
            avg_fill_price=price,
            limit_price=request.limit_price,
            raw={"commission": commission, "notional": notional},
        )
        self._orders[order_id] = order
        return order

    def _apply_position(self, symbol: str, signed_qty: int, price: float) -> None:
        pos = self._positions.get(symbol)
        if pos is None:
            self._positions[symbol] = Position(
                symbol=symbol, quantity=signed_qty, avg_price=price, market_price=price
            )
            return

        new_qty = pos.quantity + signed_qty
        # Closing or reducing: realize P&L on the closed portion.
        if pos.quantity != 0 and (pos.quantity > 0) != (signed_qty > 0):
            closing = min(abs(signed_qty), abs(pos.quantity))
            direction = 1 if pos.quantity > 0 else -1
            self._realized_pnl += direction * (price - pos.avg_price) * closing * _CONTRACT_MULTIPLIER

        if new_qty == 0:
            del self._positions[symbol]
            return
        # Average in only when adding to the same direction.
        if (pos.quantity > 0) == (signed_qty > 0):
            total = pos.avg_price * abs(pos.quantity) + price * abs(signed_qty)
            pos.avg_price = total / abs(new_qty)
        pos.quantity = new_qty
        pos.market_price = price

    async def modify_order(
        self,
        order_id: str,
        *,
        quantity: Optional[int] = None,
        limit_price: Optional[float] = None,
    ) -> Order:
        # Paper fills are immediate, so there is nothing working to modify.
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"Unknown order: {order_id}")
        return order

    async def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None or order.status != OrderStatus.SUBMITTED:
            return False
        order.status = OrderStatus.CANCELLED
        return True

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_account_info(self) -> AccountInfo:
        unrealized = sum(
            (p.market_price - p.avg_price) * p.quantity * _CONTRACT_MULTIPLIER
            for p in self._positions.values()
        )
        net_liq = self._cash + unrealized
        return AccountInfo(
            cash=self._cash,
            buying_power=max(self._cash, 0.0),
            net_liquidation=net_liq,
            realized_pnl=self._realized_pnl,
            unrealized_pnl=unrealized,
        )

    # ---- simulation helpers ---------------------------------------------
    def mark_to_market(self, prices: dict[str, float]) -> None:
        """Update market prices and unrealized P&L for held positions."""
        for symbol, price in prices.items():
            pos = self._positions.get(symbol)
            if pos is not None:
                pos.market_price = price
                pos.unrealized_pnl = (
                    (price - pos.avg_price) * pos.quantity * _CONTRACT_MULTIPLIER
                )
