"""Replay a precomputed signal-score series as a Strategy.

The charts endpoint computes :meth:`PriceActionStrategy.series` once (vectorized,
O(n)) and then needs the *trades* those signals produce. Running the real
strategy through the backtester would recompute indicators every bar (O(n²)).
Instead, wrap the precomputed signed scores in this strategy so the backtester
gets O(1) signal lookups per bar — identical results, far cheaper.
"""
from __future__ import annotations

from .base import Signal, SignalAction, Strategy
from ..data.base import Bar, IVAnalysis, OptionContract, OptionRight


class ReplaySignalStrategy(Strategy):
    """Emit a signal per bar from a precomputed signed-score series.

    ``scores[i]`` is the signed confluence net at bar ``i`` (>0 bullish/CALL,
    <0 bearish/PUT). A signal fires when ``abs(score) >= min_score`` — the same
    rule :class:`PriceActionStrategy.generate_signals` applies.
    """

    name = "degeneratr"

    def __init__(self, scores: list[int], min_score: int) -> None:
        self._scores = scores
        self._min = min_score

    async def generate_signals(
        self,
        ticker: str,
        bars: list[Bar],
        option_chain: list[OptionContract],
        iv_analysis: IVAnalysis,
    ) -> list[Signal]:
        i = len(bars) - 1
        if i < 0 or i >= len(self._scores):
            return []
        sc = self._scores[i]
        spot = bars[-1].close
        if sc >= self._min:
            return [self._mk(ticker, OptionRight.CALL, sc, spot)]
        if -sc >= self._min:
            return [self._mk(ticker, OptionRight.PUT, -sc, spot)]
        return []

    def _mk(self, ticker: str, right: OptionRight, score: int, spot: float) -> Signal:
        direction = "bullish" if right == OptionRight.CALL else "bearish"
        return Signal(
            ticker=ticker, action=SignalAction.BUY, right=right,
            confidence=min(0.95, 0.5 + 0.1 * abs(score)), contract=None, quantity=0,
            reason=f"{direction} confluence {abs(score)}", strategy=self.name,
            meta={"spot": spot, "score": abs(score)},
        )
