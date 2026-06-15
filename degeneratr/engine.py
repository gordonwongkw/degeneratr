"""TradingEngine — wires scanner -> strategy -> risk -> broker.

Two entry points:
  * ``run_paper()`` — uses the configured (paper) broker for a dry run.
  * ``run_live()``  — same loop against the live/execution broker; refuses to
    start unless the broker provider is explicitly configured for execution.

The loop is a single pass over scanned candidates (one "tick"); a scheduler or
``--loop`` wrapper can call :meth:`tick` repeatedly.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .broker import BrokerProvider, get_broker
from .broker.base import Order
from .config import Settings, get_settings
from .data.base import BarPeriod, MarketDataProvider
from .data.factory import get_provider
from .risk.manager import RiskManager
from .scanner.universe import Candidate, TickerScanner
from .strategies.base import Signal, Strategy, select_otm_contract

logger = logging.getLogger("degeneratr.engine")


@dataclass(slots=True)
class TickReport:
    timestamp: datetime
    candidates: list[Candidate] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)


class TradingEngine:
    def __init__(
        self,
        strategies: Sequence[Strategy],
        provider: Optional[MarketDataProvider] = None,
        broker: Optional[BrokerProvider] = None,
        risk: Optional[RiskManager] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._provider = provider or get_provider()
        self._broker = broker or get_broker(self._settings)
        self._risk = risk or RiskManager(self._settings)
        self._scanner = TickerScanner(self._provider)
        self._strategies = list(strategies)
        if not self._strategies:
            raise ValueError("TradingEngine requires at least one strategy")

    # ---- one pass over the universe -------------------------------------
    async def tick(
        self,
        scan_limit: int = 10,
        bar_period: BarPeriod = BarPeriod.FIVE_MINUTES,
        lookback_minutes: int = 240,
        dry_run: bool = False,
    ) -> TickReport:
        report = TickReport(timestamp=datetime.now())

        candidates = await self._scanner.scan(limit=scan_limit)
        report.candidates = candidates
        if not candidates:
            logger.info("scanner returned no candidates")
            return report

        account = await self._broker.get_account_info()
        positions = await self._broker.get_positions()

        end = datetime.now()
        begin = end - timedelta(minutes=lookback_minutes)

        for cand in candidates:
            try:
                signals = await self._signals_for(cand.symbol, bar_period, begin, end)
            except Exception as exc:  # noqa: BLE001 - one bad ticker shouldn't halt the tick
                logger.warning("signal generation failed for %s: %s", cand.symbol, exc)
                report.rejections.append(f"{cand.symbol}: {exc}")
                continue

            for signal in signals:
                report.signals.append(signal)
                decision = self._risk.evaluate(signal, account, positions)
                if not decision.approved or decision.order is None:
                    report.rejections.append(f"{signal.ticker}: {decision.reason}")
                    continue
                if dry_run:
                    logger.info("[dry-run] would place %s x%d %s",
                                signal.ticker, decision.quantity, signal.right.value)
                    continue
                order = await self._broker.place_order(decision.order)
                report.orders.append(order)
                logger.info("placed order %s for %s", order.order_id, signal.ticker)

        return report

    async def _signals_for(
        self, symbol: str, bar_period: BarPeriod, begin: datetime, end: datetime
    ) -> list[Signal]:
        bars = await self._provider.get_bars(symbol, bar_period, begin, end)
        chain = await self._provider.get_option_chain(symbol)
        iv = await self._provider.get_iv_analysis(symbol)
        spot = bars[-1].close if bars else None
        out: list[Signal] = []
        for strat in self._strategies:
            for sig in await strat.generate_signals(symbol, bars, chain, iv):
                # Price-action signals carry only a direction — resolve the
                # closest OTM contract for execution.
                if sig.contract is None and spot is not None:
                    sig.contract = select_otm_contract(chain, spot, sig.right)
                if sig.contract is not None:
                    out.append(sig)
        return out

    # ---- run modes ------------------------------------------------------
    async def run_paper(self, ticks: int = 1, interval_seconds: float = 0.0) -> list[TickReport]:
        """Run the loop against the configured (paper) broker."""
        reports: list[TickReport] = []
        for n in range(ticks):
            reports.append(await self.tick(dry_run=False))
            if interval_seconds and n < ticks - 1:
                await asyncio.sleep(interval_seconds)
        return reports

    async def run_live(self, ticks: int = 1, interval_seconds: float = 60.0) -> list[TickReport]:
        """Run the loop against a live execution broker.

        Guard rail: refuses to run unless the broker provider is set to a real
        execution backend (i.e. not ``paper``).
        """
        if self._settings.broker_provider.strip().lower() == "paper":
            raise RuntimeError(
                "run_live() called while BROKER_PROVIDER=paper — refusing. "
                "Set BROKER_PROVIDER=moomoo to trade for real."
            )
        reports: list[TickReport] = []
        for n in range(ticks):
            if self._risk.kill_switch_tripped():
                logger.error("daily kill-switch active — halting live run")
                break
            reports.append(await self.tick(dry_run=False))
            if interval_seconds and n < ticks - 1:
                await asyncio.sleep(interval_seconds)
        return reports

    async def close(self) -> None:
        await self._provider.close()
        await self._broker.close()
