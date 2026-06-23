"""Telegram Bot API notifier.

Posts plain-text alerts to a chat via the Bot API. Stays inert (logs one warning)
until ``telegram_bot_token`` + ``telegram_chat_id`` are configured, so importing /
constructing it never breaks the rest of the app.

Uses ``requests`` when available (pulled in transitively by yfinance) and falls
back to the stdlib ``urllib`` so there's no hard new dependency.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

from ..config import Settings, get_settings

logger = logging.getLogger("degeneratr.notify")

try:  # requests is present via yfinance, but don't hard-depend on it
    import requests  # type: ignore
except Exception:  # noqa: BLE001
    requests = None  # type: ignore

_API = "https://api.telegram.org/bot{token}/sendMessage"


def _et(epoch: int, fmt: str = "%H:%M:%S") -> str:
    """Format an epoch built by ``_epoch`` (ET wall-clock stored as UTC) back to
    ET wall-clock — so ``gmtime`` is exactly right here."""
    return time.strftime(fmt, time.gmtime(epoch))


class TelegramNotifier:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self._token = s.telegram_bot_token.strip()
        self._chat_id = s.telegram_chat_id.strip()
        self._warned = False

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, text: str) -> bool:
        """Deliver a message. Returns True on success, False if unconfigured or the
        API call failed (never raises — notifications must not crash the caller)."""
        if not self.configured:
            if not self._warned:
                logger.warning(
                    "Telegram notifier inert: set TELEGRAM_BOT_TOKEN and "
                    "TELEGRAM_CHAT_ID in .env to enable alerts."
                )
                self._warned = True
            return False
        url = _API.format(token=self._token)
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        try:
            if requests is not None:
                r = requests.post(url, json=payload, timeout=10)
                if r.status_code != 200:
                    # Telegram's body says exactly why (e.g. "chat not found",
                    # "Unauthorized") — surface it instead of a bare status.
                    logger.warning("Telegram send failed: HTTP %s — %s",
                                   r.status_code, (r.text or "")[:300])
                    return False
                return True
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                    return resp.status == 200
            except urllib.error.HTTPError as he:
                body = he.read().decode("utf-8", "replace")[:300]
                logger.warning("Telegram send failed: HTTP %s — %s", he.code, body)
                return False
        except Exception as exc:  # noqa: BLE001 - never let a notification crash the loop
            logger.warning("Telegram send error: %s", exc)
            return False


# ---- message builders (plain text + emoji; no parse_mode → no escaping traps) ----

def format_signal(symbol: str, sig: dict) -> str:
    arrow = "🟢▲" if sig["dir"] == "bull" else "🔴▼"
    word = "BULLISH" if sig["dir"] == "bull" else "BEARISH"
    reason = sig.get("reason") or ""
    return (
        f"{arrow} SIGNAL · {symbol} {word}\n"
        f"confluence {sig.get('score', '?')} @ {sig.get('price', '?')}  ({_et(sig['time'])} ET)"
        + (f"\n{reason}" if reason else "")
    )


def format_entry(symbol: str, t: dict) -> str:
    side = "LONG / CALL" if t["dir"] == "bull" else "SHORT / PUT"
    icon = "🟢" if t["dir"] == "bull" else "🟣"
    return (
        f"{icon} ENTRY #{t['n']} · {symbol} {side}\n"
        f"in {t['entry_price']:.2f} × {t.get('qty', 0)}  ({_et(t['entry_time'])} ET)"
    )


def format_exit(symbol: str, t: dict) -> str:
    icon = "✅" if t["win"] else "❌"
    pnl = t["pnl"]
    sign = "+" if pnl >= 0 else "-"
    return (
        f"{icon} EXIT #{t['n']} · {symbol} {t.get('exit_reason', '')}\n"
        f"out {t['exit_price']:.2f}  P&L {sign}${abs(pnl):,.0f} "
        f"({sign}{abs(t.get('pnl_pct', 0)):.1f}%)  ({_et(t['exit_time'])} ET)"
    )


def _strategy_overview() -> list[str]:
    """Setup lines derived from the LIVE config (so the briefing never drifts)."""
    import inspect

    from ..backtester.underlying import UnderlyingBacktester
    from ..strategies.price_action import PriceActionStrategy

    s = PriceActionStrategy()
    sig = inspect.signature(UnderlyingBacktester.__init__)
    tp = sig.parameters["take_profit_pct"].default * 100
    sl = sig.parameters["stop_loss_pct"].default * 100
    inds = []
    if s.use_ema:
        inds.append(f"EMA {s.ema_fast}/{s.ema_slow}")
    if s.use_vwap:
        inds.append("VWAP")
    if s.use_macd:
        inds.append("MACD")
    if s.use_rsi:
        inds.append("RSI")
    if s.use_bbands:
        inds.append(f"Bollinger ({s.bb_mode})")
    gate = f" + ADX({s.adx_len})≥{s.adx_min:.0f} gate" if s.adx_min > 0 else ""
    return [
        "\U0001F4CB Setup",
        f"• Signal: {', '.join(inds)}",
        f"• Trigger: ≥{s.min_score} confluence{gate}",
        "• Timeframe: 15-minute bars",
        f"• Exit: +{tp:.1f}% take-profit / -{sl:.1f}% stop",
        "• Trade: 0DTE OTM call (bull) / put (bear)",
        "• Window: 09:30–16:00 ET",
    ]


def format_market_open(charts: list[dict], day_label: str) -> str:
    """Market-open briefing: watchlist ranked by pre-market gap (vs prior close),
    the top name to watch, and a quick setup overview."""
    rows = sorted(charts, key=lambda c: abs(c.get("day_change_pct", 0) or 0), reverse=True)
    lines = [f"☀️ MARKET OPEN — {day_label}", ""]
    if rows:
        t = rows[0]
        g = t.get("day_change_pct", 0) or 0
        lines.append(
            f"\U0001F440 Top watch: {t['symbol']}  "
            f"({'+' if g >= 0 else ''}{g:.2f}% gap · ATR {t.get('atr_pct', 0) or 0:.2f}%)"
        )
        lines.append("")
    lines.append("Pre-market — most active (vs prior close):")
    for i, c in enumerate(rows):
        g = c.get("day_change_pct", 0) or 0
        atr = c.get("atr_pct", 0) or 0
        bullet = "\U0001F525" if i < 2 else "•"
        lines.append(f"{bullet} {c['symbol']} {'+' if g >= 0 else ''}{g:.2f}%  ·  ATR {atr:.2f}%")
    lines.append("")
    lines += _strategy_overview()
    lines.append("")
    lines.append("Live alerts on signals, entries & exits. Summary at the close. \U0001F340")
    return "\n".join(lines)


def format_eod_summary(charts: list[dict], day_label: str) -> str:
    lines = [f"📊 END-OF-DAY · {day_label}"]
    total_trades = total_net = total_wins = total_losses = 0
    for c in sorted(charts, key=lambda x: x.get("net_pnl", 0), reverse=True):
        trades = c.get("trades", [])
        if not trades:
            continue
        wins = sum(1 for t in trades if t["win"])
        losses = len(trades) - wins
        net = c.get("net_pnl", 0)
        total_trades += len(trades)
        total_net += net
        total_wins += wins
        total_losses += losses
        sign = "+" if net >= 0 else "-"
        lines.append(
            f"{c['symbol']:<5} {len(trades)}t  {wins}W/{losses}L  {sign}${abs(net):,.0f}"
        )
    if total_trades == 0:
        lines.append("No trades today.")
    else:
        win_rate = total_wins / total_trades * 100 if total_trades else 0
        sign = "+" if total_net >= 0 else "-"
        lines.append("—")
        lines.append(
            f"TOTAL {total_trades}t  {total_wins}W/{total_losses}L  "
            f"{win_rate:.0f}% win  net {sign}${abs(total_net):,.0f}"
        )
    return "\n".join(lines)
