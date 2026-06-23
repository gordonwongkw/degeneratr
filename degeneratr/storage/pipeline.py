"""In-process data pipeline: seed once from yfinance, then accrue Tiger bars and
persist the algorithm's trade log.

Runs as a background task inside the web service (a Render disk binds to one
service, so a separate cron can't share the store). Mirrors the Telegram
watcher's loop shape and uses the shared :mod:`degeneratr.marketclock`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from ..config import Settings, get_settings
from ..data.base import BarPeriod, MarketDataProvider
from ..data.factory import get_provider
from ..marketclock import after_close, day_key, market_hours, now_et
from ..strategies.price_action import PriceActionStrategy
from .ingest import ingest_yfinance
from .store import BarStore

logger = logging.getLogger("degeneratr.pipeline")

_SIGNAL_PERIOD = BarPeriod.FIFTEEN_MINUTES  # the strategy runs on 15m


def _parse_periods(spec: str) -> list[BarPeriod]:
    vals = {x.strip() for x in spec.split(",") if x.strip()}
    return [p for p in BarPeriod if p.value in vals] or [BarPeriod.FIVE_MINUTES, _SIGNAL_PERIOD]


async def ingest_underlying(
    symbols: list[str], periods: list[BarPeriod], *,
    store: BarStore, provider: MarketDataProvider, days: int = 5,
) -> int:
    """Pull the latest ``days``-window of UNDERLYING bars per symbol/period from
    ``provider`` (Tiger, going forward) and upsert into the store. No options."""
    end = datetime.now()
    begin = end - timedelta(days=days)
    total = 0
    for symbol in symbols:
        for period in periods:
            try:
                bars = await provider.get_bars(symbol, period, begin, end)
                total += store.save_underlying(symbol, period.value, bars)
            except Exception as exc:  # noqa: BLE001 - one symbol/period shouldn't sink the rest
                logger.warning("ingest %s %s failed: %s", symbol, period.value, exc)
    return total


class _PreloadedProvider:
    """Feed one symbol's already-loaded bars to the backtester (no DB re-read)."""

    def __init__(self, symbol: str, bars: list) -> None:
        self._symbol = symbol
        self._bars = bars

    async def get_bars(self, symbol, period, begin_time, end_time):
        return self._bars if symbol == self._symbol else []

    def __getattr__(self, name):
        async def _noop(*a, **k):
            return [] if "bars" not in name else {}
        return _noop


async def _persist_one(symbol: str, settings: Settings, store: BarStore) -> int:
    """Replay one symbol's stored history into trade_log via the FAST path:
    `series()` once (vectorized O(n)) + ReplaySignalStrategy through the
    backtester — the same cheap path the charts use (not the O(n²) per-bar
    `generate_signals`, which blocks)."""
    from ..backtester.underlying import UnderlyingBacktester
    from ..strategies.replay import ReplaySignalStrategy

    end = datetime.now()
    begin = end - timedelta(days=400)  # all stored history (datetime.min errors on Windows)
    bars = store.load_underlying(symbol, _SIGNAL_PERIOD.value, begin, end)
    if len(bars) < 31:
        return 0
    strat = PriceActionStrategy()
    ser = strat.series(bars)
    bt = UnderlyingBacktester(
        strategy=ReplaySignalStrategy(ser["score"], strat.min_score),
        settings=settings, provider=_PreloadedProvider(symbol, bars),
    )
    result = await bt.run(symbol, bars[0].time, bars[-1].time, period=_SIGNAL_PERIOD)
    # Persist only realized round-trips — an 'open' position is unrealized and would
    # inflate the performance stats; it lands once it actually closes.
    realized = [rt for rt in result.round_trips if rt.exit_reason != "open"]
    return store.save_trades(symbol, realized)


def _persist_blocking(settings: Settings, store: BarStore) -> int:
    async def _all() -> int:
        total = 0
        for symbol in settings.watchlist_symbols:
            try:
                total += await _persist_one(symbol, settings, store)
            except Exception as exc:  # noqa: BLE001
                logger.warning("trade-log persist for %s failed: %s", symbol, exc)
        return total

    return asyncio.run(_all())


async def persist_trade_log(settings: Settings, *, store: BarStore) -> int:
    """Replay the strategy over the stored history and upsert every round-trip into
    ``trade_log`` (idempotent). Runs in a **worker thread** — the backtest is
    CPU-bound and must NOT run on the web server's event loop, or it starves the
    health check and Render returns 502."""
    return await asyncio.to_thread(_persist_blocking, settings, store)


async def _seed_if_empty(settings: Settings, store: BarStore, periods: list[BarPeriod]) -> None:
    """One-time yfinance seed: if the store has no underlying bars, pull ~60d of
    5m/15m so the dashboard has data immediately on a fresh deploy."""
    if store.coverage().get("underlying"):
        logger.info("pipeline: store already populated, skipping yfinance seed")
        return
    logger.info("pipeline: empty store — seeding from yfinance (one-time)…")
    try:
        res = await ingest_yfinance(settings.watchlist_symbols, periods, store=store)
        logger.info("pipeline: yfinance seed saved %s", res["saved"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline: yfinance seed failed: %s", exc)


async def run_data_pipeline(settings: Settings | None = None) -> None:
    """Seed once, then accrue Tiger bars during the session and persist the trade
    log after the close. Runs until cancelled."""
    settings = settings or get_settings()
    store = BarStore(settings.bar_store_path)
    periods = _parse_periods(settings.ingest_periods)
    interval = settings.ingest_interval_seconds
    window = settings.ingest_window_days

    await _seed_if_empty(settings, store, periods)
    # A fresh seed gives the trade log something to chew on right away.
    try:
        await persist_trade_log(settings, store=store)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline: initial trade-log persist failed: %s", exc)

    # Live provider for going-forward ingestion (uncached). Built once and reused;
    # if it can't be built (e.g. Tiger creds not set), we still serve the seed.
    provider: MarketDataProvider | None = None
    try:
        provider = get_provider(settings.model_copy(update={"bar_cache_ttl": 0}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline: live provider unavailable (%s) — ingest paused, seed still served", exc)

    last_eod: str | None = None
    logger.info("degeneratr data pipeline online (provider=%s, every %.0fs)",
                settings.market_data_provider, interval)
    while True:
        now = now_et()
        try:
            if market_hours(now) and provider is not None:
                n = await ingest_underlying(settings.watchlist_symbols, periods,
                                            store=store, provider=provider, days=window)
                # Persist the trade log intraday too (not just after the close) so the
                # performance panel reflects today's trades within one ingest cycle.
                saved = await persist_trade_log(settings, store=store)
                logger.debug("pipeline: ingested %d bars, persisted %d trades", n, saved)
                await asyncio.sleep(interval)
            elif after_close(now) and last_eod != day_key(now):
                if provider is not None:
                    await ingest_underlying(settings.watchlist_symbols, periods,
                                            store=store, provider=provider, days=window)
                saved = await persist_trade_log(settings, store=store)
                last_eod = day_key(now)
                logger.info("pipeline: end-of-day trade log persisted (%d round-trips)", saved)
                await asyncio.sleep(300)
            else:
                await asyncio.sleep(300)
        except asyncio.CancelledError:
            logger.info("data pipeline stopped")
            raise
        except Exception as exc:  # noqa: BLE001 - one bad cycle shouldn't kill the loop
            logger.warning("pipeline cycle failed: %s", exc)
            await asyncio.sleep(interval)
