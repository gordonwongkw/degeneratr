"""Market-data provider factory — selects the concrete provider via settings."""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from .base import MarketDataProvider
from ..config import Settings, get_settings


def _build_provider(settings: Settings) -> MarketDataProvider:
    name = settings.market_data_provider.strip().lower()
    if name == "tiger":
        from .tiger import TigerDataProvider

        return TigerDataProvider(settings)
    raise ValueError(f"Unknown market data provider: {settings.market_data_provider!r}")


@lru_cache(maxsize=1)
def _cached_provider() -> MarketDataProvider:
    return _build_provider(get_settings())


def get_provider(settings: Optional[Settings] = None) -> MarketDataProvider:
    """Return a market-data provider.

    With no argument a process-wide cached instance is returned. Pass explicit
    ``settings`` (e.g. in tests) to build a fresh, uncached provider.
    """
    if settings is not None:
        return _build_provider(settings)
    return _cached_provider()
