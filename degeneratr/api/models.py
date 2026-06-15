"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    ticker: str = "SPY"
    strategy: str = "iv_rank"
    days: int = Field(default=5, ge=1, le=30)
    period: str = Field(default="5m")
    warmup: int = Field(default=30, ge=5, le=200)
    source: str = Field(default="live")  # "live" (Tiger) or "store" (local archive)
    iv: Optional[float] = Field(default=None)  # annualized vol; None → estimate from bars

    # Risk overrides (defaults tuned for ~$5–10 SPY options on a $25k account).
    max_loss_per_trade: float = Field(default=1500.0, gt=0)
    per_trade_fraction: float = Field(default=0.10, gt=0, le=1)
    max_daily_loss: float = Field(default=5000.0, gt=0)

    # Exit + gating overrides (fractions of the underlying price move).
    # Defaults = sweep's robust 2:1 reward/risk (0.8% take-profit, 0.4% stop).
    take_profit_pct: float = Field(default=0.008, gt=0)
    stop_loss_pct: float = Field(default=0.004, gt=0)
    max_concurrent: int = Field(default=5, ge=1, le=50)
    cooldown_bars: int = Field(default=6, ge=0, le=200)


class TradeOut(BaseModel):
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    exit_reason: str
    entry_reason: str  # which signal(s) triggered the entry
    win: bool
    # Contract identity.
    strike: float
    expiry: str
    right: str  # CALL / PUT
    side: str   # BUY / SELL (entry side)


class BacktestResponse(BaseModel):
    ticker: str
    strategy: str
    days: int
    starting_cash: float
    ending_equity: float
    return_pct: float
    win_rate: float
    wins: int
    losses: int
    avg_win: float
    avg_loss: float
    profit_factor: Optional[float]
    expectancy: float
    max_drawdown: float
    signals_generated: int
    signals_rejected: int
    total_commission: float
    equity_curve: list[tuple[str, float]]
    price_series: list[tuple[str, float]]
    trades: list[TradeOut]


class ScanCandidateOut(BaseModel):
    symbol: str
    score: float
    iv_rank: Optional[float] = None
    iv_percentile: Optional[float] = None
    net_inflow: Optional[float] = None
    earnings_within_days: Optional[int] = None
    reasons: list[str] = []


class ScanResponse(BaseModel):
    count: int
    candidates: list[ScanCandidateOut]
    error: Optional[str] = None
