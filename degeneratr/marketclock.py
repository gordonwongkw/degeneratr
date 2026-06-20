"""US regular-session clock (Eastern Time), shared by the Telegram watcher and
the data pipeline so they agree on market hours / day rollover.

Bar timestamps elsewhere are naive ET-wall-clock; this module is about the
*current* wall-clock, so it uses a real tz (DST-correct) via zoneinfo.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN_MIN = 9 * 60 + 30   # 09:30 ET
CLOSE_MIN = 16 * 60      # 16:00 ET


def now_et() -> datetime:
    return datetime.now(ET)


def minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def is_weekday(now: datetime) -> bool:
    return now.weekday() < 5


def market_hours(now: datetime) -> bool:
    """True during the US regular session (Mon–Fri, 09:30–16:00 ET)."""
    return is_weekday(now) and OPEN_MIN <= minutes(now) < CLOSE_MIN


def after_close(now: datetime) -> bool:
    """True on a weekday at/after the 16:00 ET close."""
    return is_weekday(now) and minutes(now) >= CLOSE_MIN


def day_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")
