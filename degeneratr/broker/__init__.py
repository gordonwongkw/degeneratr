"""Execution layer: broker ABC, MooMoo + paper implementations and factory."""
from __future__ import annotations

from typing import Optional

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
from .paper import PaperBroker
from ..config import Settings, get_settings


def get_broker(settings: Optional[Settings] = None) -> BrokerProvider:
    """Construct the configured broker provider."""
    settings = settings or get_settings()
    name = settings.broker_provider.strip().lower()
    if name == "paper":
        return PaperBroker(settings)
    if name == "moomoo":
        from .moomoo import MooMooBroker

        return MooMooBroker(settings)
    raise ValueError(f"Unknown broker provider: {settings.broker_provider!r}")


__all__ = [
    "AccountInfo",
    "BrokerProvider",
    "Order",
    "OrderRequest",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
    "PaperBroker",
    "get_broker",
]
