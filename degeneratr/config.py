"""Application configuration loaded from environment / `.env` via pydantic-settings.

All credentials and tunable limits live here. Nothing should be hardcoded
elsewhere in the codebase — import `get_settings()` and read from it.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env against the project root (parent of this package) so the app
# loads credentials regardless of the current working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class TigerTradeEnv(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class MooMooTradeEnv(str, Enum):
    REAL = "REAL"
    SIMULATE = "SIMULATE"


class Settings(BaseSettings):
    """Strongly-typed view over the process environment."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Provider selection ----
    market_data_provider: str = Field(default="tiger")
    broker_provider: str = Field(default="paper")

    # ---- Tiger ----
    tiger_id: str = Field(default="")
    # Generic fallback account (used if the env-specific one below is unset).
    tiger_account: str = Field(default="")
    # Env-specific accounts; the active one is chosen by ``tiger_trade_env``.
    tiger_paper_account: str = Field(default="")
    tiger_live_account: str = Field(default="")
    tiger_private_key_path: str = Field(default="")
    tiger_trade_env: TigerTradeEnv = Field(default=TigerTradeEnv.PAPER)

    @property
    def active_tiger_account(self) -> str:
        """Resolve the Tiger account for the current trade env (PAPER/LIVE)."""
        if self.tiger_trade_env == TigerTradeEnv.LIVE:
            return self.tiger_live_account or self.tiger_account
        return self.tiger_paper_account or self.tiger_account

    # ---- MooMoo ----
    moomoo_host: str = Field(default="127.0.0.1")
    moomoo_port: int = Field(default=11111)
    moomoo_security_firm: str = Field(default="FUTUINC")
    moomoo_unlock_trade: str = Field(default="")
    moomoo_trade_env: MooMooTradeEnv = Field(default=MooMooTradeEnv.SIMULATE)

    # ---- Risk ----
    risk_max_loss_per_trade: float = Field(default=250.0)
    risk_max_daily_loss: float = Field(default=1000.0)
    # Net delta cap (delta × contracts × 100). 0DTE scalping runs high intraday
    # delta, so this is permissive by default; tighten it to curb leverage.
    risk_max_delta_exposure: float = Field(default=2500.0)
    risk_per_trade_fraction: float = Field(default=0.02)

    # ---- Backtester ----
    commission_per_contract: float = Field(default=0.65)
    backtest_starting_cash: float = Field(default=25000.0)

    # ---- Local data store (accumulates bars across runs / days) ----
    bar_store_path: str = Field(default=str(_PROJECT_ROOT / "data" / "bars.db"))
    # Tickers captured by backfill / swept by default.
    watchlist: str = Field(default="SPY,QQQ,AAPL,AMD,NVDA,MU")

    @property
    def watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    # ---- Telegram notifications ----
    # Bot token from @BotFather and the chat id to deliver alerts to. The notifier
    # is inert (logs one warning) until both are set, so nothing breaks meanwhile.
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")
    # Master switch: when true, `serve` auto-starts the watcher as a background task.
    telegram_enabled: bool = Field(default=False)
    # Seconds between watcher polls during market hours.
    watch_interval_seconds: float = Field(default=20.0)

    # ---- Misc ----
    log_level: str = Field(default="INFO")
    # In-memory cache TTL (seconds) for live bar/chain fetches. Historical bars
    # don't change, so repeated backtests reuse them. 0 disables caching.
    bar_cache_ttl: float = Field(default=300.0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached, process-wide settings instance."""
    return Settings()
