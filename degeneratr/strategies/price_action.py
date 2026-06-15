"""degeneratr — a price-action confluence algorithm.

Signals are derived **only** from the underlying's price action via technical
indicators (EMA, VWAP, MACD, RSI, Bollinger Bands). Options data is NOT used to
trigger trades. Each enabled indicator casts a bullish or bearish vote; when the
net vote reaches ``min_score`` the algorithm emits a directional signal:

    bullish  → BUY the closest out-of-the-money CALL
    bearish  → BUY the closest out-of-the-money PUT

Contract selection (closest OTM) happens downstream in the backtester / engine,
so this strategy returns signals with ``contract=None`` and only sets the
direction (``right``).

Every indicator and its parameters is configurable so the configuration can be
swept and optimized.
"""
from __future__ import annotations

from typing import Optional

from ..data.base import Bar, IVAnalysis, OptionContract, OptionRight
from ..indicators.technical import bars_to_frame, bollinger, ema, macd, rsi, vwap
from .base import Signal, SignalAction, Strategy


class PriceActionStrategy(Strategy):
    name = "degeneratr"

    def __init__(
        self,
        *,
        # Defaults = the most robust sweep config (the only one profitable on
        # SPY, QQQ and AAPL): min_score 2, EMA 9/21, breakout BB, paired with a
        # 0.8%/0.4% take-profit/stop in the underlying backtester.
        ema_fast: int = 9,
        ema_slow: int = 21,
        rsi_len: int = 14,
        rsi_bull: float = 54.0,
        rsi_bear: float = 46.0,
        bb_len: int = 20,
        bb_std: float = 2.0,
        bb_mode: str = "breakout",  # "breakout" or "reversion"
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        use_ema: bool = True,
        use_vwap: bool = True,
        use_macd: bool = True,
        use_rsi: bool = True,
        use_bbands: bool = True,
        min_score: int = 2,
        min_bars: int = 30,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_len = rsi_len
        self.rsi_bull = rsi_bull
        self.rsi_bear = rsi_bear
        self.bb_len = bb_len
        self.bb_std = bb_std
        self.bb_mode = bb_mode
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.use_ema = use_ema
        self.use_vwap = use_vwap
        self.use_macd = use_macd
        self.use_rsi = use_rsi
        self.use_bbands = use_bbands
        self.min_score = min_score
        self.min_bars = min_bars

    async def generate_signals(
        self,
        ticker: str,
        bars: list[Bar],
        option_chain: list[OptionContract],  # unused: signals are price-action only
        iv_analysis: IVAnalysis,             # unused
    ) -> list[Signal]:
        if len(bars) < self.min_bars:
            return []
        frame = bars_to_frame(bars)
        if frame.empty:
            return []
        close = float(frame["close"].iloc[-1])

        bull = 0
        bear = 0
        votes: list[str] = []

        if self.use_ema:
            ef = ema(frame, self.ema_fast)
            es = ema(frame, self.ema_slow)
            if ef is not None and es is not None:
                if ef > es:
                    bull += 1; votes.append("EMA↑")
                elif ef < es:
                    bear += 1; votes.append("EMA↓")

        if self.use_vwap:
            vw = vwap(frame)
            if vw is not None:
                if close > vw:
                    bull += 1; votes.append("VWAP↑")
                elif close < vw:
                    bear += 1; votes.append("VWAP↓")

        if self.use_macd:
            m = macd(frame, self.macd_fast, self.macd_slow, self.macd_signal)
            h = m["histogram"]
            if h is not None:
                if h > 0:
                    bull += 1; votes.append("MACD+")
                elif h < 0:
                    bear += 1; votes.append("MACD-")

        if self.use_rsi:
            r = rsi(frame, self.rsi_len)["value"]
            if r is not None:
                if r >= self.rsi_bull:
                    bull += 1; votes.append(f"RSI {r:.0f}↑")
                elif r <= self.rsi_bear:
                    bear += 1; votes.append(f"RSI {r:.0f}↓")

        if self.use_bbands:
            bb = bollinger(frame, self.bb_len, self.bb_std)
            up, lo = bb["upper"], bb["lower"]
            if up is not None and lo is not None:
                if self.bb_mode == "breakout":
                    if close > up:
                        bull += 1; votes.append("BB breakout↑")
                    elif close < lo:
                        bear += 1; votes.append("BB breakout↓")
                else:  # reversion: fade the band
                    if close <= lo:
                        bull += 1; votes.append("BB reversion↑")
                    elif close >= up:
                        bear += 1; votes.append("BB reversion↓")

        net = bull - bear
        if net >= self.min_score:
            return [self._signal(ticker, OptionRight.CALL, net, votes, close)]
        if -net >= self.min_score:
            return [self._signal(ticker, OptionRight.PUT, -net, votes, close)]
        return []

    def _signal(
        self, ticker: str, right: OptionRight, score: int, votes: list[str], spot: float
    ) -> Signal:
        direction = "bullish" if right == OptionRight.CALL else "bearish"
        return Signal(
            ticker=ticker,
            action=SignalAction.BUY,        # we always BUY premium (call or put)
            right=right,
            confidence=min(0.95, 0.5 + 0.1 * score),
            contract=None,                  # execution layer selects closest OTM
            quantity=0,                     # 0 = let the risk manager size the position
            reason=f"{direction} confluence {score}: {', '.join(votes)}",
            strategy=self.name,
            meta={"spot": spot, "score": score},
        )
