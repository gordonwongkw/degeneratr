"""Strategy contract and the :class:`Signal` it emits.

A strategy consumes a ticker's recent bars, its option chain and IV stats and
returns zero or more :class:`Signal` objects. Signals are intentionally broker-
agnostic — the risk manager validates them and the engine translates them into
broker orders.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from ..data.base import Bar, IVAnalysis, OptionContract, OptionRight


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(slots=True)
class Signal:
    """An intent to open/close an options position.

    The strategy fills in what it knows; ``contract`` may be ``None`` for an
    underlying-only directional view that a later stage resolves to a strike.
    """

    ticker: str
    action: SignalAction
    right: OptionRight
    confidence: float = 0.5                 # 0–1
    contract: Optional[OptionContract] = None
    quantity: int = 1                        # contracts
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    strategy: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    meta: dict = field(default_factory=dict)


class Strategy(ABC):
    """Base class for all signal-generating strategies."""

    #: Human-readable strategy name; subclasses should override.
    name: str = "strategy"

    @abstractmethod
    async def generate_signals(
        self,
        ticker: str,
        bars: list[Bar],
        option_chain: list[OptionContract],
        iv_analysis: IVAnalysis,
    ) -> list[Signal]:
        """Return signals for ``ticker`` given its market context."""

    # ---- shared helpers -------------------------------------------------
    @staticmethod
    def _atm_contract(
        option_chain: list[OptionContract],
        spot: float,
        right: OptionRight,
    ) -> Optional[OptionContract]:
        """Pick the at-the-money contract of ``right`` nearest to ``spot``."""
        candidates = [c for c in option_chain if c.right == right and c.strike > 0]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c.strike - spot))


def select_otm_contract(
    option_chain: list[OptionContract],
    spot: float,
    right: OptionRight,
) -> Optional[OptionContract]:
    """Pick the closest out-of-the-money contract for an execution direction.

    Bullish (CALL) → smallest strike above spot. Bearish (PUT) → largest strike
    below spot. Returns ``None`` when no OTM strike exists in the chain.
    """
    if right == OptionRight.CALL:
        cands = [c for c in option_chain if c.right == OptionRight.CALL and c.strike > spot]
        return min(cands, key=lambda c: c.strike) if cands else None
    cands = [c for c in option_chain if c.right == OptionRight.PUT and c.strike < spot]
    return max(cands, key=lambda c: c.strike) if cands else None
