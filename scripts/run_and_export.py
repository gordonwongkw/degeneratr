"""Dev helper: run a backtest and export the result to JSON for visualization.

Usage:
    python scripts/run_and_export.py SPY iv_rank 5 out.json
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta

from degeneratr.backtester.engine import Backtester
from degeneratr.data.base import BarPeriod
from degeneratr.strategies import STRATEGY_REGISTRY


async def main() -> None:
    ticker = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    strat_name = sys.argv[2] if len(sys.argv) > 2 else "iv_rank"
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    out = sys.argv[4] if len(sys.argv) > 4 else "backtest_out.json"

    strat = STRATEGY_REGISTRY[strat_name]()
    bt = Backtester(strategy=strat)
    end = datetime.now()
    begin = end - timedelta(days=days)
    r = await bt.run(ticker, begin, end, period=BarPeriod.FIVE_MINUTES)

    payload = {
        "ticker": ticker,
        "strategy": strat_name,
        "days": days,
        "starting_cash": r.starting_cash,
        "ending_equity": r.ending_equity,
        "return_pct": r.return_pct,
        "win_rate": r.win_rate,
        "wins": len(r.wins),
        "losses": len(r.losses),
        "avg_win": r.avg_win,
        "avg_loss": r.avg_loss,
        "profit_factor": (None if r.profit_factor == float("inf") else r.profit_factor),
        "expectancy": r.expectancy,
        "max_drawdown": r.max_drawdown,
        "signals_generated": r.signals_generated,
        "signals_rejected": r.signals_rejected,
        "total_commission": r.total_commission,
        "equity_curve": [[t.isoformat(), round(v, 2)] for t, v in r.equity_curve],
        "round_trips": [
            {
                "entry_time": rt.entry_time.isoformat(),
                "exit_time": rt.exit_time.isoformat(),
                "entry_price": rt.entry_price,
                "exit_price": rt.exit_price,
                "qty": rt.quantity,
                "pnl": round(rt.pnl, 2),
                "exit_reason": rt.exit_reason,
                "win": rt.win,
            }
            for rt in r.round_trips
        ],
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"wrote {out}: {r.num_trades} trades, win_rate={r.win_rate*100:.1f}%, "
          f"return={r.return_pct:+.2f}%, maxDD={r.max_drawdown:,.0f}")


if __name__ == "__main__":
    asyncio.run(main())
