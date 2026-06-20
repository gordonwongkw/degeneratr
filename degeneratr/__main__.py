"""Command-line entrypoint for degeneratr.

Examples
--------
    python -m degeneratr paper --ticks 1 --dry-run
    python -m degeneratr paper --strategies momentum_breakout iv_rank
    python -m degeneratr scan --limit 15
    python -m degeneratr backtest --ticker SPY --strategy zero_dte --days 5
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from .config import get_settings
from .data.base import BarPeriod
from .engine import TickReport, TradingEngine
from .strategies import STRATEGY_REGISTRY


def _configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )


def _build_strategies(names: list[str]):
    selected = names or [next(iter(STRATEGY_REGISTRY))]
    strategies = []
    for name in selected:
        cls = STRATEGY_REGISTRY.get(name)
        if cls is None:
            raise SystemExit(
                f"Unknown strategy {name!r}. Available: {', '.join(STRATEGY_REGISTRY)}"
            )
        strategies.append(cls())
    return strategies


def _print_report(report: TickReport) -> None:
    print(f"\n=== tick @ {report.timestamp:%Y-%m-%d %H:%M:%S} ===")
    print(f"  candidates: {len(report.candidates)}")
    for c in report.candidates[:10]:
        print(f"    {c.symbol:<8} score={c.score:6.1f} {' '.join(c.reasons)}")
    print(f"  signals: {len(report.signals)}  orders: {len(report.orders)}")
    for o in report.orders:
        print(f"    order {o.order_id} {o.side.value} {o.symbol} x{o.quantity} @ {o.avg_fill_price}")
    if report.rejections:
        print(f"  rejections: {len(report.rejections)}")
        for r in report.rejections[:10]:
            print(f"    - {r}")


async def _run_paper(args: argparse.Namespace) -> None:
    engine = TradingEngine(strategies=_build_strategies(args.strategies))
    try:
        reports = await engine.run_paper(ticks=args.ticks, interval_seconds=args.interval)
        for rep in reports:
            _print_report(rep)
    finally:
        await engine.close()


async def _run_scan(args: argparse.Namespace) -> None:
    from .scanner.universe import TickerScanner

    scanner = TickerScanner()
    candidates = await scanner.scan(limit=args.limit)
    print(f"\nTop {len(candidates)} candidates:")
    for c in candidates:
        print(
            f"  {c.symbol:<8} score={c.score:6.1f} "
            f"iv_rank={c.iv_rank} flow={c.net_inflow} earn={c.earnings_within_days}"
        )


async def _run_backtest(args: argparse.Namespace) -> None:
    # Price-action strategy on the underlying; P&L is derived purely from the
    # stock's move (no options data). Same engine the dashboard / API use, so the
    # CLI and web results agree. Live execution still buys the closest-OTM 0DTE
    # CALL (bull) / PUT (bear) — that's the broker layer, not the backtest P&L.
    from .backtester.underlying import UnderlyingBacktester

    cls = STRATEGY_REGISTRY.get(args.strategy)
    if cls is None:
        raise SystemExit(f"Unknown strategy {args.strategy!r}")

    provider = None
    if args.source == "store":
        from .config import get_settings
        from .storage import BarStore, StoreProvider

        provider = StoreProvider(BarStore(get_settings().bar_store_path))
    bt = UnderlyingBacktester(strategy=cls(), provider=provider)
    period = next((p for p in BarPeriod if p.value == args.period), BarPeriod.FIFTEEN_MINUTES)
    end = datetime.now()
    begin = end - timedelta(days=args.days)
    result = await bt.run(args.ticker, begin, end, period=period)
    pf = result.profit_factor
    pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
    print(f"\n=== backtest {args.ticker} / {args.strategy} ({args.days}d) ===")
    print(f"  starting cash : {result.starting_cash:,.2f}")
    print(f"  ending equity : {result.ending_equity:,.2f}")
    print(f"  return        : {result.return_pct:+.2f}%")
    print(f"  realized P&L  : {result.realized_pnl:,.2f}")
    print(f"  commission    : {result.total_commission:,.2f}")
    print(f"  signals       : {result.signals_generated} generated / {result.signals_rejected} gated")
    print(f"  --- success rate ---")
    print(f"  round-trips   : {result.num_trades}  ({len(result.wins)}W / {len(result.losses)}L)")
    print(f"  win rate      : {result.win_rate * 100:.1f}%")
    print(f"  avg win       : {result.avg_win:,.2f}")
    print(f"  avg loss      : {result.avg_loss:,.2f}")
    print(f"  profit factor : {pf_str}")
    print(f"  expectancy    : {result.expectancy:,.2f} per trade")
    print(f"  max drawdown  : {result.max_drawdown:,.2f}")


async def _run_backfill(args: argparse.Namespace) -> None:
    from .config import get_settings
    from .storage import backfill

    symbols = args.symbols or get_settings().watchlist_symbols
    print(f"backfilling {symbols} ({args.days}d, ±{args.band} strikes)…")
    result = await backfill(
        symbols, days=args.days, strike_band=args.band, expiries=args.expiries
    )
    s = result["saved"]
    print(f"saved: {s['underlying']} underlying bars, {s['option_bars']} option bars "
          f"across {s['contracts']} contracts")
    _print_coverage(result["coverage"])


async def _run_ingest(args: argparse.Namespace) -> None:
    from .config import get_settings
    from .storage import ingest_yfinance

    symbols = args.symbols or get_settings().watchlist_symbols
    periods = [next(p for p in BarPeriod if p.value == v) for v in args.periods]
    print(f"ingesting {symbols} @ {args.periods} from yfinance…")
    result = await ingest_yfinance(symbols, periods)
    for per, n in result["saved"].items():
        print(f"  {per}: {n} bars saved")
    _print_coverage(result["coverage"])


async def _run_coverage(args: argparse.Namespace) -> None:
    from .config import get_settings
    from .storage import BarStore

    _print_coverage(BarStore(get_settings().bar_store_path).coverage())


def _print_coverage(cov: dict) -> None:
    print("\n=== store coverage ===")
    print("  underlying:")
    for u in cov.get("underlying", []):
        print(f"    {u['symbol']:<6} {u['period']:<4} {u['bars']:>6} bars  "
              f"{(u['from'] or '?')[:16]} → {(u['to'] or '?')[:16]}")
    o = cov.get("options", {})
    print(f"  options: {o.get('contracts', 0)} contracts, {o.get('bars', 0)} bars  "
          f"{(o.get('from') or '?')[:16]} → {(o.get('to') or '?')[:16]}")


async def _run_performance(args: argparse.Namespace) -> None:
    from .config import get_settings
    from .storage import BarStore, persist_trade_log

    settings = get_settings()
    store = BarStore(settings.bar_store_path)
    if args.rebuild:
        n = await persist_trade_log(settings, store=store)
        print(f"persisted {n} round-trips to trade_log")
    perf = store.performance_summary()
    o = perf["overall"]
    _pf = lambda v: "inf" if v is None else f"{v}"  # noqa: E731
    print("\n=== algorithm performance (persisted trade log) ===")
    print(f"  trades        : {o['trades']}  ({o['wins']}W / {o['losses']}L)")
    print(f"  win rate      : {o['win_rate'] * 100:.1f}%")
    print(f"  net P&L       : {o['net_pnl']:,.2f}")
    print(f"  profit factor : {_pf(o['profit_factor'])}")
    print(f"  expectancy    : {o['expectancy']:,.2f} per trade")
    if perf["per_symbol"]:
        print("  --- per symbol ---")
        for sym, s in perf["per_symbol"].items():
            print(f"    {sym:<6} {s['trades']:>4}t  {s['wins']}W/{s['losses']}L  "
                  f"win {s['win_rate'] * 100:5.1f}%  net {s['net_pnl']:>11,.2f}  pf {_pf(s['profit_factor'])}")


async def _run_watch(args: argparse.Namespace) -> None:
    from .notify import run_watch_loop
    from .notify.telegram import TelegramNotifier

    if args.test:
        ok = TelegramNotifier().send(
            "✅ degeneratr watcher online — Telegram is configured."
        )
        print(
            "test ping sent."
            if ok
            else "test ping NOT sent — check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env."
        )
        return
    await run_watch_loop()


def _run_serve(args: argparse.Namespace) -> None:
    import uvicorn

    print(f"degeneratr dashboard -> http://{args.host}:{args.port}")
    uvicorn.run(
        "degeneratr.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="degeneratr", description="Options day-trading tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_paper = sub.add_parser("paper", help="run the engine in paper mode")
    p_paper.add_argument("--ticks", type=int, default=1)
    p_paper.add_argument("--interval", type=float, default=0.0, help="seconds between ticks")
    p_paper.add_argument("--strategies", nargs="*", default=[], help="strategy names")
    p_paper.add_argument("--dry-run", action="store_true")
    p_paper.set_defaults(func=_run_paper)

    p_scan = sub.add_parser("scan", help="scan the universe for candidates")
    p_scan.add_argument("--limit", type=int, default=20)
    p_scan.set_defaults(func=_run_scan)

    p_bt = sub.add_parser("backtest", help="backtest a strategy on a ticker")
    p_bt.add_argument("--ticker", required=True)
    p_bt.add_argument("--strategy", default=next(iter(STRATEGY_REGISTRY)))
    p_bt.add_argument("--days", type=int, default=60)
    p_bt.add_argument("--period", default="15m",
                      choices=[p.value for p in BarPeriod],
                      help="bar period (default 15m — best for this strategy)")
    p_bt.add_argument("--source", choices=["live", "store"], default="store",
                      help="local accumulated store (default) or live Tiger data")
    p_bt.set_defaults(func=_run_backtest)

    p_bf = sub.add_parser("backfill", help="capture current Tiger data into the local store")
    p_bf.add_argument("--symbols", nargs="+", default=None, help="default: the configured watchlist")
    p_bf.add_argument("--days", type=int, default=5)
    p_bf.add_argument("--band", type=int, default=12, help="strikes per side near spot")
    p_bf.add_argument("--expiries", type=int, default=1, help="front expiries to capture")
    p_bf.set_defaults(func=_run_backfill)

    p_ing = sub.add_parser("ingest", help="seed the store with yfinance underlying bars")
    p_ing.add_argument("--symbols", nargs="+", default=None, help="default: the configured watchlist")
    p_ing.add_argument("--periods", nargs="+", default=["5m", "15m"],
                       choices=[p.value for p in BarPeriod], help="bar periods to ingest")
    p_ing.set_defaults(func=_run_ingest)

    p_cov = sub.add_parser("coverage", help="show what's in the local data store")
    p_cov.set_defaults(func=_run_coverage)

    p_serve = sub.add_parser("serve", help="launch the web dashboard + API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=_run_serve)

    p_watch = sub.add_parser("watch", help="poll live data and push Telegram alerts")
    p_watch.add_argument("--test", action="store_true",
                         help="send a single Telegram test ping and exit")
    p_watch.set_defaults(func=_run_watch)

    p_perf = sub.add_parser("performance", help="show persisted trade-log performance")
    p_perf.add_argument("--rebuild", action="store_true",
                        help="replay the algorithm over the store and persist the trade log first")
    p_perf.set_defaults(func=_run_performance)

    return parser


def main() -> None:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args()
    # `serve` runs uvicorn (its own event loop); the rest are coroutines.
    if asyncio.iscoroutinefunction(args.func):
        asyncio.run(args.func(args))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
