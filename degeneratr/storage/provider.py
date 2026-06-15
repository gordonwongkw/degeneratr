"""A MarketDataProvider that serves entirely from the local BarStore.

Lets backtests run offline against the accumulated archive (no Tiger calls, no
rate limits, deeper-than-3-day history once data builds up). The option "chain"
is reconstructed from the option identifiers present in the store.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
from typing import Optional

from ..data.base import (
    Bar,
    BarPeriod,
    IVAnalysis,
    MarketDataProvider,
    OptionContract,
    OptionRight,
    Quote,
    ScanResult,
)
from .store import BarStore


def parse_option_identifier(identifier: str) -> Optional[OptionContract]:
    """Parse an OCC-style Tiger identifier, e.g. ``SPY   260615P00725000``."""
    try:
        root = identifier[0:6].strip()
        yy, mm, dd = identifier[6:8], identifier[8:10], identifier[10:12]
        cp = identifier[12]
        strike = int(identifier[13:21]) / 1000.0
    except (IndexError, ValueError):
        return None
    return OptionContract(
        symbol=root,
        identifier=identifier,
        expiry=f"20{yy}-{mm}-{dd}",
        strike=strike,
        right=OptionRight.CALL if cp.upper() == "C" else OptionRight.PUT,
    )


class StoreProvider(MarketDataProvider):
    def __init__(self, store: BarStore) -> None:
        self._store = store

    async def get_bars(
        self, symbol: str, period: BarPeriod, begin_time: datetime, end_time: datetime
    ) -> list[Bar]:
        return await asyncio.to_thread(
            self._store.load_underlying, symbol, period.value, begin_time, end_time
        )

    async def get_option_bars(
        self,
        identifiers: list[str],
        begin_time: datetime,
        end_time: datetime,
        period: BarPeriod = BarPeriod.ONE_MINUTE,
    ) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        for ident in identifiers:
            out[ident] = await asyncio.to_thread(
                self._store.load_option, ident, period.value, begin_time, end_time
            )
        return out

    async def _contracts_for(self, symbol: str) -> list[OptionContract]:
        idents = await asyncio.to_thread(self._store.option_identifiers)
        contracts = [parse_option_identifier(i) for i in idents]
        return [c for c in contracts if c is not None and c.symbol == symbol]

    async def get_option_expirations(self, symbol: str) -> list[str]:
        contracts = await self._contracts_for(symbol)
        return sorted({c.expiry for c in contracts})

    async def get_option_chain(
        self, symbol: str, expiry: Optional[str] = None
    ) -> list[OptionContract]:
        contracts = await self._contracts_for(symbol)
        if not contracts:
            return []
        if expiry is None:
            # Default to the expiry with the most stored contracts.
            expiry = Counter(c.expiry for c in contracts).most_common(1)[0][0]
        return [c for c in contracts if c.expiry == expiry]

    async def get_iv_analysis(self, symbol: str) -> IVAnalysis:
        # Price-action strategy ignores IV; return an empty analysis.
        return IVAnalysis(symbol=symbol)

    async def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last=0.0, bid=0.0, ask=0.0, volume=0, timestamp=datetime.now())

    async def scan_universe(
        self, filters: Optional[dict] = None, limit: int = 50
    ) -> list[ScanResult]:
        symbols = await asyncio.to_thread(self._store.underlying_symbols)
        return [ScanResult(symbol=s) for s in symbols[:limit]]
