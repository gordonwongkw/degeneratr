"""Underlying-only backtester — P&L derived purely from the stock price.

Signals come from the underlying's price action. A buy signal goes long the
direction (the call you'd buy), a sell signal goes short (the put). P&L is just
the stock's move × position size — no options, no model, no options data.

Position size is risk-based: each trade risks ``max_loss_per_trade`` (capped to a
fraction of equity), so shares = risk / stop-distance, with a leverage cap.

Produces the same :class:`BacktestResult` shape as the other backtesters so the
API / dashboard work unchanged. Round-trips carry the underlying entry/exit
prices, share quantity, and the % move as ``pnl_pct``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..config import Settings, get_settings
from ..data.base import Bar, BarPeriod, IVAnalysis, MarketDataProvider, OptionRight
from ..data.factory import get_provider
from ..risk.manager import RiskManager
from ..strategies.base import Signal, Strategy
from .engine import BacktestResult, RoundTrip


@dataclass(slots=True)
class _Open:
    signal: Signal
    direction: int          # +1 long (call), -1 short (put)
    shares: int
    entry_time: datetime
    entry_index: int
    entry_price: float


class UnderlyingBacktester:
    def __init__(
        self,
        strategy: Strategy,
        provider: Optional[MarketDataProvider] = None,
        settings: Optional[Settings] = None,
        risk: Optional[RiskManager] = None,
        *,
        take_profit_pct: float = 0.005,   # +move in the underlying to take profit
        stop_loss_pct: float = 0.003,     # adverse move to stop out
        max_hold_bars: int = 24,
        max_concurrent_positions: int = 5,
        cooldown_bars: int = 6,
        max_leverage: float = 4.0,
        gap_minutes: int = 60,
        **_ignored,                       # tolerate extra kwargs (e.g. iv) from callers
    ) -> None:
        self._strategy = strategy
        self._provider = provider or get_provider()
        self._settings = settings or get_settings()
        self._risk = risk or RiskManager(self._settings)
        self._tp = take_profit_pct
        self._sl = stop_loss_pct
        self._max_hold = max_hold_bars
        self._max_concurrent = max_concurrent_positions
        self._cooldown_bars = cooldown_bars
        self._max_leverage = max_leverage
        self._gap_minutes = gap_minutes

    def _session_lasts(self, bars: list[Bar]) -> set[int]:
        n = len(bars)
        out: set[int] = set()
        for i in range(n):
            gap = (bars[i + 1].time - bars[i].time).total_seconds() / 60 if i + 1 < n else 1e9
            if gap > self._gap_minutes:
                out.add(i)
        return out

    async def run(
        self,
        ticker: str,
        begin_time: datetime,
        end_time: datetime,
        period: BarPeriod = BarPeriod.FIVE_MINUTES,
        warmup: int = 30,
    ) -> BacktestResult:
        start_cash = float(self._settings.backtest_starting_cash)
        bars = await self._provider.get_bars(ticker, period, begin_time, end_time)
        if len(bars) <= warmup:
            return BacktestResult(
                starting_cash=start_cash, ending_equity=start_cash,
                realized_pnl=0.0, total_commission=0.0, num_trades=0,
            )

        session_last = self._session_lasts(bars)
        empty_iv = IVAnalysis(symbol=ticker)
        cash = start_cash
        realized = 0.0
        open_trades: list[_Open] = []
        round_trips: list[RoundTrip] = []
        equity_curve: list[tuple[datetime, float]] = []
        cooldown: dict[str, int] = {}
        signals_generated = 0
        signals_rejected = 0
        self._risk.reset_daily()
        current_day = bars[warmup].time.date()

        for i in range(warmup, len(bars)):
            now = bars[i].time
            spot = bars[i].close
            is_session_end = i in session_last
            if now.date() != current_day:
                self._risk.reset_daily()
                current_day = now.date()

            # ---- exits ----
            still: list[_Open] = []
            for tr in open_trades:
                move = (spot / tr.entry_price - 1.0) * tr.direction
                reason = None
                if is_session_end:
                    reason = "session_close"
                elif move >= self._tp:
                    reason = "take_profit"
                elif move <= -self._sl:
                    reason = "stop_loss"
                elif i - tr.entry_index >= self._max_hold:
                    reason = "max_hold"
                if reason is None:
                    still.append(tr)
                    continue
                pnl = (spot - tr.entry_price) * tr.direction * tr.shares
                cash += pnl
                realized += pnl
                self._risk.record_realized_pnl(pnl)
                round_trips.append(self._round_trip(tr, now, spot, pnl, reason))
                key = self._key(tr.signal)
                cooldown[key] = i + self._cooldown_bars
            open_trades = still

            # ---- entries ----
            if not is_session_end and not self._risk.kill_switch_tripped():
                signals = await self._strategy.generate_signals(ticker, bars[: i + 1], [], empty_iv)
                equity = cash + sum(
                    (spot - t.entry_price) * t.direction * t.shares for t in open_trades
                )
                for sig in signals:
                    signals_generated += 1
                    key = self._key(sig)
                    if len(open_trades) >= self._max_concurrent or cooldown.get(key, -1) > i:
                        signals_rejected += 1
                        continue
                    shares = self._size(sig, spot, equity)
                    if shares < 1:
                        signals_rejected += 1
                        continue
                    direction = 1 if sig.right == OptionRight.CALL else -1
                    open_trades.append(_Open(
                        signal=sig, direction=direction, shares=shares,
                        entry_time=now, entry_index=i, entry_price=spot,
                    ))
                    cooldown[key] = i + self._cooldown_bars

            equity = cash + sum(
                (spot - t.entry_price) * t.direction * t.shares for t in open_trades
            )
            equity_curve.append((now, equity))

        # settle open trades at the last bar
        final = bars[-1]
        for tr in open_trades:
            pnl = (final.close - tr.entry_price) * tr.direction * tr.shares
            cash += pnl
            realized += pnl
            round_trips.append(self._round_trip(tr, final.time, final.close, pnl, "session_close"))

        return BacktestResult(
            starting_cash=start_cash, ending_equity=cash, realized_pnl=realized,
            total_commission=0.0, num_trades=len(round_trips),
            signals_generated=signals_generated, signals_rejected=signals_rejected,
            round_trips=round_trips, equity_curve=equity_curve,
            price_series=[(b.time, b.close) for b in bars],
        )

    # ---- helpers ----
    @staticmethod
    def _key(sig: Signal) -> str:
        return f"{sig.ticker}-{sig.right.value}"

    def _size(self, sig: Signal, spot: float, equity: float) -> int:
        """Risk-based shares: risk the per-trade budget over the stop distance."""
        stop_dist = spot * self._sl
        if stop_dist <= 0:
            return 0
        budget = min(
            self._settings.risk_max_loss_per_trade,
            equity * self._settings.risk_per_trade_fraction * max(sig.confidence, 0.2),
        )
        shares = math.floor(budget / stop_dist)
        # Leverage cap on notional.
        cap = math.floor(equity * self._max_leverage / spot)
        return max(0, min(shares, cap))

    def _round_trip(
        self, tr: _Open, exit_time: datetime, exit_price: float, pnl: float, reason: str
    ) -> RoundTrip:
        from ..broker.base import OrderSide

        move_pct = (exit_price / tr.entry_price - 1.0) * tr.direction * 100.0
        return RoundTrip(
            symbol=tr.signal.ticker, option_id=self._key(tr.signal),
            side=OrderSide.BUY if tr.direction > 0 else OrderSide.SELL,
            quantity=tr.shares, entry_time=tr.entry_time, entry_price=round(tr.entry_price, 2),
            exit_time=exit_time, exit_price=round(exit_price, 2), pnl=round(pnl, 2),
            commission=0.0, exit_reason=reason, strategy=tr.signal.strategy,
            pnl_pct=round(move_pct, 3), entry_reason=tr.signal.reason,
            strike=0.0, expiry="", right=tr.signal.right.value,
        )
