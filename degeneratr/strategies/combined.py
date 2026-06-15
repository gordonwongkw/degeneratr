"""The degeneratr algorithm — one strategy that fuses every signal source.

Rather than picking a single technique, this runs the momentum, IV-rank and
0DTE components together and merges their output into one coherent set of
signals. When multiple components agree on a direction we keep the highest-
confidence one and bump its confidence (agreement is corroboration), so the
risk manager sees a single sized order per direction instead of duplicates.

This is the production trading logic; the individual component strategies remain
available for isolated analysis/backtesting.
"""
from __future__ import annotations

from ..data.base import Bar, IVAnalysis, OptionContract
from .base import Signal, Strategy
from .iv_rank import IVRankStrategy
from .momentum import MomentumBreakout
from .zero_dte import ZeroDTE


class CombinedStrategy(Strategy):
    name = "degeneratr"

    def __init__(self) -> None:
        self._components: list[Strategy] = [
            MomentumBreakout(),
            IVRankStrategy(),
            ZeroDTE(),
        ]

    async def generate_signals(
        self,
        ticker: str,
        bars: list[Bar],
        option_chain: list[OptionContract],
        iv_analysis: IVAnalysis,
    ) -> list[Signal]:
        raw: list[Signal] = []
        for comp in self._components:
            raw.extend(await comp.generate_signals(ticker, bars, option_chain, iv_analysis))
        if not raw:
            return []

        # Merge by (action, right): keep the highest-confidence signal per
        # direction and record which components fired.
        best: dict[tuple[str, str], Signal] = {}
        agree: dict[tuple[str, str], list[str]] = {}
        for sig in raw:
            key = (sig.action.value, sig.right.value)
            agree.setdefault(key, []).append(sig.strategy)
            current = best.get(key)
            if current is None or sig.confidence > current.confidence:
                best[key] = sig

        merged: list[Signal] = []
        for key, sig in best.items():
            sources = agree[key]
            # Corroboration bonus: +0.1 per extra component that agreed, capped.
            sig.confidence = min(0.99, sig.confidence + 0.10 * (len(set(sources)) - 1))
            sig.strategy = self.name
            sig.reason = f"{sig.reason} [{'+'.join(sorted(set(sources)))}]"
            merged.append(sig)
        return merged
