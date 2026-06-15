"""MooMoo / Futu execution provider.

Connects to a local OpenD gateway (no API keys). Like the Tiger data provider,
the SDK is synchronous so every call is wrapped in ``asyncio.to_thread`` and the
contexts are built lazily under a lock. ``unlock_trade`` is called before any
order is placed. SIMULATE vs REAL is selected from settings.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from .base import (
    AccountInfo,
    BrokerProvider,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from ..config import MooMooTradeEnv, Settings, get_settings


def _us(symbol: str) -> str:
    """Normalize a bare US ticker to MooMoo's ``US.XXX`` form."""
    if "." in symbol:
        return symbol
    return f"US.{symbol}"


class MooMooBroker(BrokerProvider):
    """Concrete :class:`BrokerProvider` backed by ``moomoo-api``."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._trade_ctx: Any | None = None
        self._quote_ctx: Any | None = None
        self._init_lock = asyncio.Lock()
        self._unlocked = False

    # ---- lazy, lock-guarded context init --------------------------------
    async def _trade(self) -> Any:
        if self._trade_ctx is not None:
            return self._trade_ctx
        async with self._init_lock:
            if self._trade_ctx is None:
                self._trade_ctx = await asyncio.to_thread(self._build_trade_ctx)
                await self._ensure_unlocked()
        return self._trade_ctx

    def _build_trade_ctx(self) -> Any:
        from moomoo import OpenSecTradeContext, SecurityFirm, TrdMarket

        firm = getattr(SecurityFirm, self._settings.moomoo_security_firm, SecurityFirm.FUTUINC)
        return OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=self._settings.moomoo_host,
            port=self._settings.moomoo_port,
            security_firm=firm,
        )

    def _trd_env(self) -> Any:
        from moomoo import TrdEnv

        return (
            TrdEnv.REAL
            if self._settings.moomoo_trade_env == MooMooTradeEnv.REAL
            else TrdEnv.SIMULATE
        )

    async def _ensure_unlocked(self) -> None:
        """Unlock trading once. SIMULATE generally needs no password but we honor it."""
        if self._unlocked:
            return
        from moomoo import RET_OK

        ctx = self._trade_ctx
        pwd = self._settings.moomoo_unlock_trade or None
        ret, data = await asyncio.to_thread(ctx.unlock_trade, pwd)
        if ret != RET_OK:
            raise RuntimeError(f"MooMoo unlock_trade failed: {data}")
        self._unlocked = True

    # ---- BrokerProvider API ---------------------------------------------
    async def place_order(self, request: OrderRequest) -> Order:
        from moomoo import OrderType as MMOrderType, RET_OK, TrdSide

        ctx = await self._trade()
        side = TrdSide.BUY if request.side == OrderSide.BUY else TrdSide.SELL
        order_type = (
            MMOrderType.NORMAL
            if request.order_type == OrderType.LIMIT
            else MMOrderType.MARKET
        )
        price = request.limit_price or 0.0
        ret, data = await asyncio.to_thread(
            ctx.place_order,
            price,
            request.quantity,
            _us(request.symbol),
            side,
            order_type,
            trd_env=self._trd_env(),
        )
        if ret != RET_OK:
            return Order(
                order_id="",
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                status=OrderStatus.REJECTED,
                raw={"error": data},
            )
        row = data.iloc[0]
        return self._to_order(row, request)

    async def modify_order(
        self,
        order_id: str,
        *,
        quantity: Optional[int] = None,
        limit_price: Optional[float] = None,
    ) -> Order:
        from moomoo import ModifyOrderOp, RET_OK

        ctx = await self._trade()
        ret, data = await asyncio.to_thread(
            ctx.modify_order,
            ModifyOrderOp.NORMAL,
            order_id,
            quantity if quantity is not None else 0,
            limit_price if limit_price is not None else 0.0,
            trd_env=self._trd_env(),
        )
        if ret != RET_OK:
            raise RuntimeError(f"MooMoo modify_order failed: {data}")
        row = data.iloc[0]
        return Order(
            order_id=str(row.get("order_id", order_id)),
            symbol=str(row.get("code", "")),
            side=OrderSide.BUY,
            quantity=int(quantity or 0),
            status=OrderStatus.SUBMITTED,
            limit_price=limit_price,
            raw=row.to_dict(),
        )

    async def cancel_order(self, order_id: str) -> bool:
        from moomoo import ModifyOrderOp, RET_OK

        ctx = await self._trade()
        ret, data = await asyncio.to_thread(
            ctx.modify_order,
            ModifyOrderOp.CANCEL,
            order_id,
            0,
            0.0,
            trd_env=self._trd_env(),
        )
        return ret == RET_OK

    async def get_positions(self) -> list[Position]:
        from moomoo import RET_OK

        ctx = await self._trade()
        ret, data = await asyncio.to_thread(
            ctx.position_list_query, trd_env=self._trd_env()
        )
        if ret != RET_OK or data is None or data.empty:
            return []
        positions: list[Position] = []
        for _, row in data.iterrows():
            qty = int(row.get("qty", 0) or 0)
            direction = str(row.get("position_side", "LONG")).upper()
            signed = -qty if direction.startswith("SHORT") else qty
            positions.append(
                Position(
                    symbol=str(row.get("code", "")),
                    quantity=signed,
                    avg_price=float(row.get("cost_price", 0) or 0),
                    market_price=float(row.get("nominal_price", 0) or 0),
                    unrealized_pnl=float(row.get("unrealized_pl", 0) or 0),
                    raw=row.to_dict(),
                )
            )
        return positions

    async def get_account_info(self) -> AccountInfo:
        from moomoo import RET_OK

        ctx = await self._trade()
        ret, data = await asyncio.to_thread(
            ctx.accinfo_query, trd_env=self._trd_env()
        )
        if ret != RET_OK or data is None or data.empty:
            return AccountInfo(cash=0.0, buying_power=0.0, net_liquidation=0.0)
        row = data.iloc[0]
        return AccountInfo(
            cash=float(row.get("cash", 0) or 0),
            buying_power=float(row.get("power", 0) or 0),
            net_liquidation=float(row.get("total_assets", 0) or 0),
            realized_pnl=float(row.get("realized_pl", 0) or 0),
            unrealized_pnl=float(row.get("unrealized_pl", 0) or 0),
            raw=row.to_dict(),
        )

    async def close(self) -> None:
        for ctx in (self._trade_ctx, self._quote_ctx):
            if ctx is not None:
                await asyncio.to_thread(ctx.close)
        self._trade_ctx = None
        self._quote_ctx = None
        self._unlocked = False

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def _to_order(row: Any, request: OrderRequest) -> Order:
        return Order(
            order_id=str(row.get("order_id", "")),
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            status=OrderStatus.SUBMITTED,
            limit_price=request.limit_price,
            raw=row.to_dict() if hasattr(row, "to_dict") else {},
        )
