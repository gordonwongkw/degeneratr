"""Market-hours watcher: polls live chart data and pushes Telegram alerts.

Runs the exact same signal/trade computation the dashboard uses
(:func:`degeneratr.api.routes.compute_charts`) and diffs each poll against the
last-seen state per symbol to emit:

* a **SIGNAL** alert on every new bull/bear onset (real-time),
* **ENTRY**/**EXIT** alerts when a completed round-trip first appears, and
* a single **end-of-day summary** shortly after the 16:00 ET close.

State is in-memory and resets at each new trading day. The first poll of a run
(or session) is a silent baseline so historical signals aren't replayed as spam.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from .telegram import (
    TelegramNotifier,
    format_entry,
    format_eod_summary,
    format_exit,
    format_signal,
)

logger = logging.getLogger("degeneratr.notify")

ET = ZoneInfo("America/New_York")
_OPEN_MIN = 9 * 60 + 30   # 09:30 ET
_CLOSE_MIN = 16 * 60      # 16:00 ET


def _now_et() -> datetime:
    return datetime.now(ET)


def _minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _is_weekday(now: datetime) -> bool:
    return now.weekday() < 5


def _market_hours(now: datetime) -> bool:
    return _is_weekday(now) and _OPEN_MIN <= _minutes(now) < _CLOSE_MIN


class _State:
    """Per-run alert bookkeeping, reset when the trading day rolls over."""

    def __init__(self) -> None:
        self.day: str | None = None
        self.baselined = False
        self.eod_sent = False
        self.last_signal: dict[str, int] = {}      # symbol -> last signal epoch
        self.seen_trades: dict[str, set] = {}       # symbol -> {(n, entry, exit)}

    def roll(self, day: str) -> None:
        if day != self.day:
            self.day = day
            self.baselined = False
            self.eod_sent = False
            self.last_signal.clear()
            self.seen_trades.clear()


def _diff_and_alert(charts: list[dict], st: _State, notifier: TelegramNotifier) -> int:
    """Emit alerts for new signals/trades. On the baseline poll, only record state
    (no sends). Returns the number of messages sent."""
    sent = 0
    for c in charts:
        sym = c["symbol"]
        sigs = c.get("signals", [])
        trades = c.get("trades", [])
        last_sig = st.last_signal.get(sym, 0)
        seen = st.seen_trades.setdefault(sym, set())
        if st.baselined:
            for s in sigs:
                if s["time"] > last_sig:
                    if notifier.send(format_signal(sym, s)):
                        sent += 1
            for t in trades:
                key = (t["n"], t["entry_time"], t["exit_time"])
                if key not in seen:
                    if notifier.send(format_entry(sym, t)):
                        sent += 1
                    if notifier.send(format_exit(sym, t)):
                        sent += 1
        # update state regardless (baseline records the current world silently)
        if sigs:
            st.last_signal[sym] = max(last_sig, max(s["time"] for s in sigs))
        for t in trades:
            seen.add((t["n"], t["entry_time"], t["exit_time"]))
    return sent


async def run_watch_loop(settings: Settings | None = None, interval: float | None = None) -> None:
    """Poll live data and push Telegram alerts until cancelled."""
    from ..api.routes import compute_charts  # local import avoids a circular import

    settings = settings or get_settings()
    interval = interval or settings.watch_interval_seconds
    notifier = TelegramNotifier(settings)
    if not notifier.configured:
        logger.warning("Watcher started but Telegram is not configured — alerts will be silent.")
    st = _State()
    logger.info("degeneratr watcher online (interval %.0fs)", interval)

    while True:
        now = _now_et()
        st.roll(now.strftime("%Y-%m-%d"))
        try:
            if _market_hours(now):
                data = await compute_charts(settings, period="5m", source="live", days=3)
                if not st.baselined:
                    _diff_and_alert(data["charts"], st, notifier)  # silent baseline
                    st.baselined = True
                    logger.info("watcher baselined %d symbols", len(data["charts"]))
                else:
                    n = _diff_and_alert(data["charts"], st, notifier)
                    if n:
                        logger.info("watcher sent %d alert(s)", n)
                await asyncio.sleep(interval)
            elif _is_weekday(now) and _minutes(now) >= _CLOSE_MIN and not st.eod_sent:
                data = await compute_charts(settings, period="5m", source="live", days=1)
                day_label = now.strftime("%a %b %d")  # e.g. "Mon Jun 16"
                notifier.send(format_eod_summary(data["charts"], day_label))
                st.eod_sent = True
                logger.info("watcher sent end-of-day summary")
                await asyncio.sleep(60)
            else:
                # pre-market, weekend, or EOD already sent — idle cheaply
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("watcher stopped")
            raise
        except Exception as exc:  # noqa: BLE001 - one bad poll shouldn't kill the loop
            logger.warning("watcher poll failed: %s", exc)
            await asyncio.sleep(interval)
