"""Out-of-sample ablation: do the success-rate filters actually help?

For each config we run the full 60-day 15m backtest per ticker, then split the
resulting trades by entry time into TRAIN (first 60%) and TEST (last 40%). A
filter is only worth keeping if it improves the TEST (out-of-sample) numbers —
in-sample gains are how you fool yourself. Metrics are pooled across tickers.

    PYTHONPATH=. python scripts/sweep_improve.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "scripts")
from datetime import datetime

from degeneratr.backtester.underlying import UnderlyingBacktester
from degeneratr.config import Settings
from degeneratr.data.base import BarPeriod
from degeneratr.risk.manager import RiskManager
from degeneratr.storage import BarStore
from degeneratr.strategies import PriceActionStrategy
from degeneratr.strategies.replay import ReplaySignalStrategy

TICKERS = ["SPY", "QQQ", "AAPL", "AMD", "NVDA", "MU"]
PERIOD = BarPeriod.FIFTEEN_MINUTES
WINDOWS = [("09:45", "11:30"), ("14:00", "15:45")]

# Each config: strategy filters + exit tweaks (anything unset = current default).
CONFIGS = [
    {"name": "baseline"},
    {"name": "adx20", "adx_min": 20},
    {"name": "adx25", "adx_min": 25},
    {"name": "trend50", "ema_trend": 50},
    {"name": "time-of-day", "time_windows": WINDOWS},
    {"name": "score3", "min_score": 3},
    {"name": "breakeven .2%", "breakeven_after": 0.002},
    {"name": "trail .3%", "trail_pct": 0.003},
    {"name": "adx25+trend50", "adx_min": 25, "ema_trend": 50},
    {"name": "adx25+trend50+time", "adx_min": 25, "ema_trend": 50, "time_windows": WINDOWS},
    {"name": "adx25+trend50+score3", "adx_min": 25, "ema_trend": 50, "min_score": 3},
    {"name": "ALL filters", "adx_min": 25, "ema_trend": 50, "time_windows": WINDOWS, "min_score": 3},
    {"name": "ALL+breakeven", "adx_min": 25, "ema_trend": 50, "time_windows": WINDOWS, "min_score": 3, "breakeven_after": 0.002},
    {"name": "ALL+trail", "adx_min": 25, "ema_trend": 50, "time_windows": WINDOWS, "min_score": 3, "trail_pct": 0.003},
]

_STRAT_KEYS = {"adx_min", "ema_trend", "time_windows", "min_score"}
_EXIT_KEYS = {"breakeven_after", "trail_pct"}


def metrics(rts):
    n = len(rts)
    if n == 0:
        return (0, 0.0, 0.0, 0.0, 0.0)
    wins = [r for r in rts if r.pnl > 0]
    gw = sum(r.pnl for r in wins)
    gl = -sum(r.pnl for r in rts if r.pnl <= 0)
    pf = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)
    return (n, len(wins) / n * 100, sum(r.pnl for r in rts) / n, pf, sum(r.pnl for r in rts))


async def run_config(cfg, settings, bars_by_ticker, split_time):
    train, test = [], []
    for tk, bars in bars_by_ticker.items():
        strat_kw = {k: cfg[k] for k in _STRAT_KEYS if k in cfg}
        exit_kw = {k: cfg[k] for k in _EXIT_KEYS if k in cfg}
        min_score = cfg.get("min_score", 2)
        scores = PriceActionStrategy(
            adx_min=cfg.get("adx_min", 0.0), ema_trend=cfg.get("ema_trend", 0),
            time_windows=cfg.get("time_windows"), min_score=min_score,
        ).series(bars)["score"]
        bt = UnderlyingBacktester(
            strategy=ReplaySignalStrategy(scores, min_score), settings=settings,
            risk=RiskManager(settings), provider=_Pre(tk, bars),
            take_profit_pct=0.004, stop_loss_pct=0.004, **exit_kw,
        )
        r = await bt.run(tk, bars[0].time, bars[-1].time, period=PERIOD)
        for rt in r.round_trips:
            (train if rt.entry_time < split_time[tk] else test).append(rt)
    return metrics(train), metrics(test)


class _Pre:
    def __init__(self, sym, bars):
        self._sym, self._bars = sym, bars

    async def get_bars(self, symbol, period, b, e):
        return self._bars if symbol == self._sym else []

    def __getattr__(self, name):
        async def _n(*a, **k):
            return [] if "bars" not in name else {}
        return _n


async def main():
    settings = Settings(risk_max_loss_per_trade=1500, risk_per_trade_fraction=0.10, risk_max_daily_loss=5000)
    store = BarStore(settings.bar_store_path)
    bars_by_ticker, split_time = {}, {}
    for tk in TICKERS:
        bars = store.load_underlying(tk, PERIOD.value, datetime(2026, 1, 1), datetime(2026, 12, 31))
        bars_by_ticker[tk] = bars
        split_time[tk] = bars[int(len(bars) * 0.6)].time  # first 60% = train

    print(f"{'config':<22} | {'TRAIN: n  win%   exp    pf':<30} | {'TEST(OOS): n  win%   exp    pf':<32}")
    print("-" * 92)
    for cfg in CONFIGS:
        (tn, tw, te, tpf, _), (sn, sw, se, spf, snet) = await run_config(cfg, settings, bars_by_ticker, split_time)
        print(f"{cfg['name']:<22} | {tn:>5} {tw:>5.1f} {te:>7.1f} {tpf:>5.2f}      "
              f"| {sn:>5} {sw:>5.1f} {se:>7.1f} {spf:>5.2f}   net {snet:>9.0f}")


if __name__ == "__main__":
    asyncio.run(main())
