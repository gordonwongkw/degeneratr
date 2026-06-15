"""Ingest underlying bars from yfinance into the local store.

yfinance keeps ~60 days of 5m/15m history, so a single ingest seeds the archive
with far more than Tiger's rolling window — and once stored, backtests and the
dashboard read it offline without re-fetching. Re-running is safe (the store
upserts on key + timestamp), so the archive only grows.
"""
from __future__ import annotations

import logging

from ..config import get_settings
from ..data.base import BarPeriod
from ..data.yfinance_provider import YFinanceDataProvider
from .store import BarStore

logger = logging.getLogger("degeneratr.ingest")


async def ingest_yfinance(
    symbols: list[str],
    periods: list[BarPeriod],
    *,
    store: BarStore | None = None,
) -> dict:
    """Pull each symbol's full retained window per period and persist it."""
    store = store or BarStore(get_settings().bar_store_path)
    provider = YFinanceDataProvider()
    from datetime import datetime

    lo, hi = datetime.min, datetime.max
    saved: dict[str, int] = {}
    for period in periods:
        total = 0
        for symbol in symbols:
            bars = await provider.get_bars(symbol, period, lo, hi)
            n = store.save_underlying(symbol, period.value, bars)
            total += n
            logger.info("ingested %s %s: %d bars", symbol, period.value, n)
        saved[period.value] = total
    return {"saved": saved, "coverage": store.coverage()}
