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
from ..marketclock import CLOSE_MIN
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
    peak: float = 0.0        # best favorable move so far (for breakeven/trailing)


class UnderlyingBacktester:
    def __init__(
        self,
        strategy: Strategy,
        provider: Optional[MarketDataProvider] = None,
        settings: Optional[Settings] = None,
        risk: Optional[RiskManager] = None,
        *,
        take_profit_pct: float = 0.004,   # +move in the underlying to take profit
        stop_loss_pct: float = 0.004,     # adverse move to stop out
        max_hold_bars: int = 24,
        max_concurrent_positions: int = 5,
        cooldown_bars: int = 6,
        max_leverage: float = 4.0,
        gap_minutes: int = 60,
        breakeven_after: float = 0.0,     # once +this move, stop moves to entry (0 = off)
        trail_pct: float = 0.0,           # trail stop this far below peak once active (0 = off)
        edge_window: int = 0,             # circuit breaker: look back this many trades (0 = off)
        edge_cooldown: int = 0,           # ...pause new entries this many bars when they're net-losing
        size_mode: str = "linear",        # position sizing: flat | linear | capped | peak4
        size_weights: Optional[dict] = None,  # explicit {score: factor} (overrides size_mode)
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
        self._breakeven = breakeven_after
        self._trail = trail_pct
        self._edge_window = edge_window
        self._edge_cooldown = edge_cooldown
        self._size_mode = size_mode
        self._size_weights = size_weights

    def _session_lasts(self, bars: list[Bar]) -> set[int]:
        """Indices where a trading session ends *within the data* — i.e. the NEXT
        bar is a different calendar day. Detecting the boundary by DATE (not a
        fixed time gap) means an intraday hole in the data (missing bars — common
        with live/Tiger fetches) isn't mislabeled as a mid-day 'session_close'.

        The final bar is intentionally NOT included: it's the end of the data
        *window*, not necessarily a session. A position still open there is settled
        separately and flagged 'open' when it's the current live session (see the
        settle step in :meth:`run`)."""
        out: set[int] = set()
        for i in range(len(bars) - 1):
            if bars[i + 1].time.date() != bars[i].time.date():
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
        recent_pnls: list[float] = []   # circuit breaker: this ticker's recent trade P&Ls
        pause_until = -1                # ...block new entries through this bar index
        self._risk.reset_daily()
        current_day = bars[warmup].time.date()

        for i in range(warmup, len(bars)):
            now = bars[i].time
            spot = bars[i].close
            is_session_end = i in session_last
            closed_this_bar = False
            if now.date() != current_day:
                self._risk.reset_daily()
                current_day = now.date()

            # ---- exits ----
            still: list[_Open] = []
            for tr in open_trades:
                move = (spot / tr.entry_price - 1.0) * tr.direction
                if move > tr.peak:
                    tr.peak = move
                reason = None
                if is_session_end:
                    reason = "session_close"
                elif move >= self._tp:
                    reason = "take_profit"
                elif move <= -self._sl:
                    reason = "stop_loss"
                elif self._trail > 0 and tr.peak >= self._trail and move <= tr.peak - self._trail:
                    reason = "trail_stop"
                elif self._breakeven > 0 and tr.peak >= self._breakeven and move <= 0:
                    reason = "breakeven"
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
                recent_pnls.append(pnl)
                closed_this_bar = True
                key = self._key(tr.signal)
                cooldown[key] = i + self._cooldown_bars
            open_trades = still

            # ---- circuit breaker: pause entries after a cold streak ----
            # Re-checked only when a trade just closed (so the window can't freeze
            # us out permanently); a probe trade after the cooldown re-tests edge.
            if (closed_this_bar and self._edge_window
                    and len(recent_pnls) >= self._edge_window
                    and sum(recent_pnls[-self._edge_window:]) < 0):
                pause_until = i + self._edge_cooldown

            # ---- entries ----
            # Don't open on the very last bar — it's no longer treated as a session
            # end, so an entry there would be a zero-duration trade settled the same
            # bar. (Matches the old behaviour, where the last bar blocked entries.)
            if (not is_session_end and i < len(bars) - 1
                    and not self._risk.kill_switch_tripped() and i > pause_until):
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

        # Settle anything still open at the end of the data window. It's only a real
        # end-of-day 'session_close' if the data actually reaches the bell (~15:45+
        # ET). If the data ends mid-session — the live current bar, or a stale /
        # truncated day — the position's true exit is unknown, so flag it 'open'
        # rather than pretend it closed at the close.
        final = bars[-1]
        end_minute = final.time.hour * 60 + final.time.minute
        end_reason = "session_close" if end_minute >= CLOSE_MIN - 20 else "open"
        for tr in open_trades:
            pnl = (final.close - tr.entry_price) * tr.direction * tr.shares
            cash += pnl
            realized += pnl
            round_trips.append(self._round_trip(tr, final.time, final.close, pnl, end_reason))

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
        """Risk-based shares: risk the per-trade budget over the stop distance.

        ``size_mode`` shapes the budget by signal confluence (score 2–5):
          flat   — same size every trade (ignore confidence)
          linear — budget ∝ confidence (more conviction → bigger; default)
          capped — like linear but capped at score 4 (don't over-size score 5,
                   which backtests as the worst bucket)
          peak4  — boost score 4, cut score 5 back to the score-2 level
        """
        stop_dist = spot * self._sl
        if stop_dist <= 0:
            return 0
        score = int(sig.meta.get("score", 2)) if sig.meta else 2
        if self._size_weights is not None:
            factor = self._size_weights.get(score, 0.7)
        elif self._size_mode == "flat":
            factor = 0.7
        elif self._size_mode == "capped":
            factor = min(max(sig.confidence, 0.2), 0.9)
        elif self._size_mode == "peak4":
            factor = {2: 0.7, 3: 0.8, 4: 1.0, 5: 0.7}.get(score, 0.7)
        else:  # linear
            factor = max(sig.confidence, 0.2)
        budget = min(
            self._settings.risk_max_loss_per_trade,
            equity * self._settings.risk_per_trade_fraction * factor,
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
