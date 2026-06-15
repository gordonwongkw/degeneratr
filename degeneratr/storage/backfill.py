"""Capture current Tiger data into the local store.

Run this regularly (e.g. daily after the close) so the archive grows: each run
appends the latest ~3-day window of underlying bars plus option bars for the
near-the-money strikes of the front expiries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from ..config import get_settings
from ..data.base import BarPeriod, MarketDataProvider, OptionRight
from ..data.factory import get_provider
from .store import BarStore

logger = logging.getLogger("degeneratr.backfill")


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def backfill(
    symbols: list[str],
    *,
    period: BarPeriod = BarPeriod.FIVE_MINUTES,
    days: int = 5,
    strike_band: int = 12,
    expiries: int = 1,
    chunk: int = 25,
    store: Optional[BarStore] = None,
    provider: Optional[MarketDataProvider] = None,
) -> dict:
    """Pull and persist underlying + near-money option bars for ``symbols``.

    ``strike_band`` = number of strikes per side (calls/puts) nearest spot.
    ``expiries`` = how many front expiries to capture.
    """
    provider = provider or get_provider()
    store = store or BarStore(get_settings().bar_store_path)
    end = datetime.now()
    begin = end - timedelta(days=days)
    saved = {"underlying": 0, "option_bars": 0, "contracts": 0}

    for symbol in symbols:
        ubars = await provider.get_bars(symbol, period, begin, end)
        saved["underlying"] += store.save_underlying(symbol, period.value, ubars)
        if not ubars:
            logger.warning("no underlying bars for %s", symbol)
            continue
        spot = ubars[-1].close

        exps = await provider.get_option_expirations(symbol)
        identifiers: list[str] = []
        for expiry in exps[:expiries]:
            chain = await provider.get_option_chain(symbol, expiry)
            calls = sorted(
                [c for c in chain if c.right == OptionRight.CALL and c.strike > 0],
                key=lambda c: abs(c.strike - spot),
            )[:strike_band]
            puts = sorted(
                [c for c in chain if c.right == OptionRight.PUT and c.strike > 0],
                key=lambda c: abs(c.strike - spot),
            )[:strike_band]
            identifiers.extend(c.identifier for c in calls + puts if c.identifier)

        saved["contracts"] += len(identifiers)
        # Batch identifiers per option_bars call to limit rate-limit pressure.
        for batch in _chunks(identifiers, chunk):
            obars = await provider.get_option_bars(batch, begin, end, period)
            for ident, bars in obars.items():
                saved["option_bars"] += store.save_option(ident, period.value, bars)
        logger.info("backfilled %s: %d contracts", symbol, len(identifiers))

    return {"saved": saved, "coverage": store.coverage()}
