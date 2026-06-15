"""MomentumBreakout — directional call/put entries from RSI/MACD/VWAP/EMA."""
from __future__ import annotations

from typing import Optional

from ..data.base import Bar, IVAnalysis, OptionContract, OptionRight
from ..indicators.technical import compute_all
from .base import Signal, SignalAction, Strategy


class MomentumBreakout(Strategy):
    """Buy ATM calls on bullish momentum, ATM puts on bearish momentum.

    A long signal needs price above VWAP, fast EMA above slow EMA, a bullish
    MACD histogram cross and RSI in a constructive (not overbought) band. The
    short side mirrors it.
    """

    name = "momentum_breakout"

    def __init__(self, rsi_floor: float = 50.0, rsi_ceiling: float = 70.0) -> None:
        self.rsi_floor = rsi_floor
        self.rsi_ceiling = rsi_ceiling

    async def generate_signals(
        self,
        ticker: str,
        bars: list[Bar],
        option_chain: list[OptionContract],
        iv_analysis: IVAnalysis,
    ) -> list[Signal]:
        if len(bars) < 30:
            return []

        ind = compute_all(bars)
        spot = ind["last_close"]
        vwap = ind["vwap"]
        ema_fast = ind["ema_fast"]
        ema_slow = ind["ema_slow"]
        rsi = ind["rsi"]["value"]
        macd = ind["macd"]
        if None in (spot, vwap, ema_fast, ema_slow, rsi):
            return []

        bullish = (
            spot > vwap
            and ema_fast > ema_slow
            and macd["bullish_cross"]
            and self.rsi_floor <= rsi < self.rsi_ceiling
        )
        bearish = (
            spot < vwap
            and ema_fast < ema_slow
            and macd["bearish_cross"]
            and (100 - self.rsi_ceiling) < rsi <= (100 - self.rsi_floor)
        )

        if bullish:
            return self._build(ticker, option_chain, spot, OptionRight.CALL, rsi, ind)
        if bearish:
            return self._build(ticker, option_chain, spot, OptionRight.PUT, rsi, ind)
        return []

    def _build(
        self,
        ticker: str,
        option_chain: list[OptionContract],
        spot: float,
        right: OptionRight,
        rsi: float,
        ind: dict,
    ) -> list[Signal]:
        contract = self._atm_contract(option_chain, spot, right)
        if contract is None:
            return []
        atr = ind["atr"] or 0.0
        # Confidence scales with EMA separation, capped to [0.5, 0.95].
        spread = abs((ind["ema_fast"] or 0) - (ind["ema_slow"] or 0))
        confidence = max(0.5, min(0.95, 0.5 + spread / max(spot, 1e-9) * 20))
        mid = (contract.bid + contract.ask) / 2 if contract.ask else contract.last
        return [
            Signal(
                ticker=ticker,
                action=SignalAction.BUY,
                right=right,
                confidence=confidence,
                contract=contract,
                limit_price=round(mid, 2) if mid else None,
                stop_loss=round(spot - atr, 2) if right == OptionRight.CALL else round(spot + atr, 2),
                take_profit=round(spot + 2 * atr, 2) if right == OptionRight.CALL else round(spot - 2 * atr, 2),
                reason=f"momentum {right.value.lower()} (rsi={rsi:.0f})",
                strategy=self.name,
                meta={"spot": spot, "atr": atr},
            )
        ]
