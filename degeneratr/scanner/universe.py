"""TickerScanner — surfaces candidate tickers with no hardcoded symbols.

Pipeline:
  1. Tiger ``market_scanner`` produces a raw US universe filtered server-side by
     option volume / IV percentile.
  2. Each candidate is enriched with IV rank, intraday capital flow (sentiment)
     and proximity to an earnings event.
  3. Candidates are scored and ranked; the top ``limit`` are returned.

The scoring weights are deliberately simple and live in one place so they're
easy to tune.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..data.base import MarketDataProvider, ScanResult
from ..data.factory import get_provider


@dataclass(slots=True)
class Candidate:
    symbol: str
    score: float
    iv_rank: Optional[float] = None
    iv_percentile: Optional[float] = None
    net_inflow: Optional[float] = None
    earnings_within_days: Optional[int] = None
    reasons: tuple[str, ...] = ()


class TickerScanner:
    """Rank tradeable option tickers using Tiger's server-side scan + enrichment."""

    def __init__(self, provider: Optional[MarketDataProvider] = None) -> None:
        self._provider = provider or get_provider()

    async def scan(
        self,
        limit: int = 20,
        scan_filters: Optional[dict] = None,
        enrich: bool = True,
    ) -> list[Candidate]:
        raw: list[ScanResult] = await self._provider.scan_universe(
            filters=scan_filters, limit=max(limit * 3, limit)
        )
        if not raw:
            return []

        if not enrich:
            return [Candidate(symbol=r.symbol, score=r.score) for r in raw[:limit]]

        earnings = await self._load_earnings_window()
        candidates = await asyncio.gather(
            *(self._enrich(r.symbol, earnings) for r in raw)
        )
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:limit]

    async def _enrich(self, symbol: str, earnings: dict[str, int]) -> Candidate:
        iv_task = self._provider.get_iv_analysis(symbol)
        flow_task = self._maybe_capital_flow(symbol)
        iv, flow = await asyncio.gather(iv_task, flow_task)

        net_inflow = flow.get("net_inflow") if flow else None
        earn_days = earnings.get(symbol)
        score, reasons = self._score(iv.iv_rank, iv.iv_percentile, net_inflow, earn_days)
        return Candidate(
            symbol=symbol,
            score=score,
            iv_rank=iv.iv_rank,
            iv_percentile=iv.iv_percentile,
            net_inflow=net_inflow,
            earnings_within_days=earn_days,
            reasons=tuple(reasons),
        )

    async def _maybe_capital_flow(self, symbol: str) -> dict:
        getter = getattr(self._provider, "get_capital_flow", None)
        if getter is None:
            return {}
        try:
            return await getter(symbol)
        except Exception:  # noqa: BLE001 - enrichment is best-effort
            return {}

    async def _load_earnings_window(self, days: int = 7) -> dict[str, int]:
        getter = getattr(self._provider, "get_earnings_calendar", None)
        if getter is None:
            return {}
        today = datetime.now().date()
        try:
            events = await getter(
                today.isoformat(), (today + timedelta(days=days)).isoformat()
            )
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, int] = {}
        for ev in events:
            sym = ev.get("symbol")
            date_str = ev.get("earnings_date", "")[:10]
            if not sym or not date_str:
                continue
            try:
                d = datetime.fromisoformat(date_str).date()
            except ValueError:
                continue
            out[sym] = min(out.get(sym, 999), (d - today).days)
        return out

    @staticmethod
    def _score(
        iv_rank: Optional[float],
        iv_percentile: Optional[float],
        net_inflow: Optional[float],
        earnings_within_days: Optional[int],
    ) -> tuple[float, list[str]]:
        """Combine enrichment signals into a single rank score (higher = better)."""
        score = 0.0
        reasons: list[str] = []

        # Volatility extremes (either cheap or rich) are tradeable; reward distance
        # from the neutral 50 IV-rank midpoint.
        if iv_rank is not None:
            edge = abs(iv_rank - 50.0) / 50.0
            score += edge * 40
            reasons.append(f"iv_rank={iv_rank:.0f}")
        if iv_percentile is not None:
            score += abs(iv_percentile - 50.0) / 50.0 * 10

        # Strong directional money flow (either sign) adds conviction.
        if net_inflow is not None and net_inflow != 0:
            score += min(20.0, abs(net_inflow) / 1_000_000.0)
            reasons.append("flow" + ("+" if net_inflow > 0 else "-"))

        # Imminent earnings = volatility catalyst; reward closeness.
        if earnings_within_days is not None and earnings_within_days >= 0:
            score += max(0.0, 20.0 - earnings_within_days * 2.5)
            reasons.append(f"earnings_in_{earnings_within_days}d")

        return score, reasons
