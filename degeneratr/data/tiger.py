"""Tiger Brokers market-data provider.

Notes / hard-won lessons baked in here:
  * The Tiger SDK is fully synchronous; every call is wrapped in
    ``asyncio.to_thread`` so it never blocks the event loop.
  * Tiger's default urllib3 connection pool is tiny and gets exhausted when
    several requests fire in parallel on startup — we monkeypatch a larger
    ``PoolManager`` at import time (see below).
  * Client construction races when multiple coroutines hit it at once, so init
    is lazy and guarded by an ``asyncio.Lock``.
  * Tiger returns NaN for missing numeric fields which breaks JSON; all numeric
    access goes through ``_safe_float`` / ``_safe_int``.
  * IV rank / percentile arrive as 0–1 or 0–100 depending on account region;
    we normalize everything to 0–100.
  * Raw expiry values are cached verbatim — never reconstruct them.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime
from typing import Any, Optional

# --------------------------------------------------------------------------
# Pool fix — applied at import time, BEFORE any Tiger client is built.
# Tiger's web_utils ships a single-pool PoolManager that throttles/ò exhausts
# under parallel first-requests. Replace it with a roomier, non-blocking one.
# --------------------------------------------------------------------------
try:  # pragma: no cover - depends on tigeropen being installed
    from urllib3 import PoolManager
    import tigeropen.common.util.web_utils as _tiger_web_utils

    _tiger_web_utils.http_pool = PoolManager(num_pools=10, maxsize=32, block=False)
except Exception:  # noqa: BLE001 - never let the pool fix break import
    pass

from .base import (
    Bar,
    BarPeriod,
    IVAnalysis,
    MarketDataProvider,
    OptionContract,
    OptionRight,
    Quote,
    ScanResult,
)
from ..config import Settings, TigerTradeEnv, get_settings


# ---- numeric coercion helpers -------------------------------------------
def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to float, turning None/NaN/garbage into ``default``."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce ``value`` to int, turning None/NaN/garbage into ``default``."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return int(f)


def _to_ms(dt: datetime) -> int:
    """Convert a datetime to a millisecond epoch (Tiger time format)."""
    return int(dt.timestamp() * 1000)


from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _et_naive(ms: int) -> datetime:
    """Epoch ms -> US/Eastern wall-clock (naive), so bar times read in US market
    time regardless of the server's timezone (matches the yfinance store)."""
    return datetime.fromtimestamp(ms / 1000, _ET).replace(tzinfo=None)


def _normalize_iv_pct(value: Any) -> Optional[float]:
    """Normalize an IV rank/percentile to 0–100. Accepts 0–1 or 0–100 input."""
    if value is None:
        return None
    v = _safe_float(value, default=-1.0)
    if v < 0:
        return None
    return v * 100.0 if v <= 1.0 else v


class TigerDataProvider(MarketDataProvider):
    """Concrete :class:`MarketDataProvider` backed by ``tigeropen``."""

    # Map our provider-agnostic periods onto Tiger's BarPeriod enum lazily
    # (the import is deferred so the module loads even without the SDK present).
    _PERIOD_NAME = {
        BarPeriod.ONE_MINUTE: "ONE_MINUTE",
        BarPeriod.FIVE_MINUTES: "FIVE_MINUTES",
        BarPeriod.FIFTEEN_MINUTES: "FIFTEEN_MINUTES",
        BarPeriod.THIRTY_MINUTES: "HALF_HOUR",
        BarPeriod.ONE_HOUR: "ONE_HOUR",
        BarPeriod.ONE_DAY: "DAY",
    }

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._quote_client: Any | None = None
        self._init_lock = asyncio.Lock()
        # Cache of raw expiry values per symbol (cached verbatim, never rebuilt).
        self._expiry_cache: dict[str, list[str]] = {}
        # TTL cache for bars/chain — historical data doesn't change, so repeated
        # backtests reuse the result instead of re-hitting Tiger.
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_ttl = float(self._settings.bar_cache_ttl)

    def _cache_get(self, key: str) -> Any:
        if self._cache_ttl <= 0:
            return None
        item = self._cache.get(key)
        if item is not None and (time.monotonic() - item[0]) < self._cache_ttl:
            return item[1]
        return None

    def _cache_put(self, key: str, value: Any) -> None:
        if self._cache_ttl > 0:
            self._cache[key] = (time.monotonic(), value)

    # ---- lazy, lock-guarded client init ---------------------------------
    async def _client(self) -> Any:
        if self._quote_client is not None:
            return self._quote_client
        async with self._init_lock:
            if self._quote_client is None:
                self._quote_client = await asyncio.to_thread(self._build_client)
        return self._quote_client

    def _build_client(self) -> Any:
        """Construct a Tiger QuoteClient from settings. Runs on a worker thread."""
        from tigeropen.common.consts import Language
        from tigeropen.common.util.signature_utils import read_private_key
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        from tigeropen.quote.quote_client import QuoteClient

        config = TigerOpenClientConfig()
        config.tiger_id = self._settings.tiger_id
        config.account = self._settings.active_tiger_account
        config.language = Language.en_US
        # read_private_key strips the PEM headers and returns the base64 key body
        # that Tiger's signer expects (a raw file read fails with "Incorrect padding").
        config.private_key = read_private_key(self._settings.tiger_private_key_path)
        # PAPER vs LIVE affects which account context Tiger resolves.
        config.env = (
            "PROD" if self._settings.tiger_trade_env == TigerTradeEnv.LIVE else "SANDBOX"
        )
        return QuoteClient(config)

    def _bar_period(self, period: BarPeriod) -> Any:
        from tigeropen.common.consts import BarPeriod as TigerBarPeriod

        return getattr(TigerBarPeriod, self._PERIOD_NAME[period])

    async def _retry(self, fn: Any, *args: Any, retries: int = 5, base_delay: float = 2.0) -> Any:
        """Run a sync SDK call on a thread, backing off on Tiger rate limits."""
        for attempt in range(retries):
            try:
                return await asyncio.to_thread(fn, *args)
            except Exception as exc:  # noqa: BLE001
                if "rate limit" in str(exc).lower() and attempt < retries - 1:
                    await asyncio.sleep(base_delay * (attempt + 1))
                    continue
                raise

    # ---- MarketDataProvider API -----------------------------------------
    async def get_quote(self, symbol: str) -> Quote:
        client = await self._client()
        df = await asyncio.to_thread(client.get_briefs, [symbol])
        row = df.iloc[0] if df is not None and not df.empty else {}

        def g(key: str) -> Any:
            try:
                return row[key]
            except (KeyError, IndexError, TypeError):
                return None

        return Quote(
            symbol=symbol,
            last=_safe_float(g("latest_price")),
            bid=_safe_float(g("bid_price")),
            ask=_safe_float(g("ask_price")),
            volume=_safe_int(g("volume")),
            timestamp=datetime.now(),
            open=_safe_float(g("open")),
            high=_safe_float(g("high")),
            low=_safe_float(g("low")),
            prev_close=_safe_float(g("prev_close")),
        )

    async def get_bars(
        self,
        symbol: str,
        period: BarPeriod,
        begin_time: datetime,
        end_time: datetime,
    ) -> list[Bar]:
        # Tiger returns a fixed recent window regardless of the requested range,
        # so cache by (symbol, period) — that window is stable for historical data.
        cache_key = f"bars:{symbol}:{period.value}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        client = await self._client()
        df = await self._retry(
            client.get_bars,
            [symbol],
            self._bar_period(period),
            _to_ms(begin_time),
            _to_ms(end_time),
        )
        bars: list[Bar] = []
        if df is None or df.empty:
            return bars
        for _, row in df.iterrows():
            ts = _safe_int(row.get("time"))
            bars.append(
                Bar(
                    symbol=symbol,
                    time=_et_naive(ts) if ts else end_time,
                    open=_safe_float(row.get("open")),
                    high=_safe_float(row.get("high")),
                    low=_safe_float(row.get("low")),
                    close=_safe_float(row.get("close")),
                    volume=_safe_int(row.get("volume")),
                )
            )
        self._cache_put(cache_key, bars)
        return bars

    async def get_option_expirations(self, symbol: str) -> list[str]:
        if symbol in self._expiry_cache:
            return self._expiry_cache[symbol]
        client = await self._client()
        df = await asyncio.to_thread(client.get_option_expirations, [symbol])
        expiries: list[str] = []
        if df is not None and not df.empty:
            # Cache the raw values exactly as Tiger returns them.
            for _, row in df.iterrows():
                raw = row.get("date") if "date" in row else row.get("timestamp")
                if raw is not None:
                    expiries.append(str(raw))
        self._expiry_cache[symbol] = expiries
        return expiries

    async def get_option_chain(
        self, symbol: str, expiry: Optional[str] = None
    ) -> list[OptionContract]:
        if expiry is None:
            expiries = await self.get_option_expirations(symbol)
            if not expiries:
                return []
            expiry = expiries[0]

        client = await self._client()
        df = await self._retry(client.get_option_chain, symbol, expiry)
        contracts: list[OptionContract] = []
        if df is None or df.empty:
            return contracts
        for _, row in df.iterrows():
            put_call = str(row.get("put_call", "")).upper()
            right = OptionRight.PUT if put_call.startswith("P") else OptionRight.CALL
            contracts.append(
                OptionContract(
                    symbol=symbol,
                    identifier=str(row.get("identifier", "")),
                    expiry=expiry,
                    strike=_safe_float(row.get("strike")),
                    right=right,
                    bid=_safe_float(row.get("bid_price")),
                    ask=_safe_float(row.get("ask_price")),
                    last=_safe_float(row.get("latest_price")),
                    volume=_safe_int(row.get("volume")),
                    open_interest=_safe_int(row.get("open_interest")),
                    implied_vol=_safe_float(row.get("implied_vol")) or None,
                    delta=_safe_float(row.get("delta")) or None,
                    gamma=_safe_float(row.get("gamma")) or None,
                    theta=_safe_float(row.get("theta")) or None,
                    vega=_safe_float(row.get("vega")) or None,
                )
            )
        return contracts

    async def get_iv_analysis(self, symbol: str) -> IVAnalysis:
        client = await self._client()
        from tigeropen.common.consts import Market, OptionAnalysisPeriod

        # get_option_analysis returns a list of OptionAnalysis objects (not a
        # DataFrame); IV rank/percentile are nested under .iv_metric as 0–1 values.
        result = await asyncio.to_thread(
            client.get_option_analysis,
            [symbol],
            OptionAnalysisPeriod.FIFTY_TWO_WEEK,
            Market.US,
        )
        if not result:
            return IVAnalysis(symbol=symbol)
        item = result[0]
        metric = getattr(item, "iv_metric", None)
        rank = getattr(metric, "rank", None) if metric is not None else None
        percentile = getattr(metric, "percentile", None) if metric is not None else None
        return IVAnalysis(
            symbol=symbol,
            iv=_safe_float(getattr(item, "implied_vol_30_days", None)) or None,
            iv_rank=_normalize_iv_pct(rank),
            iv_percentile=_normalize_iv_pct(percentile),
        )

    async def get_option_bars(
        self,
        identifiers: list[str],
        begin_time: datetime,
        end_time: datetime,
        period: BarPeriod = BarPeriod.ONE_MINUTE,
    ) -> dict[str, list[Bar]]:
        client = await self._client()
        df = await self._retry(
            client.get_option_bars,
            identifiers,
            _to_ms(begin_time),
            _to_ms(end_time),
            self._bar_period(period),
        )
        out: dict[str, list[Bar]] = {ident: [] for ident in identifiers}
        if df is None or df.empty:
            return out
        for _, row in df.iterrows():
            ident = str(row.get("identifier", ""))
            ts = _safe_int(row.get("time"))
            out.setdefault(ident, []).append(
                Bar(
                    symbol=ident,
                    time=_et_naive(ts) if ts else end_time,
                    open=_safe_float(row.get("open")),
                    high=_safe_float(row.get("high")),
                    low=_safe_float(row.get("low")),
                    close=_safe_float(row.get("close")),
                    volume=_safe_int(row.get("volume")),
                )
            )
        return out

    async def scan_universe(
        self, filters: Optional[dict] = None, limit: int = 50
    ) -> list[ScanResult]:
        """Run a Tiger server-side market scan.

        ``filters`` is passed through to the SDK's ``market_scanner``; callers
        (see :mod:`degeneratr.scanner.universe`) build the filter/sort objects.
        """
        client = await self._client()
        from tigeropen.common.consts import Market

        scan = await asyncio.to_thread(
            client.market_scanner,
            Market.US,
            (filters or {}).get("filters"),
            (filters or {}).get("sort_field_data"),
            (filters or {}).get("page", 0),
            min(limit, (filters or {}).get("page_size", limit)),
        )
        results: list[ScanResult] = []
        items = getattr(scan, "items", None) or []
        for item in items[:limit]:
            sym = getattr(item, "symbol", None) or (
                item.get("symbol") if isinstance(item, dict) else None
            )
            if not sym:
                continue
            results.append(ScanResult(symbol=str(sym), score=0.0, extras={"raw": item}))
        return results

    # ---- supplemental signals (used by the scanner) ---------------------
    async def get_capital_flow(self, symbol: str) -> dict[str, Any]:
        """Money-flow snapshot for ``symbol`` (net inflow proxy for sentiment)."""
        client = await self._client()
        from tigeropen.common.consts import CapitalPeriod

        df = await asyncio.to_thread(
            client.get_capital_flow, symbol, period=CapitalPeriod.INTRADAY
        )
        if df is None or getattr(df, "empty", True):
            return {}
        row = df.iloc[-1]
        return {
            "net_inflow": _safe_float(row.get("net_inflow")),
            "in_flow": _safe_float(row.get("in_flow")),
            "out_flow": _safe_float(row.get("out_flow")),
        }

    async def get_earnings_calendar(
        self, begin_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """Upcoming earnings events between two YYYY-MM-DD dates."""
        client = await self._client()
        from tigeropen.common.consts import Market

        df = await asyncio.to_thread(
            client.get_corporate_earnings_calendar, Market.US, begin_date, end_date
        )
        events: list[dict[str, Any]] = []
        if df is None or getattr(df, "empty", True):
            return events
        for _, row in df.iterrows():
            events.append(
                {
                    "symbol": str(row.get("symbol", "")),
                    "earnings_date": str(row.get("earnings_date", "")),
                    "eps_estimate": _safe_float(row.get("eps_estimate")),
                }
            )
        return events
