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

from ..config import Settings, get_settings
from ..marketclock import OPEN_MIN, after_close, day_key, market_hours, minutes, now_et
from .telegram import (
    TelegramNotifier,
    format_entry,
    format_eod_summary,
    format_exit,
    format_market_open,
)

logger = logging.getLogger("degeneratr.notify")


class _State:
    """Per-run alert bookkeeping, reset when the trading day rolls over."""

    def __init__(self) -> None:
        self.day: str | None = None
        self.baselined = False
        self.open_briefed = False
        self.eod_sent = False
        self.seen_entries: dict[str, set] = {}   # symbol -> {(n, entry_time)} alerted ENTRY
        self.exited: dict[str, set] = {}          # symbol -> {(n, entry_time)} alerted EXIT

    def roll(self, day: str) -> None:
        if day != self.day:
            self.day = day
            self.baselined = False
            self.open_briefed = False
            self.eod_sent = False
            self.seen_entries.clear()
            self.exited.clear()


def _diff_and_alert(charts: list[dict], st: _State, notifier: TelegramNotifier) -> int:
    """Emit ENTRY when a trade first appears and EXIT when it first *closes*. Keyed
    by the entry — so an 'open' position (whose mark-to-market exit moves every
    poll) isn't re-alerted, and no EXIT fires until it actually closes. No signal
    alerts. On the baseline poll, only record state (no sends). Returns sends."""
    sent = 0
    for c in charts:
        sym = c["symbol"]
        seen = st.seen_entries.setdefault(sym, set())
        exited = st.exited.setdefault(sym, set())
        for t in c.get("trades", []):
            ek = (t["n"], t["entry_time"])
            closed = t.get("exit_reason") != "open"
            if st.baselined:
                if ek not in seen and notifier.send(format_entry(sym, t)):
                    sent += 1
                if closed and ek not in exited and notifier.send(format_exit(sym, t)):
                    sent += 1
            # update state regardless (baseline records the current world silently)
            seen.add(ek)
            if closed:
                exited.add(ek)
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
        now = now_et()
        st.roll(day_key(now))
        try:
            if market_hours(now):
                data = await compute_charts(settings, period="5m", source="live", days=3)
                if not st.baselined:
                    _diff_and_alert(data["charts"], st, notifier)  # silent baseline
                    st.baselined = True
                    logger.info("watcher baselined %d symbols", len(data["charts"]))
                    if data["charts"]:
                        # Near the open → full market-open briefing (pre-market
                        # movers + setup). A mid-day restart instead gets a light
                        # heartbeat, so it doesn't falsely announce "market open".
                        if not st.open_briefed and minutes(now) <= OPEN_MIN + 30:
                            notifier.send(format_market_open(
                                data["charts"], now.strftime("%a %b %d")))
                            st.open_briefed = True
                        else:
                            syms = ", ".join(c["symbol"] for c in data["charts"])
                            notifier.send(f"🟢 degeneratr live — watching {syms}.")
                else:
                    n = _diff_and_alert(data["charts"], st, notifier)
                    if n:
                        logger.info("watcher sent %d alert(s)", n)
                await asyncio.sleep(interval)
            elif after_close(now) and not st.eod_sent:
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
