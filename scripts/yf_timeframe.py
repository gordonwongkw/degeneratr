"""Throwaway: compare timeframes for the price-action strategy using yfinance.

yfinance retains far more intraday history than Tiger (60d of 5m/15m, ~8d of 1m),
so we use it to ask: which day-trading timeframe (1m/5m/15m) does the strategy
perform best on? Same strategy config across timeframes; P&L from the underlying
move via UnderlyingBacktester.

    PYTHONPATH=. python scripts/yf_timeframe.py
"""
from __future__ import annotations

import asyncio
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

import yfinance as yf

from degeneratr.backtester.underlying import UnderlyingBacktester
from degeneratr.config import Settings
from degeneratr.data.base import Bar, BarPeriod
from degeneratr.risk.manager import RiskManager
from degeneratr.strategies import PriceActionStrategy

TICKERS = ["SPY", "QQQ", "AAPL", "AMD", "NVDA", "MU"]

# interval -> yfinance period string (max available for that interval)
TF = {
    BarPeriod.ONE_MINUTE: "8d",
    BarPeriod.FIVE_MINUTES: "60d",
    BarPeriod.FIFTEEN_MINUTES: "60d",
}


class YFProvider:
    """Minimal MarketDataProvider: only get_bars, backed by yfinance (cached)."""

    def __init__(self) -> None:
        self._cache: dict = {}

    async def get_bars(self, symbol, period: BarPeriod, begin, end):
        key = (symbol, period.value)
        if key not in self._cache:
            df = yf.Ticker(symbol).history(
                period=TF[period], interval=period.value, auto_adjust=True
            )
            bars = []
            for ts, row in df.iterrows():
                t = ts.tz_localize(None) if ts.tzinfo else ts
                bars.append(Bar(
                    symbol=symbol, time=t.to_pydatetime(),
                    open=float(row["Open"]), high=float(row["High"]),
                    low=float(row["Low"]), close=float(row["Close"]),
                    volume=int(row["Volume"]),
                ))
            self._cache[key] = bars
        return self._cache[key]

    def __getattr__(self, name):  # tolerate any other provider calls (unused)
        async def _noop(*a, **k):
            return []
        return _noop


async def run_tf(period: BarPeriod, settings, provider) -> dict:
    agg = {"net": 0.0, "trades": 0, "wins": 0.0, "maxdd": 0.0, "pf_sum": 0.0, "n": 0, "per": []}
    end = datetime.now()
    begin = end - timedelta(days=70)
    for tk in TICKERS:
        strat = PriceActionStrategy()  # defaults = robust config (ema 9/21, score 2, breakout)
        bt = UnderlyingBacktester(
            strategy=strat, provider=provider, settings=settings,
            risk=RiskManager(settings), take_profit_pct=0.008, stop_loss_pct=0.004,
            max_concurrent_positions=5, cooldown_bars=6,
        )
        r = await bt.run(tk, begin, end, period=period)
        pf = r.profit_factor
        pf = None if pf == float("inf") else pf
        agg["net"] += r.ending_equity - r.starting_cash
        agg["trades"] += r.num_trades
        agg["wins"] += r.win_rate * r.num_trades
        agg["maxdd"] = max(agg["maxdd"], r.max_drawdown)
        if pf is not None:
            agg["pf_sum"] += pf; agg["n"] += 1
        agg["per"].append((tk, r.num_trades, round(r.win_rate * 100, 1),
                           round(r.ending_equity - r.starting_cash, 0),
                           None if pf is None else round(pf, 2)))
    return agg


async def main():
    # Same amplified risk settings the sweep used, for comparability.
    settings = Settings(risk_max_loss_per_trade=1500, risk_per_trade_fraction=0.10,
                        risk_max_daily_loss=5000)
    provider = YFProvider()
    print(f"{'timeframe':<10} {'net':>10} {'trades':>7} {'win%':>6} {'avgPF':>6} {'maxDD':>8}  bars/ticker")
    for period in (BarPeriod.ONE_MINUTE, BarPeriod.FIVE_MINUTES, BarPeriod.FIFTEEN_MINUTES):
        agg = await run_tf(period, settings, provider)
        win = round(agg["wins"] / agg["trades"] * 100, 1) if agg["trades"] else 0
        avgpf = round(agg["pf_sum"] / agg["n"], 2) if agg["n"] else 0
        nbars = len(await provider.get_bars("SPY", period, None, None))
        print(f"{period.value:<10} {agg['net']:>10.0f} {agg['trades']:>7} {win:>6} "
              f"{avgpf:>6} {agg['maxdd']:>8.0f}  {nbars}")
        for tk, t, w, net, pf in agg["per"]:
            print(f"    {tk:<6} {t:>4}t  win {w:>5}%  net {net:>8}  pf {pf}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
