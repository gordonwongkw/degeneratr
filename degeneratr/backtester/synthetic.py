"""Synthetic 0DTE backtester — prices options with a model, no options data.

Signals come from the underlying's price action. On a buy signal the bot buys
the closest-OTM **0DTE** call; on a sell signal, the closest-OTM 0DTE put. The
option is priced with Black-Scholes (py_vollib) from the underlying price,
the modelled time-to-expiry (the option expires at that session's close), and a
volatility input — so the backtest needs only underlying bars.

Produces the same :class:`BacktestResult` shape as the option-bar backtester, so
the API / dashboard work unchanged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
from py_vollib.black_scholes import black_scholes
from py_vollib.black_scholes.greeks.analytical import delta as bs_delta

from ..broker.base import OrderRequest, OrderSide, OrderType
from ..broker.paper import PaperBroker
from ..config import Settings, get_settings
from ..data.base import (
    Bar,
    BarPeriod,
    IVAnalysis,
    MarketDataProvider,
    OptionContract,
    OptionRight,
)
from ..data.factory import get_provider
from ..risk.manager import RiskManager
from ..strategies.base import Signal, SignalAction, Strategy
from .engine import BacktestResult, RoundTrip

_CONTRACT_MULTIPLIER = 100
_SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0
_MIN_T = 30.0 / _SECONDS_PER_YEAR  # 30s floor so BS never divides by zero

_PERIOD_MINUTES = {
    BarPeriod.ONE_MINUTE: 1, BarPeriod.FIVE_MINUTES: 5, BarPeriod.FIFTEEN_MINUTES: 15,
    BarPeriod.THIRTY_MINUTES: 30, BarPeriod.ONE_HOUR: 60, BarPeriod.ONE_DAY: 390,
}


def _intrinsic(right: OptionRight, spot: float, strike: float) -> float:
    return max(0.0, spot - strike) if right == OptionRight.CALL else max(0.0, strike - spot)


def _bs_price(right: OptionRight, spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    flag = "c" if right == OptionRight.CALL else "p"
    t = max(t, _MIN_T)
    sigma = max(sigma, 1e-4)
    try:
        return max(0.0, float(black_scholes(flag, spot, strike, t, r, sigma)))
    except Exception:  # noqa: BLE001 - fall back to intrinsic on any model error
        return _intrinsic(right, spot, strike)


def _bs_delta(right: OptionRight, spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    flag = "c" if right == OptionRight.CALL else "p"
    try:
        return float(bs_delta(flag, spot, strike, max(t, _MIN_T), r, max(sigma, 1e-4)))
    except Exception:  # noqa: BLE001
        return 0.5 if right == OptionRight.CALL else -0.5


def _otm_strike(spot: float, right: OptionRight, inc: float) -> float:
    """Closest out-of-the-money strike on the ``inc`` grid."""
    if right == OptionRight.CALL:
        return math.floor(spot / inc + 1e-9) * inc + inc
    return math.ceil(spot / inc - 1e-9) * inc - inc


@dataclass(slots=True)
class _Open:
    signal: Signal
    right: OptionRight
    strike: float
    expiry: str
    symbol: str
    quantity: int
    entry_time: datetime
    entry_index: int
    entry_price: float
    last_price: float


class SyntheticBacktester:
    def __init__(
        self,
        strategy: Strategy,
        provider: Optional[MarketDataProvider] = None,
        settings: Optional[Settings] = None,
        risk: Optional[RiskManager] = None,
        commission_per_contract: Optional[float] = None,
        *,
        iv: Optional[float] = None,           # annualized vol; None → estimate from bars
        iv_scale: float = 1.0,                # multiplier on estimated realized vol
        risk_free_rate: float = 0.04,
        strike_increment: float = 1.0,
        take_profit_pct: float = 0.50,
        stop_loss_pct: float = 0.50,
        max_hold_bars: int = 24,
        max_concurrent_positions: int = 5,
        cooldown_bars: int = 6,
        gap_minutes: int = 60,
    ) -> None:
        self._strategy = strategy
        self._provider = provider or get_provider()
        self._settings = settings or get_settings()
        self._risk = risk or RiskManager(self._settings)
        self._commission = (
            commission_per_contract if commission_per_contract is not None
            else self._settings.commission_per_contract
        )
        self._iv = iv
        self._iv_scale = iv_scale
        self._r = risk_free_rate
        self._inc = strike_increment
        self._tp = take_profit_pct
        self._sl = stop_loss_pct
        self._max_hold = max_hold_bars
        self._max_concurrent = max_concurrent_positions
        self._cooldown_bars = cooldown_bars
        self._gap_minutes = gap_minutes

    # ---- volatility estimate ----
    def _estimate_iv(self, bars: list[Bar], period: BarPeriod) -> float:
        closes = np.array([b.close for b in bars], dtype=float)
        if len(closes) < 3:
            return 0.20
        rets = np.diff(np.log(closes))
        per_year = 252.0 * (390.0 / _PERIOD_MINUTES.get(period, 5))
        sigma = float(np.std(rets) * math.sqrt(per_year)) * self._iv_scale
        return max(0.05, min(sigma, 3.0))  # clamp to a sane band

    # ---- session boundaries (a 0DTE option expires at its session close) ----
    def _session_ends(self, bars: list[Bar]) -> tuple[list[datetime], set[int]]:
        n = len(bars)
        last_idx: list[int] = []
        for i in range(n):
            gap = (bars[i + 1].time - bars[i].time).total_seconds() / 60 if i + 1 < n else 1e9
            if gap > self._gap_minutes:
                last_idx.append(i)
        ends: list[datetime] = [bars[-1].time] * n
        li = 0
        for i in range(n):
            while li < len(last_idx) and last_idx[li] < i:
                li += 1
            ends[i] = bars[last_idx[li]].time if li < len(last_idx) else bars[-1].time
        return ends, set(last_idx)

    async def run(
        self,
        ticker: str,
        begin_time: datetime,
        end_time: datetime,
        period: BarPeriod = BarPeriod.FIVE_MINUTES,
        warmup: int = 30,
    ) -> BacktestResult:
        broker = PaperBroker(self._settings)
        broker._commission = self._commission
        bars = await self._provider.get_bars(ticker, period, begin_time, end_time)
        if len(bars) <= warmup:
            acct = await broker.get_account_info()
            return BacktestResult(
                starting_cash=self._settings.backtest_starting_cash,
                ending_equity=acct.net_liquidation, realized_pnl=acct.realized_pnl,
                total_commission=0.0, num_trades=0,
            )

        sigma = self._iv if self._iv is not None else self._estimate_iv(bars, period)
        session_end, session_last = self._session_ends(bars)
        empty_iv = IVAnalysis(symbol=ticker)

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
            is_expiry = i in session_last
            t_years = max((session_end[i] - now).total_seconds() / _SECONDS_PER_YEAR, _MIN_T)

            if now.date() != current_day:
                self._risk.reset_daily()
                current_day = now.date()

            # ---- 1. mark + exits ----
            still: list[_Open] = []
            for tr in open_trades:
                premium = (
                    _intrinsic(tr.right, spot, tr.strike) if is_expiry
                    else _bs_price(tr.right, spot, tr.strike, t_years, self._r, sigma)
                )
                tr.last_price = premium
                reason = self._exit_reason(tr, premium, i, is_expiry)
                if reason is None:
                    still.append(tr)
                    continue
                rt = await self._close(broker, tr, now, premium, reason)
                round_trips.append(rt)
                self._risk.record_realized_pnl(rt.pnl)
                cooldown[tr.symbol] = i + self._cooldown_bars
            open_trades = still

            # ---- 2. entries (skip the expiry bar — no time left) ----
            if not is_expiry:
                signals = await self._strategy.generate_signals(ticker, bars[: i + 1], [], empty_iv)
                account = await broker.get_account_info()
                positions = await broker.get_positions()
                for sig in signals:
                    signals_generated += 1
                    strike = _otm_strike(spot, sig.right, self._inc)
                    premium = _bs_price(sig.right, spot, strike, t_years, self._r, sigma)
                    if premium <= 0.02:
                        signals_rejected += 1
                        continue
                    expiry = session_end[i].date().isoformat()
                    symbol = f"{ticker}-{sig.right.value}-{strike:g}-{expiry}"
                    if len(open_trades) >= self._max_concurrent or cooldown.get(symbol, -1) > i:
                        signals_rejected += 1
                        continue
                    # Hand the risk manager a modelled contract so sizing + delta work.
                    sig.contract = OptionContract(
                        symbol=ticker, identifier=symbol, expiry=expiry, strike=strike,
                        right=sig.right, last=round(premium, 2),
                        delta=_bs_delta(sig.right, spot, strike, t_years, self._r, sigma),
                    )
                    sig.limit_price = round(premium, 2)
                    decision = self._risk.evaluate(sig, account, positions)
                    if not decision.approved or decision.order is None:
                        signals_rejected += 1
                        continue
                    filled = await broker.place_order(OrderRequest(
                        symbol=symbol, side=OrderSide.BUY, quantity=decision.quantity,
                        order_type=OrderType.LIMIT, limit_price=round(premium, 2),
                        client_tag=sig.strategy, meta={"mark": premium},
                    ))
                    open_trades.append(_Open(
                        signal=sig, right=sig.right, strike=strike, expiry=expiry, symbol=symbol,
                        quantity=decision.quantity, entry_time=now, entry_index=i,
                        entry_price=filled.avg_fill_price, last_price=filled.avg_fill_price,
                    ))
                    cooldown[symbol] = i + self._cooldown_bars

            acct = await broker.get_account_info()
            equity_curve.append((now, acct.net_liquidation))

        # settle anything still open at the final bar (expiry intrinsic)
        final_spot = bars[-1].close
        for tr in open_trades:
            premium = _intrinsic(tr.right, final_spot, tr.strike)
            round_trips.append(await self._close(broker, tr, bars[-1].time, premium, "expiry"))

        acct = await broker.get_account_info()
        return BacktestResult(
            starting_cash=self._settings.backtest_starting_cash,
            ending_equity=acct.net_liquidation, realized_pnl=acct.realized_pnl,
            total_commission=sum(r.commission for r in round_trips),
            num_trades=len(round_trips),
            signals_generated=signals_generated, signals_rejected=signals_rejected,
            round_trips=round_trips, equity_curve=equity_curve,
            price_series=[(b.time, b.close) for b in bars],
        )

    def _exit_reason(self, tr: _Open, premium: float, index: int, is_expiry: bool) -> Optional[str]:
        if is_expiry:
            return "expiry"
        if tr.entry_price > 0:
            ret = premium / tr.entry_price - 1.0  # long premium
            if ret >= self._tp:
                return "take_profit"
            if ret <= -self._sl:
                return "stop_loss"
        if index - tr.entry_index >= self._max_hold:
            return "max_hold"
        return None

    async def _close(self, broker: PaperBroker, tr: _Open, now: datetime, premium: float, reason: str) -> RoundTrip:
        await broker.place_order(OrderRequest(
            symbol=tr.symbol, side=OrderSide.SELL, quantity=tr.quantity,
            order_type=OrderType.LIMIT, limit_price=premium, meta={"mark": premium},
        ))
        commission = self._commission * tr.quantity * 2
        gross = (premium - tr.entry_price) * tr.quantity * _CONTRACT_MULTIPLIER
        return RoundTrip(
            symbol=tr.signal.ticker, option_id=tr.symbol, side=OrderSide.BUY, quantity=tr.quantity,
            entry_time=tr.entry_time, entry_price=tr.entry_price, exit_time=now,
            exit_price=round(premium, 2), pnl=gross - commission, commission=commission,
            exit_reason=reason, strategy=tr.signal.strategy, entry_reason=tr.signal.reason,
            strike=tr.strike, expiry=tr.expiry, right=tr.right.value,
        )
