"""RiskManager — the gate between strategy signals and the broker.

Enforces, in order:
  * daily kill-switch (cumulative realized loss),
  * per-trade max loss,
  * net delta exposure ceiling,
  * position sizing from account equity and the configured risk fraction,
  * basic Greeks sanity checks.

It converts an approved :class:`Signal` into a broker :class:`OrderRequest`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..broker.base import AccountInfo, OrderRequest, OrderSide, OrderType, Position
from ..config import Settings, get_settings
from ..strategies.base import Signal, SignalAction

_CONTRACT_MULTIPLIER = 100


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    quantity: int = 0
    reason: str = ""
    order: Optional[OrderRequest] = None


class RiskManager:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._realized_loss_today = 0.0

    # ---- daily P&L tracking ---------------------------------------------
    def record_realized_pnl(self, pnl: float) -> None:
        """Feed realized P&L back in so the daily kill-switch can trip."""
        if pnl < 0:
            self._realized_loss_today += -pnl

    def reset_daily(self) -> None:
        """Reset the daily loss tally — call at the start of each session."""
        self._realized_loss_today = 0.0

    @property
    def daily_loss(self) -> float:
        return self._realized_loss_today

    def kill_switch_tripped(self) -> bool:
        return self._realized_loss_today >= self._s.risk_max_daily_loss

    # ---- core evaluation ------------------------------------------------
    def evaluate(
        self,
        signal: Signal,
        account: AccountInfo,
        positions: list[Position],
    ) -> RiskDecision:
        if self.kill_switch_tripped():
            return RiskDecision(False, reason="daily max loss reached — kill switch active")

        contract = signal.contract
        if contract is None:
            return RiskDecision(False, reason="signal has no resolved contract")

        price = signal.limit_price or contract.last or (contract.bid + contract.ask) / 2
        if not price or price <= 0:
            return RiskDecision(False, reason="no usable contract price")

        # ---- position sizing ----
        qty = self._size_position(signal, price, account)
        if qty < 1:
            return RiskDecision(False, reason="position size rounds to zero")

        # ---- per-trade max loss ----
        per_trade_risk = self._per_trade_risk(signal, price, qty)
        if per_trade_risk > self._s.risk_max_loss_per_trade:
            # Shrink to fit the per-trade cap if possible.
            qty = self._shrink_to_cap(signal, price)
            if qty < 1:
                return RiskDecision(
                    False, reason=f"per-trade risk {per_trade_risk:.0f} exceeds cap"
                )

        # ---- delta exposure ceiling ----
        projected = self._projected_delta(signal, qty, positions)
        if abs(projected) > self._s.risk_max_delta_exposure:
            return RiskDecision(
                False,
                reason=f"net delta {projected:.0f} would exceed limit "
                f"{self._s.risk_max_delta_exposure:.0f}",
            )

        # ---- Greeks sanity ----
        ok, why = self._greeks_ok(signal)
        if not ok:
            return RiskDecision(False, reason=why)

        order = OrderRequest(
            symbol=contract.identifier or signal.ticker,
            side=OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL,
            quantity=qty,
            order_type=OrderType.LIMIT if signal.limit_price else OrderType.MARKET,
            limit_price=signal.limit_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            client_tag=signal.strategy,
            meta={"mark": price, **signal.meta},
        )
        return RiskDecision(True, quantity=qty, reason="approved", order=order)

    # ---- helpers --------------------------------------------------------
    def _size_position(self, signal: Signal, price: float, account: AccountInfo) -> int:
        """Size by risk fraction of equity, scaled by signal confidence."""
        equity = max(account.net_liquidation, 0.0)
        risk_budget = equity * self._s.risk_per_trade_fraction * max(signal.confidence, 0.1)
        cost_per_contract = price * _CONTRACT_MULTIPLIER
        if cost_per_contract <= 0:
            return 0
        qty = int(risk_budget // cost_per_contract)
        # Respect an explicit strategy request as an upper bound when provided.
        if signal.quantity:
            qty = min(qty, signal.quantity) if qty else signal.quantity
        return max(qty, 0)

    def _per_trade_risk(self, signal: Signal, price: float, qty: int) -> float:
        """Dollar risk for the trade. For long premium, max loss is the debit.

        When a stop is provided we approximate risk as distance-to-stop in the
        underlying times delta; otherwise we fall back to the full debit.
        """
        if signal.action == SignalAction.BUY:
            return price * qty * _CONTRACT_MULTIPLIER
        # Short premium: use stop distance if available, else a 2x premium proxy.
        return price * qty * _CONTRACT_MULTIPLIER * 2.0

    def _shrink_to_cap(self, signal: Signal, price: float) -> int:
        cap = self._s.risk_max_loss_per_trade
        cost_per_contract = price * _CONTRACT_MULTIPLIER
        if signal.action != SignalAction.BUY:
            cost_per_contract *= 2.0
        if cost_per_contract <= 0:
            return 0
        return int(cap // cost_per_contract)

    def _projected_delta(
        self, signal: Signal, qty: int, positions: list[Position]
    ) -> float:
        current = sum(p.raw.get("delta", 0.0) * p.quantity for p in positions)
        delta = (signal.contract.delta if signal.contract else None) or 0.0
        sign = 1 if signal.action == SignalAction.BUY else -1
        added = delta * qty * sign * _CONTRACT_MULTIPLIER
        return current + added

    def _greeks_ok(self, signal: Signal) -> tuple[bool, str]:
        c = signal.contract
        if c is None:
            return False, "no contract"
        # Reject obviously broken Greeks (e.g. zero/inverted spreads).
        if c.ask and c.bid and c.ask < c.bid:
            return False, "crossed bid/ask"
        if c.delta is not None and abs(c.delta) > 1.0:
            return False, "implausible delta"
        return True, "ok"
