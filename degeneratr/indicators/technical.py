"""Technical indicators computed with pandas-ta over a list of :class:`Bar`.

Each function returns a small typed dict so callers don't have to know pandas.
``compute_all`` runs the full battery and is what strategies consume.
"""
from __future__ import annotations

from typing import Optional, TypedDict

import pandas as pd
import pandas_ta as ta

from ..data.base import Bar


class RSIResult(TypedDict):
    value: Optional[float]
    overbought: bool
    oversold: bool


class MACDResult(TypedDict):
    macd: Optional[float]
    signal: Optional[float]
    histogram: Optional[float]
    bullish_cross: bool
    bearish_cross: bool


class BollingerResult(TypedDict):
    upper: Optional[float]
    middle: Optional[float]
    lower: Optional[float]
    percent_b: Optional[float]


class IndicatorSnapshot(TypedDict):
    rsi: RSIResult
    macd: MACDResult
    vwap: Optional[float]
    ema_fast: Optional[float]
    ema_slow: Optional[float]
    atr: Optional[float]
    bollinger: BollingerResult
    last_close: Optional[float]


def bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
    """Convert bars into an OHLCV DataFrame indexed by time."""
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(
        {
            "time": [b.time for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    ).set_index("time")
    return frame


def _last(series: Optional[pd.Series]) -> Optional[float]:
    if series is None or series.empty:
        return None
    val = series.iloc[-1]
    return None if pd.isna(val) else float(val)


def rsi(frame: pd.DataFrame, length: int = 14) -> RSIResult:
    series = ta.rsi(frame["close"], length=length)
    value = _last(series)
    return RSIResult(
        value=value,
        overbought=value is not None and value >= 70,
        oversold=value is not None and value <= 30,
    )


def macd(frame: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> MACDResult:
    df = ta.macd(frame["close"], fast=fast, slow=slow, signal=signal)
    if df is None or df.empty:
        return MACDResult(
            macd=None, signal=None, histogram=None,
            bullish_cross=False, bearish_cross=False,
        )
    macd_col, hist_col, signal_col = df.columns[0], df.columns[1], df.columns[2]
    macd_val = _last(df[macd_col])
    signal_val = _last(df[signal_col])
    hist_val = _last(df[hist_col])
    prev_hist = float(df[hist_col].iloc[-2]) if len(df) >= 2 and not pd.isna(df[hist_col].iloc[-2]) else None

    bullish = bearish = False
    if hist_val is not None and prev_hist is not None:
        bullish = prev_hist <= 0 < hist_val
        bearish = prev_hist >= 0 > hist_val
    return MACDResult(
        macd=macd_val, signal=signal_val, histogram=hist_val,
        bullish_cross=bullish, bearish_cross=bearish,
    )


def vwap(frame: pd.DataFrame) -> Optional[float]:
    if frame.empty:
        return None
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    cum_vol = frame["volume"].cumsum()
    if cum_vol.iloc[-1] == 0:
        return None
    cum_tpv = (typical * frame["volume"]).cumsum()
    return float((cum_tpv / cum_vol).iloc[-1])


def ema(frame: pd.DataFrame, length: int) -> Optional[float]:
    return _last(ta.ema(frame["close"], length=length))


def atr(frame: pd.DataFrame, length: int = 14) -> Optional[float]:
    return _last(ta.atr(frame["high"], frame["low"], frame["close"], length=length))


def bollinger(frame: pd.DataFrame, length: int = 20, std: float = 2.0) -> BollingerResult:
    df = ta.bbands(frame["close"], length=length, std=std)
    if df is None or df.empty:
        return BollingerResult(upper=None, middle=None, lower=None, percent_b=None)
    cols = df.columns
    lower = _last(df[cols[0]])
    middle = _last(df[cols[1]])
    upper = _last(df[cols[2]])
    percent = _last(df[cols[4]]) if len(cols) >= 5 else None
    return BollingerResult(upper=upper, middle=middle, lower=lower, percent_b=percent)


def compute_all(
    bars: list[Bar],
    ema_fast: int = 9,
    ema_slow: int = 21,
) -> IndicatorSnapshot:
    """Run the full indicator battery and return a typed snapshot."""
    frame = bars_to_frame(bars)
    return IndicatorSnapshot(
        rsi=rsi(frame),
        macd=macd(frame),
        vwap=vwap(frame),
        ema_fast=ema(frame, ema_fast),
        ema_slow=ema(frame, ema_slow),
        atr=atr(frame),
        bollinger=bollinger(frame),
        last_close=_last(frame["close"]) if not frame.empty else None,
    )
