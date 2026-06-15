"""Market-data provider backed by yfinance (Yahoo Finance).

Why this exists: yfinance retains far more *intraday* history than Tiger —
60 days of 5m/15m bars and ~7 days of 1m vs Tiger's ~1 month / few days — and
needs no credentials. Since degeneratr's strategy and backtester are
underlying-only (price action → P&L from the stock move), yfinance supplies
everything the backtest needs. Options methods return empty: this provider is
for market *data* only; live options execution is the broker's job.

Selected via ``MARKET_DATA_PROVIDER=yfinance``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from ..config import Settings, get_settings
from .base import (
    Bar,
    BarPeriod,
    IVAnalysis,
    MarketDataProvider,
    OptionContract,
    Quote,
    ScanResult,
)

# Max yfinance `period` string per interval (Yahoo's intraday retention limits).
_MAX_PERIOD = {
    BarPeriod.ONE_MINUTE: "7d",
    BarPeriod.FIVE_MINUTES: "60d",
    BarPeriod.FIFTEEN_MINUTES: "60d",
    BarPeriod.THIRTY_MINUTES: "60d",
    BarPeriod.ONE_HOUR: "730d",
    BarPeriod.ONE_DAY: "max",
}


class YFinanceDataProvider(MarketDataProvider):
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._cache: dict[tuple[str, str], list[Bar]] = {}

    def _fetch(self, symbol: str, period: BarPeriod) -> list[Bar]:
        import yfinance as yf

        df = yf.Ticker(symbol).history(
            period=_MAX_PERIOD.get(period, "60d"), interval=period.value, auto_adjust=True
        )
        bars: list[Bar] = []
        for ts, row in df.iterrows():
            t = ts.tz_localize(None) if getattr(ts, "tzinfo", None) else ts
            bars.append(Bar(
                symbol=symbol, time=t.to_pydatetime(),
                open=float(row["Open"]), high=float(row["High"]),
                low=float(row["Low"]), close=float(row["Close"]),
                volume=int(row["Volume"]),
            ))
        return bars

    async def get_bars(
        self, symbol: str, period: BarPeriod, begin_time: datetime, end_time: datetime
    ) -> list[Bar]:
        key = (symbol, period.value)
        if key not in self._cache:
            self._cache[key] = await asyncio.to_thread(self._fetch, symbol, period)
        # Fetch the full retained window once, then slice to the requested range.
        return [b for b in self._cache[key] if begin_time <= b.time <= end_time]

    # ---- options: not provided (underlying-only data source) ----
    async def get_option_chain(
        self, symbol: str, expiry: Optional[str] = None
    ) -> list[OptionContract]:
        return []

    async def get_option_expirations(self, symbol: str) -> list[str]:
        return []

    async def get_option_bars(
        self, identifiers: list[str], begin_time: datetime, end_time: datetime,
        period: BarPeriod = BarPeriod.ONE_MINUTE,
    ) -> dict[str, list[Bar]]:
        return {}

    async def get_iv_analysis(self, symbol: str) -> IVAnalysis:
        return IVAnalysis(symbol=symbol)

    async def get_quote(self, symbol: str) -> Quote:
        bars = await self.get_bars(symbol, BarPeriod.ONE_DAY, datetime.min, datetime.max)
        last = bars[-1].close if bars else 0.0
        return Quote(symbol=symbol, last=last, bid=last, ask=last,
                     volume=bars[-1].volume if bars else 0, timestamp=datetime.now())

    async def scan_universe(
        self, filters: Optional[dict] = None, limit: int = 50
    ) -> list[ScanResult]:
        return [ScanResult(symbol=s) for s in self._settings.watchlist_symbols[:limit]]
