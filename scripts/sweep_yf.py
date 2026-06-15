"""Parameter sweep on yfinance 60-day data, for 5m and 15m timeframes.

Faithful to the real sweep (same PriceActionStrategy + UnderlyingBacktester),
but precomputes per-bar signal scores once per (ema, bb_mode) so min_score and
exit variants are cheap replays. Prints a ranked table per timeframe and writes
sweep_yf_results.json.

    PYTHONPATH=. python scripts/sweep_yf.py
"""
from __future__ import annotations

import asyncio
import itertools
import json
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "scripts")

from datetime import datetime, timedelta

from yf_timeframe import TICKERS, YFProvider

from degeneratr.backtester.underlying import UnderlyingBacktester
from degeneratr.config import Settings
from degeneratr.data.base import BarPeriod, IVAnalysis, OptionRight
from degeneratr.risk.manager import RiskManager
from degeneratr.strategies import PriceActionStrategy
from degeneratr.strategies.base import Signal, SignalAction, Strategy

GRID = {
    "min_score": [2, 3],
    "bb_mode": ["breakout", "reversion"],
    "ema": [(9, 21), (5, 13)],
    "exit": [(0.005, 0.003), (0.008, 0.004), (0.004, 0.004)],
}
PERIODS = [BarPeriod.FIVE_MINUTES, BarPeriod.FIFTEEN_MINUTES]


class ReplayStrategy(Strategy):
    """Returns precomputed signals by prefix length — no indicator recompute."""

    name = "degeneratr"

    def __init__(self, scores: dict[int, int], min_score: int) -> None:
        self._scores = scores
        self._min = min_score

    async def generate_signals(self, ticker, bars, option_chain, iv_analysis):
        s = self._scores.get(len(bars), 0)
        spot = bars[-1].close if bars else 0.0
        if s >= self._min:
            return [self._mk(ticker, OptionRight.CALL, s, spot)]
        if -s >= self._min:
            return [self._mk(ticker, OptionRight.PUT, -s, spot)]
        return []

    def _mk(self, ticker, right, score, spot) -> Signal:
        return Signal(
            ticker=ticker, action=SignalAction.BUY, right=right,
            confidence=min(0.95, 0.5 + 0.1 * score), contract=None, quantity=0,
            reason="replay", strategy=self.name, meta={"spot": spot, "score": score},
        )


async def precompute_scores(ticker, ema, bb_mode, bars) -> dict[int, int]:
    """Signed net score per prefix length, from the REAL strategy (min_score=1)."""
    strat = PriceActionStrategy(ema_fast=ema[0], ema_slow=ema[1], min_score=1, bb_mode=bb_mode)
    iv = IVAnalysis(symbol=ticker)
    scores: dict[int, int] = {}
    for i in range(len(bars)):
        sig = await strat.generate_signals(ticker, bars[: i + 1], [], iv)
        if sig:
            sg = sig[0]
            sc = sg.meta["score"]
            scores[i + 1] = sc if sg.right == OptionRight.CALL else -sc
    return scores


def config_key(c) -> str:
    ef, es = c["ema"]; tp, sl = c["exit"]
    return f"{c['min_score']}|{c['bb_mode']}|{ef}/{es}|{tp*100:g}/{sl*100:g}"


async def sweep_period(period, settings, provider) -> list[dict]:
    end = datetime.now(); begin = end - timedelta(days=70)
    rows: list[dict] = []
    ema_bb = list(itertools.product(GRID["ema"], GRID["bb_mode"]))
    for ti, ticker in enumerate(TICKERS):
        bars = await provider.get_bars(ticker, period, begin, end)
        for ema, bb_mode in ema_bb:
            scores = await precompute_scores(ticker, ema, bb_mode, bars)
            for min_score in GRID["min_score"]:
                for tp, sl in GRID["exit"]:
                    bt = UnderlyingBacktester(
                        strategy=ReplayStrategy(scores, min_score), provider=provider,
                        settings=settings, risk=RiskManager(settings),
                        take_profit_pct=tp, stop_loss_pct=sl,
                        max_concurrent_positions=5, cooldown_bars=6,
                    )
                    r = await bt.run(ticker, begin, end, period=period)
                    pf = r.profit_factor
                    rows.append({
                        "tf": period.value, "ticker": ticker,
                        "key": config_key({"min_score": min_score, "bb_mode": bb_mode, "ema": ema, "exit": (tp, sl)}),
                        "trades": r.num_trades, "win": round(r.win_rate * 100, 1),
                        "net": round(r.ending_equity - r.starting_cash, 2),
                        "pf": (None if pf == float("inf") else round(pf, 2)),
                        "maxdd": round(r.max_drawdown, 2),
                    })
        print(f"  [{period.value}] {ticker} done ({ti+1}/{len(TICKERS)})", flush=True)
    return rows


def rank_and_print(rows, tf):
    agg: dict[str, dict] = {}
    for r in rows:
        if r["tf"] != tf:
            continue
        a = agg.setdefault(r["key"], {"key": r["key"], "net": 0.0, "trades": 0, "wins": 0.0,
                                      "maxdd": 0.0, "pf_sum": 0.0, "n": 0})
        a["net"] += r["net"]; a["trades"] += r["trades"]
        a["wins"] += r["win"] * r["trades"] / 100.0
        a["maxdd"] = max(a["maxdd"], r["maxdd"])
        a["pf_sum"] += (r["pf"] or 0); a["n"] += 1
    ranked = sorted(agg.values(), key=lambda a: a["net"], reverse=True)
    print(f"\n==== RANKED CONFIGS @ {tf} (aggregate net across {len(TICKERS)} tickers, min 30 trades) ====")
    print(f"{'config (score|bb|ema|tp/sl)':<30} {'net':>10} {'trades':>7} {'win%':>6} {'avgPF':>6} {'maxDD':>8}")
    for a in ranked:
        if a["trades"] < 30:
            continue
        win = round(a["wins"] / a["trades"] * 100, 1) if a["trades"] else 0
        avgpf = round(a["pf_sum"] / a["n"], 2) if a["n"] else 0
        print(f"{a['key']:<30} {a['net']:>10.0f} {a['trades']:>7} {win:>6} {avgpf:>6} {a['maxdd']:>8.0f}")


async def main():
    settings = Settings(risk_max_loss_per_trade=1500, risk_per_trade_fraction=0.10,
                        risk_max_daily_loss=5000)
    provider = YFProvider()
    all_rows: list[dict] = []
    for period in PERIODS:
        print(f"sweeping {period.value} ...", flush=True)
        all_rows += await sweep_period(period, settings, provider)
    json.dump(all_rows, open("sweep_yf_results.json", "w"), indent=2, default=str)
    for period in PERIODS:
        rank_and_print(all_rows, period.value)


if __name__ == "__main__":
    asyncio.run(main())
