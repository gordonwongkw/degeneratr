"""Abstract market-data provider contract and the typed records it returns.

Every concrete provider (Tiger today, others later) subclasses
``MarketDataProvider``. All methods are async so callers never block the event
loop — sync SDK calls are pushed onto threads inside the implementations.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class BarPeriod(str, Enum):
    """Provider-agnostic bar granularity. Implementations map these to SDK enums."""

    ONE_MINUTE = "1m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    THIRTY_MINUTES = "30m"
    ONE_HOUR = "1h"
    ONE_DAY = "1d"


class OptionRight(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass(slots=True)
class Quote:
    """A live snapshot for a single symbol."""

    symbol: str
    last: float
    bid: float
    ask: float
    volume: int
    timestamp: datetime
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    prev_close: Optional[float] = None


@dataclass(slots=True)
class Bar:
    """A single OHLCV candle."""

    symbol: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(slots=True)
class OptionContract:
    """One row of an option chain."""

    symbol: str            # underlying
    identifier: str        # provider option identifier (used for option bars)
    expiry: str            # raw expiry value as returned by the provider — cached verbatim
    strike: float
    right: OptionRight
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: int = 0
    open_interest: int = 0
    implied_vol: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None


@dataclass(slots=True)
class IVAnalysis:
    """IV rank / percentile for an underlying, normalized to 0–100."""

    symbol: str
    iv: Optional[float] = None
    iv_rank: Optional[float] = None        # 0–100
    iv_percentile: Optional[float] = None  # 0–100


@dataclass(slots=True)
class ScanResult:
    """A candidate ticker surfaced by a universe scan."""

    symbol: str
    score: float = 0.0
    extras: dict = field(default_factory=dict)


class MarketDataProvider(ABC):
    """Read-only access to quotes, bars, option chains, IV stats and scans."""

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote:
        """Return a live quote snapshot for ``symbol``."""

    @abstractmethod
    async def get_bars(
        self,
        symbol: str,
        period: BarPeriod,
        begin_time: datetime,
        end_time: datetime,
    ) -> list[Bar]:
        """Return OHLCV bars for ``symbol`` between two datetimes."""

    @abstractmethod
    async def get_option_chain(
        self, symbol: str, expiry: Optional[str] = None
    ) -> list[OptionContract]:
        """Return the option chain for ``symbol``. ``expiry`` selects one expiry;
        when omitted the nearest available expiry is used."""

    @abstractmethod
    async def get_option_expirations(self, symbol: str) -> list[str]:
        """Return raw expiry values for ``symbol`` (cached verbatim)."""

    @abstractmethod
    async def get_iv_analysis(self, symbol: str) -> IVAnalysis:
        """Return IV rank / percentile for ``symbol`` normalized to 0–100."""

    @abstractmethod
    async def get_option_bars(
        self,
        identifiers: list[str],
        begin_time: datetime,
        end_time: datetime,
        period: BarPeriod = BarPeriod.ONE_MINUTE,
    ) -> dict[str, list[Bar]]:
        """Return historical OHLCV bars per option identifier (for backtesting)."""

    @abstractmethod
    async def scan_universe(
        self, filters: Optional[dict] = None, limit: int = 50
    ) -> list[ScanResult]:
        """Run a server-side scan and return candidate tickers."""

    async def close(self) -> None:
        """Release any held resources. Default is a no-op."""
        return None
