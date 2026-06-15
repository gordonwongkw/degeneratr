"""Configuration sweep for the underlying-only (price-action) model.

Runs the UnderlyingBacktester across a grid of indicator / exit configs on
several tickers, aggregates per-config across tickers, and prints a ranked
table. Underlying bars are cached per ticker, so this is fast and makes no
option-data calls. Writes full results to sweep_results.json.

    python scripts/sweep.py
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
from datetime import datetime, timedelta

from degeneratr.backtester.underlying import UnderlyingBacktester
from degeneratr.config import Settings, get_settings
from degeneratr.data.base import BarPeriod
from degeneratr.data.factory import get_provider
from degeneratr.risk.manager import RiskManager
from degeneratr.storage import BarStore, StoreProvider
from degeneratr.strategies import PriceActionStrategy

TICKERS = ["SPY", "QQQ", "AAPL", "AMD", "NVDA", "MU"]
DAYS = 5
PERIOD = BarPeriod.FIVE_MINUTES

GRID = {
    "min_score": [2, 3],
    "bb_mode": ["breakout", "reversion"],
    "ema": [(9, 21), (5, 13)],
    "exit": [(0.005, 0.003), (0.008, 0.004), (0.004, 0.004)],  # (take_profit, stop_loss) of underlying
}


class _CachedProvider:
    """Memoize get_bars so all configs of a ticker share one fetch."""

    def __init__(self, real):
        self._real = real
        self._cache: dict = {}

    async def get_bars(self, symbol, period, begin, end):
        key = (symbol, period.value)
        if key not in self._cache:
            self._cache[key] = await self._real.get_bars(symbol, period, begin, end)
        return self._cache[key]

    def __getattr__(self, name):
        return getattr(self._real, name)


async def run_one(ticker, cfg, settings, provider, begin, end) -> dict:
    ef, es = cfg["ema"]
    tp, sl = cfg["exit"]
    strat = PriceActionStrategy(
        ema_fast=ef, ema_slow=es, min_score=cfg["min_score"], bb_mode=cfg["bb_mode"]
    )
    bt = UnderlyingBacktester(
        strategy=strat, provider=provider, settings=settings, risk=RiskManager(settings),
        take_profit_pct=tp, stop_loss_pct=sl, max_concurrent_positions=5, cooldown_bars=6,
    )
    r = await bt.run(ticker, begin, end, period=PERIOD)
    pf = r.profit_factor
    return {
        "ticker": ticker, "min_score": cfg["min_score"], "bb_mode": cfg["bb_mode"],
        "ema": f"{ef}/{es}", "exit": f"{tp*100:g}/{sl*100:g}",
        "trades": r.num_trades, "win": round(r.win_rate * 100, 1),
        "net": round(r.ending_equity - r.starting_cash, 2),
        "pf": (None if pf == float("inf") else round(pf, 2)),
        "exp": round(r.expectancy, 2), "maxdd": round(r.max_drawdown, 2),
    }


def config_key(row: dict) -> str:
    return f"{row['min_score']}|{row['bb_mode']}|{row['ema']}|{row['exit']}"


def _make_provider(source: str):
    if source == "store":
        return StoreProvider(BarStore(get_settings().bar_store_path))
    return get_provider()


async def main(source: str = "store", days: int | None = None) -> None:
    settings = Settings(
        risk_max_loss_per_trade=1500, risk_per_trade_fraction=0.10, risk_max_daily_loss=5000
    )
    # The store accumulates history, so sweep a wide window offline; live is
    # capped at Tiger's ~3-day window anyway.
    days = days if days is not None else (120 if source == "store" else DAYS)
    keys = list(GRID)
    rows: list[dict] = []
    end = datetime.now()
    begin = end - timedelta(days=days)
    total = len(TICKERS) * len(list(itertools.product(*GRID.values())))
    done = 0
    print(f"sweep source={source} window={days}d tickers={TICKERS}")
    for ticker in TICKERS:
        provider = _CachedProvider(_make_provider(source))
        for combo in itertools.product(*[GRID[k] for k in keys]):
            cfg = dict(zip(keys, combo))
            done += 1
            try:
                row = await run_one(ticker, cfg, settings, provider, begin, end)
            except Exception as exc:  # noqa: BLE001
                row = {"ticker": ticker, "error": str(exc), **{k: str(cfg.get(k)) for k in keys}}
            rows.append(row)
            print(f"[{done}/{total}] {ticker} {cfg} -> {row.get('trades','?')}t "
                  f"{row.get('win','?')}% net {row.get('net','?')} pf {row.get('pf','?')}")

    json.dump(rows, open("sweep_results.json", "w"), indent=2, default=str)

    agg: dict[str, dict] = {}
    for r in rows:
        if "error" in r:
            continue
        a = agg.setdefault(config_key(r), {"key": config_key(r), "net": 0.0, "trades": 0,
                                           "wins": 0.0, "maxdd": 0.0, "pf_sum": 0.0, "n": 0})
        a["net"] += r["net"]; a["trades"] += r["trades"]
        a["wins"] += r["win"] * r["trades"] / 100.0
        a["maxdd"] = max(a["maxdd"], r["maxdd"])
        a["pf_sum"] += (r["pf"] or 0); a["n"] += 1

    ranked = sorted(agg.values(), key=lambda a: a["net"], reverse=True)
    print("\n==== RANKED CONFIGS (aggregate net P&L across tickers, min 15 trades) ====")
    print(f"{'config (score|bb|ema|tp/sl)':<32} {'net':>9} {'trades':>7} {'win%':>6} {'avgPF':>6} {'maxDD':>8}")
    for a in ranked:
        if a["trades"] < 15:
            continue
        win = round(a["wins"] / a["trades"] * 100, 1) if a["trades"] else 0
        avgpf = round(a["pf_sum"] / a["n"], 2) if a["n"] else 0
        print(f"{a['key']:<32} {a['net']:>9.0f} {a['trades']:>7} {win:>6} {avgpf:>6} {a['maxdd']:>8.0f}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser()
    _p.add_argument("--source", choices=["live", "store"], default="store")
    _p.add_argument("--days", type=int, default=None)
    _a = _p.parse_args()
    asyncio.run(main(_a.source, _a.days))
