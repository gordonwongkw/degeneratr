"""ZeroDTE — 0DTE/1DTE scalping inside two intraday windows (US/Eastern).

Only fires during the morning trend window (09:45–11:30 ET) and the afternoon
window (14:00–15:45 ET), avoiding the lunchtime chop and the closing auction.
Picks the nearest expiry (0DTE if available, else 1DTE) and scalps with VWAP +
short-EMA confirmation.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from ..data.base import Bar, IVAnalysis, OptionContract, OptionRight
from ..indicators.technical import compute_all
from .base import Signal, SignalAction, Strategy

_ET = ZoneInfo("America/New_York")
_MORNING = (time(9, 45), time(11, 30))
_AFTERNOON = (time(14, 0), time(15, 45))


class ZeroDTE(Strategy):
    name = "zero_dte"

    def __init__(self, now_provider=None) -> None:
        # Injectable clock makes the time-window logic testable.
        self._now = now_provider or (lambda: datetime.now(_ET))

    def _in_window(self) -> bool:
        now_t = self._now().astimezone(_ET).time()
        return (_MORNING[0] <= now_t <= _MORNING[1]) or (
            _AFTERNOON[0] <= now_t <= _AFTERNOON[1]
        )

    async def generate_signals(
        self,
        ticker: str,
        bars: list[Bar],
        option_chain: list[OptionContract],
        iv_analysis: IVAnalysis,
    ) -> list[Signal]:
        if not self._in_window() or len(bars) < 20 or not option_chain:
            return []

        ind = compute_all(bars, ema_fast=5, ema_slow=13)
        spot = ind["last_close"]
        vwap = ind["vwap"]
        if spot is None or vwap is None:
            return []

        ema_fast = ind["ema_fast"]
        rsi = ind["rsi"]["value"]
        if ema_fast is None or rsi is None:
            return []

        long_scalp = spot > vwap and ema_fast > vwap and rsi > 50
        short_scalp = spot < vwap and ema_fast < vwap and rsi < 50
        if not (long_scalp or short_scalp):
            return []

        right = OptionRight.CALL if long_scalp else OptionRight.PUT
        # Nearest expiry contracts only (0DTE/1DTE): pick the min raw expiry.
        nearest_expiry = min((c.expiry for c in option_chain if c.expiry), default=None)
        near_chain = [c for c in option_chain if c.expiry == nearest_expiry] or option_chain
        contract = self._atm_contract(near_chain, spot, right)
        if contract is None:
            return []

        mid = (contract.bid + contract.ask) / 2 if contract.ask else contract.last
        atr = ind["atr"] or (spot * 0.002)
        return [
            Signal(
                ticker=ticker,
                action=SignalAction.BUY,
                right=right,
                confidence=0.6,
                contract=contract,
                quantity=1,
                limit_price=round(mid, 2) if mid else None,
                stop_loss=round(spot - atr, 2) if long_scalp else round(spot + atr, 2),
                take_profit=round(spot + 1.5 * atr, 2) if long_scalp else round(spot - 1.5 * atr, 2),
                reason=f"0DTE scalp {right.value.lower()} in window",
                strategy=self.name,
                meta={"expiry": nearest_expiry, "spot": spot},
            )
        ]
