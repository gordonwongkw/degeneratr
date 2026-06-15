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

import pandas as pd
import pandas_ta as ta

from ..data.base import Bar, IVAnalysis, OptionContract, OptionRight
from ..indicators.technical import bars_to_frame, bollinger, ema, macd, rsi, vwap
from .base import Signal, SignalAction, Strategy


class PriceActionStrategy(Strategy):
    name = "degeneratr"

    def __init__(
        self,
        *,
        # Defaults = the most robust sweep config from the 60-day yfinance sweep
        # (best risk-adjusted at 15m: highest profit factor, win rate and lowest
        # drawdown): min_score 2, EMA 9/21, breakout BB, paired with a symmetric
        # 0.4%/0.4% take-profit/stop in the underlying backtester at 15m bars.
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

    def series(self, bars: list[Bar]) -> dict:
        """Vectorized indicator + per-bar signal series for charting.

        Computes the same votes as :meth:`generate_signals` over the whole
        series at once. Because every indicator here is causal (EMA, MACD, RSI,
        Bollinger, cumulative VWAP only use data up to each bar), the value at
        bar ``i`` equals what ``generate_signals(bars[:i+1])`` would see — so
        this is a faithful, O(n) reconstruction of the live signals. The
        ``series_signals`` test asserts they match.
        """
        n = len(bars)
        empty = [None] * n
        if n == 0:
            return {"ema_fast": [], "ema_slow": [], "vwap": [], "bb_upper": [],
                    "bb_lower": [], "score": [], "votes": []}
        f = bars_to_frame(bars)
        close = f["close"]

        def col(series):
            return [None if pd.isna(v) else float(v) for v in series]

        ef = ta.ema(close, length=self.ema_fast) if self.use_ema else pd.Series([pd.NA] * n)
        es = ta.ema(close, length=self.ema_slow) if self.use_ema else pd.Series([pd.NA] * n)
        tp = (f["high"] + f["low"] + f["close"]) / 3.0
        cv = f["volume"].cumsum()
        vw = (tp * f["volume"]).cumsum() / cv.replace(0, pd.NA)
        macd_df = ta.macd(close, fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal)
        hist = macd_df.iloc[:, 1] if macd_df is not None and not macd_df.empty else pd.Series([pd.NA] * n, index=close.index)
        r = ta.rsi(close, length=self.rsi_len)
        bb = ta.bbands(close, length=self.bb_len, std=self.bb_std)
        if bb is not None and not bb.empty:
            bb_lower, bb_upper = bb.iloc[:, 0], bb.iloc[:, 2]
        else:
            bb_lower = bb_upper = pd.Series([pd.NA] * n, index=close.index)

        scores: list[int] = []
        votes: list[list[str]] = []
        ef_l, es_l, vw_l, h_l, r_l = list(ef), list(es), list(vw), list(hist), list(r)
        up_l, lo_l, cl_l = list(bb_upper), list(bb_lower), list(close)
        for i in range(n):
            bull = bear = 0
            v: list[str] = []
            if self.use_ema and pd.notna(ef_l[i]) and pd.notna(es_l[i]):
                if ef_l[i] > es_l[i]: bull += 1; v.append("EMA↑")
                elif ef_l[i] < es_l[i]: bear += 1; v.append("EMA↓")
            if self.use_vwap and pd.notna(vw_l[i]):
                if cl_l[i] > vw_l[i]: bull += 1; v.append("VWAP↑")
                elif cl_l[i] < vw_l[i]: bear += 1; v.append("VWAP↓")
            if self.use_macd and pd.notna(h_l[i]):
                if h_l[i] > 0: bull += 1; v.append("MACD+")
                elif h_l[i] < 0: bear += 1; v.append("MACD-")
            if self.use_rsi and pd.notna(r_l[i]):
                if r_l[i] >= self.rsi_bull: bull += 1; v.append("RSI↑")
                elif r_l[i] <= self.rsi_bear: bear += 1; v.append("RSI↓")
            if self.use_bbands and pd.notna(up_l[i]) and pd.notna(lo_l[i]):
                if self.bb_mode == "breakout":
                    if cl_l[i] > up_l[i]: bull += 1; v.append("BB↑")
                    elif cl_l[i] < lo_l[i]: bear += 1; v.append("BB↓")
                else:
                    if cl_l[i] <= lo_l[i]: bull += 1; v.append("BB↑")
                    elif cl_l[i] >= up_l[i]: bear += 1; v.append("BB↓")
            scores.append(0 if i < self.min_bars - 1 else bull - bear)
            votes.append(v)

        return {
            "ema_fast": col(ef), "ema_slow": col(es), "vwap": col(vw),
            "bb_upper": col(bb_upper), "bb_lower": col(bb_lower),
            "score": scores, "votes": votes,
        }

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
