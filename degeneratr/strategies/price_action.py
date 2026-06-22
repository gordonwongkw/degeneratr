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
from ..indicators.technical import bars_to_frame
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
        # ---- success-rate filters ----
        # ADX(15) regime gate is ON by default: a 60-day out-of-sample test
        # showed it's the one filter that robustly lifts win rate (52.7→54.3%),
        # expectancy and profit factor by skipping chop. The others (trend EMA,
        # time-of-day, breakeven/trailing) did NOT generalize out-of-sample and
        # stay off. See scripts/sweep_improve.py.
        adx_len: int = 14,
        adx_min: float = 15.0,       # regime: require ADX >= this (0 = off)
        ema_trend: int = 0,          # trend align: long only > EMA(n), short only < (0 = off)
        time_windows: Optional[list[tuple[str, str]]] = None,  # ET "HH:MM" entry windows (None = off)
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
        self.adx_len = adx_len
        self.adx_min = adx_min
        self.ema_trend = ema_trend
        # Parse "HH:MM" windows into (start_minute, end_minute) of the ET day.
        self.time_windows = None
        if time_windows:
            self.time_windows = [
                (int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:]))
                for a, b in time_windows
            ]

    async def generate_signals(
        self,
        ticker: str,
        bars: list[Bar],
        option_chain: list[OptionContract],  # unused: signals are price-action only
        iv_analysis: IVAnalysis,             # unused
    ) -> list[Signal]:
        if len(bars) < self.min_bars:
            return []
        # Delegate to the vectorized series() so the live path, the backtester,
        # the charts endpoint and the sweep all share one implementation — incl.
        # the ADX regime gate. (series() is causal, so the last value here equals
        # what this method computed bar-by-bar before the refactor.)
        ser = self.series(bars)
        score = ser["score"][-1]
        votes = ser["votes"][-1]
        close = bars[-1].close
        if score >= self.min_score:
            return [self._signal(ticker, OptionRight.CALL, score, votes, close)]
        if -score >= self.min_score:
            return [self._signal(ticker, OptionRight.PUT, -score, votes, close)]
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

        # pandas_ta returns None when a column can't be computed (too few bars for
        # the lookback — e.g. a thin live window). Coerce those to an all-NA series
        # so charting degrades to "no line" instead of raising
        # 'NoneType' object is not iterable.
        na = pd.Series([pd.NA] * n, index=close.index)
        _ser = lambda x: na if x is None else x  # noqa: E731

        ef = _ser(ta.ema(close, length=self.ema_fast)) if self.use_ema else na
        es = _ser(ta.ema(close, length=self.ema_slow)) if self.use_ema else na
        # Session-anchored VWAP: reset the running sums each trading day, the way
        # intraday VWAP actually works (a window-wide cumulative VWAP is anchored
        # to the start of history and is meaningless intraday).
        tp = (f["high"] + f["low"] + f["close"]) / 3.0
        day = f.index.normalize()
        cv = f["volume"].groupby(day).cumsum()
        vw = (tp * f["volume"]).groupby(day).cumsum() / cv.replace(0, pd.NA)
        macd_df = ta.macd(close, fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal)
        hist = macd_df.iloc[:, 1] if macd_df is not None and not macd_df.empty else pd.Series([pd.NA] * n, index=close.index)
        r = _ser(ta.rsi(close, length=self.rsi_len))
        bb = ta.bbands(close, length=self.bb_len, std=self.bb_std)
        if bb is not None and not bb.empty:
            bb_lower, bb_upper = bb.iloc[:, 0], bb.iloc[:, 2]
        else:
            bb_lower = bb_upper = pd.Series([pd.NA] * n, index=close.index)

        # ---- optional success-rate gates (computed once, applied per bar) ----
        adx_l = None
        if self.adx_min > 0:
            adx_df = ta.adx(f["high"], f["low"], close, length=self.adx_len)
            adx_col = adx_df.iloc[:, 0] if adx_df is not None and not adx_df.empty else pd.Series([pd.NA] * n, index=close.index)
            adx_l = list(adx_col)
        emat_l = list(ta.ema(close, length=self.ema_trend)) if self.ema_trend > 0 else None
        times = [b.time for b in bars]

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
            score = 0 if i < self.min_bars - 1 else bull - bear
            if score != 0:
                if adx_l is not None and (pd.isna(adx_l[i]) or adx_l[i] < self.adx_min):
                    score = 0  # regime: not enough trend strength
                elif emat_l is not None and (
                    pd.isna(emat_l[i])
                    or (score > 0 and cl_l[i] <= emat_l[i])
                    or (score < 0 and cl_l[i] >= emat_l[i])
                ):
                    score = 0  # counter-trend vs the higher-timeframe EMA
                elif self.time_windows is not None:
                    mins = times[i].hour * 60 + times[i].minute
                    if not any(a <= mins <= b for a, b in self.time_windows):
                        score = 0  # outside the allowed entry windows
            scores.append(score)
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
