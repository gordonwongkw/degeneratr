"""Event-driven backtester with exit modeling and trade-level metrics.

At each underlying bar the loop:
  1. checks open trades for an exit (take-profit / stop-loss as a % of the
     option's entry premium, an end-of-day flatten, or a max-hold timeout),
  2. asks the strategy for new entry signals and opens positions.

Every entry therefore becomes a closed *round-trip* that can be classified a
win or a loss, which is what makes a success rate (win rate, profit factor,
expectancy, drawdown) computable. Fills route through :class:`PaperBroker` so
the commission/position accounting matches live paper trading.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from typing import Optional

from ..broker.base import OrderRequest, OrderSide, OrderType
from ..broker.paper import PaperBroker
from ..config import Settings, get_settings
from ..data.base import Bar, BarPeriod, IVAnalysis, MarketDataProvider, OptionContract
from ..data.factory import get_provider
from ..risk.manager import RiskManager
from ..strategies.base import Signal, SignalAction, Strategy, select_otm_contract

_CONTRACT_MULTIPLIER = 100


def _floor_minute(dt: datetime) -> datetime:
    """Drop sub-minute precision so option/underlying bar times align."""
    return dt.replace(second=0, microsecond=0)


@dataclass(slots=True)
class _OpenTrade:
    """An in-flight position the backtester is tracking for an exit."""

    signal: Signal
    option_id: str
    side: OrderSide          # side of the ENTRY order
    quantity: int
    entry_time: datetime
    entry_index: int
    entry_price: float
    last_price: float


@dataclass(slots=True)
class RoundTrip:
    """A completed entry+exit, classified win/loss."""

    symbol: str
    option_id: str
    side: OrderSide          # entry side
    quantity: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    pnl: float               # net of round-trip commission
    commission: float
    exit_reason: str
    strategy: str = ""
    pnl_pct: float = 0.0     # return for this trade (meaning depends on backtester)
    # Why the trade was opened — the signal(s) that triggered it.
    entry_reason: str = ""
    # Contract identity so the UI can show what was actually traded.
    strike: float = 0.0
    expiry: str = ""
    right: str = ""

    @property
    def win(self) -> bool:
        return self.pnl > 0


@dataclass(slots=True)
class BacktestResult:
    starting_cash: float
    ending_equity: float
    realized_pnl: float
    total_commission: float
    num_trades: int
    signals_generated: int = 0
    signals_rejected: int = 0
    round_trips: list[RoundTrip] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    # Underlying close series (time, price) — markers are plotted on this.
    price_series: list[tuple[datetime, float]] = field(default_factory=list)

    # ---- headline metrics ----
    @property
    def return_pct(self) -> float:
        if self.starting_cash == 0:
            return 0.0
        return (self.ending_equity - self.starting_cash) / self.starting_cash * 100.0

    @property
    def wins(self) -> list[RoundTrip]:
        return [r for r in self.round_trips if r.win]

    @property
    def losses(self) -> list[RoundTrip]:
        return [r for r in self.round_trips if not r.win]

    @property
    def win_rate(self) -> float:
        """Fraction of round-trips that were profitable, 0–1."""
        if not self.round_trips:
            return 0.0
        return len(self.wins) / len(self.round_trips)

    @property
    def avg_win(self) -> float:
        return mean([r.pnl for r in self.wins]) if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return mean([r.pnl for r in self.losses]) if self.losses else 0.0

    @property
    def profit_factor(self) -> float:
        """Gross profit / gross loss. inf when there are wins but no losses."""
        gross_win = sum(r.pnl for r in self.wins)
        gross_loss = -sum(r.pnl for r in self.losses)
        if gross_loss == 0:
            return float("inf") if gross_win > 0 else 0.0
        return gross_win / gross_loss

    @property
    def expectancy(self) -> float:
        """Average P&L per round-trip (the bottom-line edge)."""
        if not self.round_trips:
            return 0.0
        return mean([r.pnl for r in self.round_trips])

    @property
    def max_drawdown(self) -> float:
        """Largest peak-to-trough drop on the equity curve (absolute dollars)."""
        peak = float("-inf")
        max_dd = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd


class Backtester:
    """Replays bars, opens positions on signals and closes them on exit rules."""

    def __init__(
        self,
        strategy: Strategy,
        provider: Optional[MarketDataProvider] = None,
        settings: Optional[Settings] = None,
        commission_per_contract: Optional[float] = None,
        risk: Optional[RiskManager] = None,
        *,
        take_profit_pct: float = 0.50,
        stop_loss_pct: float = 0.50,
        max_hold_bars: int = 60,
        flatten_eod: bool = True,
        max_concurrent_positions: int = 5,
        cooldown_bars: int = 6,
    ) -> None:
        self._strategy = strategy
        self._provider = provider or get_provider()
        self._settings = settings or get_settings()
        # Same risk controls as live: sizing, per-trade/daily loss, delta caps.
        self._risk = risk or RiskManager(self._settings)
        # Commission model: configurable, defaults to Tiger/MooMoo-typical $0.65.
        self._commission = (
            commission_per_contract
            if commission_per_contract is not None
            else self._settings.commission_per_contract
        )
        # Exit rules, expressed against the option's entry premium.
        self._tp = take_profit_pct
        self._sl = stop_loss_pct
        self._max_hold = max_hold_bars
        self._flatten_eod = flatten_eod
        # Entry gating: cap simultaneous positions and throttle re-entry so a
        # static signal can't open a position on every single bar.
        self._max_concurrent = max_concurrent_positions
        self._cooldown_bars = cooldown_bars

    async def run(
        self,
        ticker: str,
        begin_time: datetime,
        end_time: datetime,
        period: BarPeriod = BarPeriod.ONE_MINUTE,
        warmup: int = 30,
        option_bar_cache: Optional[dict[str, dict[datetime, float]]] = None,
    ) -> BacktestResult:
        broker = PaperBroker(self._settings)
        broker._commission = self._commission  # honor the override
        round_trips: list[RoundTrip] = []
        equity_curve: list[tuple[datetime, float]] = []

        bars = await self._provider.get_bars(ticker, period, begin_time, end_time)
        if len(bars) <= warmup:
            account = await broker.get_account_info()
            return BacktestResult(
                starting_cash=self._settings.backtest_starting_cash,
                ending_equity=account.net_liquidation,
                realized_pnl=account.realized_pnl,
                total_commission=0.0,
                num_trades=0,
            )

        chain = await self._provider.get_option_chain(ticker)
        iv = await self._provider.get_iv_analysis(ticker)
        if option_bar_cache is None:
            option_bar_cache = {}
        open_trades: list[_OpenTrade] = []
        cooldown: dict[str, int] = {}  # option_id -> bar index until which entry is blocked
        signals_generated = 0
        signals_rejected = 0
        self._risk.reset_daily()
        current_day = bars[warmup].time.date()

        for i in range(warmup, len(bars)):
            window = bars[: i + 1]
            now = window[-1].time

            # Reset the daily kill-switch at each new session.
            if now.date() != current_day:
                self._risk.reset_daily()
                current_day = now.date()

            # ---- 1. exits first ----
            still_open: list[_OpenTrade] = []
            for trade in open_trades:
                price = await self._option_price(
                    trade.option_id, now, begin_time, end_time, period, option_bar_cache
                )
                if price and price > 0:
                    trade.last_price = price
                reason = self._exit_reason(trade, now, i)
                if reason is None:
                    still_open.append(trade)
                    continue
                rt = await self._close_trade(broker, trade, now, reason)
                round_trips.append(rt)
                # Feed realized P&L back so the daily kill-switch can trip.
                self._risk.record_realized_pnl(rt.pnl)
                cooldown[trade.option_id] = i + self._cooldown_bars
            open_trades = still_open

            # ---- 2. new entries (risk-gated) ----
            signals = await self._strategy.generate_signals(ticker, window, chain, iv)
            account = await broker.get_account_info()
            positions = await broker.get_positions()
            for signal in signals:
                signals_generated += 1

                # Execution: a price-action signal carries only a direction, so
                # select the closest OTM contract from the chain at the live spot.
                if signal.contract is None:
                    signal.contract = select_otm_contract(
                        chain, window[-1].close, signal.right
                    )
                if signal.contract is None:
                    signals_rejected += 1
                    continue

                price = await self._price_signal(
                    signal, now, begin_time, end_time, period, option_bar_cache
                )
                if price is None or price <= 0:
                    signals_rejected += 1
                    continue

                option_id = signal.contract.identifier if signal.contract else signal.ticker
                # Entry gates: concurrent-position cap and per-contract cooldown.
                if len(open_trades) >= self._max_concurrent:
                    signals_rejected += 1
                    continue
                if cooldown.get(option_id, -1) > i:
                    signals_rejected += 1
                    continue

                # Price the signal at the historical bar so risk sizes off the
                # real entry premium, then run the same gate live trading uses.
                signal.limit_price = round(price, 2)
                decision = self._risk.evaluate(signal, account, positions)
                if not decision.approved or decision.order is None:
                    signals_rejected += 1
                    continue

                order = self._open_order(signal, price, quantity=decision.quantity)
                filled = await broker.place_order(order)
                open_trades.append(
                    _OpenTrade(
                        signal=signal,
                        option_id=order.symbol,
                        side=order.side,
                        quantity=order.quantity,
                        entry_time=now,
                        entry_index=i,
                        entry_price=filled.avg_fill_price,
                        last_price=filled.avg_fill_price,
                    )
                )
                cooldown[option_id] = i + self._cooldown_bars

            account = await broker.get_account_info()
            equity_curve.append((now, account.net_liquidation))

        # ---- flatten anything still open at the end of the window ----
        final_time = bars[-1].time
        for trade in open_trades:
            rt = await self._close_trade(broker, trade, final_time, "end_of_data")
            round_trips.append(rt)

        account = await broker.get_account_info()
        total_commission = sum(r.commission for r in round_trips)
        return BacktestResult(
            starting_cash=self._settings.backtest_starting_cash,
            ending_equity=account.net_liquidation,
            realized_pnl=account.realized_pnl,
            total_commission=total_commission,
            num_trades=len(round_trips),
            signals_generated=signals_generated,
            signals_rejected=signals_rejected,
            round_trips=round_trips,
            equity_curve=equity_curve,
            price_series=[(b.time, b.close) for b in bars],
        )

    # ---- exit evaluation ------------------------------------------------
    def _exit_reason(self, trade: _OpenTrade, now: datetime, index: int) -> Optional[str]:
        """Return why ``trade`` should close now, or ``None`` to hold."""
        entry = trade.entry_price
        price = trade.last_price
        if entry > 0 and price > 0:
            # Return on the option premium, sign-adjusted for short positions.
            if trade.side == OrderSide.BUY:
                ret = price / entry - 1.0
            else:  # short premium profits when the option gets cheaper
                ret = 1.0 - price / entry
            if ret >= self._tp:
                return "take_profit"
            if ret <= -self._sl:
                return "stop_loss"

        if self._flatten_eod and now.date() != trade.entry_time.date():
            return "eod_flatten"
        if index - trade.entry_index >= self._max_hold:
            return "max_hold"
        return None

    async def _close_trade(
        self, broker: PaperBroker, trade: _OpenTrade, now: datetime, reason: str
    ) -> RoundTrip:
        exit_side = OrderSide.SELL if trade.side == OrderSide.BUY else OrderSide.BUY
        exit_price = trade.last_price
        exit_order = OrderRequest(
            symbol=trade.option_id,
            side=exit_side,
            quantity=trade.quantity,
            order_type=OrderType.LIMIT,
            limit_price=exit_price,
            client_tag=trade.signal.strategy,
            meta={"mark": exit_price, "exit_reason": reason},
        )
        await broker.place_order(exit_order)

        commission = self._commission * trade.quantity * 2  # entry + exit legs
        gross = (
            (exit_price - trade.entry_price)
            if trade.side == OrderSide.BUY
            else (trade.entry_price - exit_price)
        ) * trade.quantity * _CONTRACT_MULTIPLIER
        contract = trade.signal.contract
        return RoundTrip(
            symbol=trade.signal.ticker,
            option_id=trade.option_id,
            side=trade.side,
            quantity=trade.quantity,
            entry_time=trade.entry_time,
            entry_price=trade.entry_price,
            exit_time=now,
            exit_price=exit_price,
            pnl=gross - commission,
            commission=commission,
            exit_reason=reason,
            strategy=trade.signal.strategy,
            entry_reason=trade.signal.reason,
            strike=contract.strike if contract else 0.0,
            expiry=contract.expiry if contract else "",
            right=contract.right.value if contract else "",
        )

    # ---- pricing --------------------------------------------------------
    async def _option_price(
        self,
        option_id: str,
        now: datetime,
        begin_time: datetime,
        end_time: datetime,
        period: BarPeriod,
        cache: dict[str, dict[datetime, float]],
    ) -> Optional[float]:
        if not option_id:
            return None
        if option_id not in cache:
            obars = await self._provider.get_option_bars(
                [option_id], begin_time, end_time, period
            )
            # Tiger stamps option bars a few seconds off the underlying grid
            # (e.g. 23:30:09.552 vs 23:30:00), so key by the floored minute to
            # align them with the underlying bar timestamps.
            cache[option_id] = {
                _floor_minute(b.time): b.close for b in obars.get(option_id, [])
            }
        return cache[option_id].get(_floor_minute(now))

    async def _price_signal(
        self,
        signal: Signal,
        now: datetime,
        begin_time: datetime,
        end_time: datetime,
        period: BarPeriod,
        cache: dict[str, dict[datetime, float]],
    ) -> Optional[float]:
        # Only price off REAL historical option bars. If none exists at this
        # bar we return None and the trade is skipped — never fall back to the
        # current (today's) snapshot premium, which would be anachronistic.
        contract = signal.contract
        if contract is None:
            return None
        price = await self._option_price(
            contract.identifier, now, begin_time, end_time, period, cache
        )
        return price if (price is not None and price > 0) else None

    @staticmethod
    def _open_order(signal: Signal, price: float, quantity: Optional[int] = None) -> OrderRequest:
        return OrderRequest(
            symbol=(signal.contract.identifier if signal.contract else signal.ticker),
            side=OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL,
            quantity=quantity or signal.quantity or 1,
            order_type=OrderType.LIMIT,
            limit_price=price,
            client_tag=signal.strategy,
            meta={"mark": price},
        )
