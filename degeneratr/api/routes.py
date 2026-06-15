"""API route handlers — thin async wrappers over the engine/backtester/scanner."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from ..backtester.engine import BacktestResult
from ..backtester.underlying import UnderlyingBacktester
from ..config import Settings, get_settings
from ..data.base import BarPeriod
from ..risk.manager import RiskManager
from ..scanner.universe import TickerScanner
from ..strategies import ALGORITHM_NAME, COMPONENT_STRATEGIES, STRATEGY_REGISTRY
from .models import (
    BacktestRequest,
    BacktestResponse,
    ScanCandidateOut,
    ScanResponse,
    TradeOut,
)

logger = logging.getLogger("degeneratr.api")
router = APIRouter(prefix="/api")

_PERIODS = {p.value: p for p in BarPeriod}


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "degeneratr"}


@router.get("/coverage")
async def coverage() -> dict:
    from ..storage import BarStore

    return BarStore(get_settings().bar_store_path).coverage()


@router.get("/strategies")
async def strategies() -> dict:
    return {
        "algorithm": ALGORITHM_NAME,
        "components": COMPONENT_STRATEGIES,
        "strategies": list(STRATEGY_REGISTRY.keys()),
    }


def _settings_with_overrides(req: BacktestRequest) -> Settings:
    base = get_settings()
    # Clone base settings (keeps creds/provider) and apply per-request risk knobs.
    return base.model_copy(
        update={
            "risk_max_loss_per_trade": req.max_loss_per_trade,
            "risk_per_trade_fraction": req.per_trade_fraction,
            "risk_max_daily_loss": req.max_daily_loss,
        }
    )


def _serialize(result: BacktestResult, req: BacktestRequest) -> BacktestResponse:
    pf = result.profit_factor
    return BacktestResponse(
        ticker=req.ticker,
        strategy=req.strategy,
        days=req.days,
        starting_cash=round(result.starting_cash, 2),
        ending_equity=round(result.ending_equity, 2),
        return_pct=round(result.return_pct, 2),
        win_rate=round(result.win_rate, 4),
        wins=len(result.wins),
        losses=len(result.losses),
        avg_win=round(result.avg_win, 2),
        avg_loss=round(result.avg_loss, 2),
        profit_factor=None if pf == float("inf") else round(pf, 2),
        expectancy=round(result.expectancy, 2),
        max_drawdown=round(result.max_drawdown, 2),
        signals_generated=result.signals_generated,
        signals_rejected=result.signals_rejected,
        total_commission=round(result.total_commission, 2),
        equity_curve=[(t.isoformat(), round(v, 2)) for t, v in result.equity_curve],
        price_series=[(t.isoformat(), round(v, 2)) for t, v in result.price_series],
        trades=[
            TradeOut(
                entry_time=rt.entry_time.isoformat(),
                exit_time=rt.exit_time.isoformat(),
                entry_price=rt.entry_price,
                exit_price=rt.exit_price,
                qty=rt.quantity,
                pnl=round(rt.pnl, 2),
                pnl_pct=round(rt.pnl_pct, 3),
                exit_reason=rt.exit_reason,
                entry_reason=rt.entry_reason,
                win=rt.win,
                strike=rt.strike,
                expiry=rt.expiry,
                right=rt.right,
                side=rt.side.value,
            )
            for rt in result.round_trips
        ],
    )


@router.post("/backtest", response_model=BacktestResponse)
async def backtest(req: BacktestRequest) -> BacktestResponse:
    strat_cls = STRATEGY_REGISTRY.get(req.strategy)
    if strat_cls is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown strategy {req.strategy!r}. Available: {sorted(STRATEGY_REGISTRY)}",
        )
    period = _PERIODS.get(req.period)
    if period is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown period {req.period!r}. Available: {sorted(_PERIODS)}",
        )

    settings = _settings_with_overrides(req)
    provider = None
    if req.source == "store":
        from ..storage import BarStore, StoreProvider

        provider = StoreProvider(BarStore(settings.bar_store_path))
    # Signals come from the underlying's price action; P&L is derived purely
    # from the stock's move (no options, no model).
    bt = UnderlyingBacktester(
        strategy=strat_cls(),
        provider=provider,
        settings=settings,
        risk=RiskManager(settings),
        take_profit_pct=req.take_profit_pct,
        stop_loss_pct=req.stop_loss_pct,
        max_concurrent_positions=req.max_concurrent,
        cooldown_bars=req.cooldown_bars,
    )
    end = datetime.now()
    begin = end - timedelta(days=req.days)
    try:
        result = await bt.run(req.ticker, begin, end, period=period, warmup=req.warmup)
    except Exception as exc:  # noqa: BLE001 - surface provider/data errors to the client
        logger.exception("backtest failed")
        raise HTTPException(status_code=502, detail=f"backtest failed: {exc}") from exc
    return _serialize(result, req)


@router.get("/scan", response_model=ScanResponse)
async def scan(limit: int = 20) -> ScanResponse:
    scanner = TickerScanner()
    try:
        candidates = await scanner.scan(limit=limit)
    except Exception as exc:  # noqa: BLE001 - scanner depends on Tiger scanner perms
        logger.exception("scan failed")
        return ScanResponse(count=0, candidates=[], error=str(exc))
    return ScanResponse(
        count=len(candidates),
        candidates=[
            ScanCandidateOut(
                symbol=c.symbol,
                score=round(c.score, 2),
                iv_rank=c.iv_rank,
                iv_percentile=c.iv_percentile,
                net_inflow=c.net_inflow,
                earnings_within_days=c.earnings_within_days,
                reasons=list(c.reasons),
            )
            for c in candidates
        ],
    )
