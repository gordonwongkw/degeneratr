"""Signal-generating strategies.

The production algorithm is :class:`PriceActionStrategy` (registered as
``degeneratr``): it triggers purely on the underlying's price action and lets
the execution layer pick the closest OTM option. The older option-aware
strategies remain importable for reference but are not part of the registry.
"""
from __future__ import annotations

from .base import Signal, SignalAction, Strategy, select_otm_contract
from .price_action import PriceActionStrategy

#: Registry for constructing strategies by name. Currently a single production
#: algorithm; configurations of it are explored via the backtest sweep.
STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    PriceActionStrategy.name: PriceActionStrategy,
}

#: No standalone component strategies are exposed in the UI for now.
COMPONENT_STRATEGIES: list[str] = []

ALGORITHM_NAME = PriceActionStrategy.name

__all__ = [
    "Signal",
    "SignalAction",
    "Strategy",
    "PriceActionStrategy",
    "select_otm_contract",
    "STRATEGY_REGISTRY",
    "COMPONENT_STRATEGIES",
    "ALGORITHM_NAME",
]
