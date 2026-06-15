"""IVRankStrategy — trade volatility regime via IV rank.

  * IV rank < 30  -> options are cheap; buy premium (long ATM straddle leg).
  * IV rank > 70  -> options are rich; sell premium (credit spread).
  * In between     -> stand aside.
"""
from __future__ import annotations

from typing import Optional

from ..data.base import Bar, IVAnalysis, OptionContract, OptionRight
from ..indicators.technical import compute_all
from .base import Signal, SignalAction, Strategy


class IVRankStrategy(Strategy):
    name = "iv_rank"

    def __init__(self, low_threshold: float = 30.0, high_threshold: float = 70.0) -> None:
        self.low_threshold = low_threshold
        self.high_threshold = high_threshold

    async def generate_signals(
        self,
        ticker: str,
        bars: list[Bar],
        option_chain: list[OptionContract],
        iv_analysis: IVAnalysis,
    ) -> list[Signal]:
        ivr = iv_analysis.iv_rank
        if ivr is None or not option_chain:
            return []

        ind = compute_all(bars) if bars else None
        spot = (ind["last_close"] if ind else None) or self._spot_from_chain(option_chain)
        if spot is None:
            return []

        if ivr < self.low_threshold:
            return self._buy_premium(ticker, option_chain, spot, ivr)
        if ivr > self.high_threshold:
            return self._sell_spread(ticker, option_chain, spot, ivr, bars, ind)
        return []

    @staticmethod
    def _spot_from_chain(chain: list[OptionContract]) -> Optional[float]:
        strikes = [c.strike for c in chain if c.strike > 0]
        return sum(strikes) / len(strikes) if strikes else None

    def _buy_premium(
        self, ticker: str, chain: list[OptionContract], spot: float, ivr: float
    ) -> list[Signal]:
        """Low IV: buy the ATM call leg (cheap convexity)."""
        call = self._atm_contract(chain, spot, OptionRight.CALL)
        if call is None:
            return []
        mid = (call.bid + call.ask) / 2 if call.ask else call.last
        return [
            Signal(
                ticker=ticker,
                action=SignalAction.BUY,
                right=OptionRight.CALL,
                confidence=min(0.9, 0.6 + (self.low_threshold - ivr) / 100),
                contract=call,
                limit_price=round(mid, 2) if mid else None,
                reason=f"low IV rank {ivr:.0f} — buy premium",
                strategy=self.name,
                meta={"iv_rank": ivr, "regime": "low_iv"},
            )
        ]

    def _sell_spread(
        self,
        ticker: str,
        chain: list[OptionContract],
        spot: float,
        ivr: float,
        bars: list[Bar],
        ind: Optional[dict],
    ) -> list[Signal]:
        """High IV: sell a credit spread on the side momentum disfavors."""
        # Lean bearish-credit (sell call) by default; flip to put-credit if price
        # is holding above VWAP.
        right = OptionRight.CALL
        if ind and ind["vwap"] and ind["last_close"] and ind["last_close"] > ind["vwap"]:
            right = OptionRight.PUT
        short_leg = self._atm_contract(chain, spot, right)
        if short_leg is None:
            return []
        mid = (short_leg.bid + short_leg.ask) / 2 if short_leg.ask else short_leg.last
        return [
            Signal(
                ticker=ticker,
                action=SignalAction.SELL,
                right=right,
                confidence=min(0.9, 0.6 + (ivr - self.high_threshold) / 100),
                contract=short_leg,
                limit_price=round(mid, 2) if mid else None,
                reason=f"high IV rank {ivr:.0f} — sell {right.value.lower()} credit spread",
                strategy=self.name,
                meta={"iv_rank": ivr, "regime": "high_iv", "structure": "credit_spread"},
            )
        ]
