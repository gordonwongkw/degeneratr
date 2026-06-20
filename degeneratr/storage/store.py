"""SQLite-backed bar store.

Tiger caps intraday history at ~3 trading days, so this store accumulates bars
across runs/days: every fetch is upserted here and backtests read the growing
union. One file, no server, safe to run repeatedly (``INSERT OR REPLACE``
dedupes on key + timestamp).
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..data.base import Bar


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class BarStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS underlying_bars(
                    symbol TEXT, period TEXT, time_ms INTEGER,
                    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                    PRIMARY KEY(symbol, period, time_ms))"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS option_bars(
                    identifier TEXT, period TEXT, time_ms INTEGER,
                    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                    PRIMARY KEY(identifier, period, time_ms))"""
            )
            # Realized round-trips from the algorithm — the persisted performance
            # record. Natural-key (symbol + entry + exit) so re-runs upsert.
            c.execute(
                """CREATE TABLE IF NOT EXISTS trade_log(
                    symbol TEXT, entry_ms INTEGER, exit_ms INTEGER,
                    direction TEXT, right TEXT,
                    entry_price REAL, exit_price REAL, qty INTEGER,
                    pnl REAL, pnl_pct REAL, win INTEGER, exit_reason TEXT,
                    PRIMARY KEY(symbol, entry_ms, exit_ms))"""
            )

    # ---- writes ----
    def save_underlying(self, symbol: str, period: str, bars: list[Bar]) -> int:
        rows = [
            (symbol, period, _to_ms(b.time), b.open, b.high, b.low, b.close, b.volume)
            for b in bars
        ]
        if not rows:
            return 0
        with self._lock, self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO underlying_bars VALUES(?,?,?,?,?,?,?,?)", rows
            )
        return len(rows)

    def save_option(self, identifier: str, period: str, bars: list[Bar]) -> int:
        rows = [
            (identifier, period, _to_ms(b.time), b.open, b.high, b.low, b.close, b.volume)
            for b in bars
        ]
        if not rows:
            return 0
        with self._lock, self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO option_bars VALUES(?,?,?,?,?,?,?,?)", rows
            )
        return len(rows)

    def save_trades(self, symbol: str, round_trips: list) -> int:
        """Upsert realized round-trips for a symbol (idempotent on entry+exit)."""
        rows = []
        for rt in round_trips:
            direction = "bull" if rt.right == "CALL" else "bear"
            win = 1 if getattr(rt, "win", rt.pnl >= 0) else 0
            rows.append((
                symbol, _to_ms(rt.entry_time), _to_ms(rt.exit_time),
                direction, rt.right, rt.entry_price, rt.exit_price, rt.quantity,
                rt.pnl, rt.pnl_pct, win, rt.exit_reason,
            ))
        if not rows:
            return 0
        with self._lock, self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO trade_log VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
        return len(rows)

    def load_trades(
        self, symbol: Optional[str] = None,
        begin: Optional[datetime] = None, end: Optional[datetime] = None,
    ) -> list[dict]:
        q = ("SELECT symbol, entry_ms, exit_ms, direction, right, entry_price, "
             "exit_price, qty, pnl, pnl_pct, win, exit_reason FROM trade_log")
        clauses, args = [], []
        if symbol:
            clauses.append("symbol=?"); args.append(symbol)
        if begin is not None:
            clauses.append("entry_ms>=?"); args.append(_to_ms(begin))
        if end is not None:
            clauses.append("entry_ms<=?"); args.append(_to_ms(end))
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY entry_ms"
        cols = ["symbol", "entry_ms", "exit_ms", "direction", "right", "entry_price",
                "exit_price", "qty", "pnl", "pnl_pct", "win", "exit_reason"]
        with self._conn() as c:
            return [dict(zip(cols, r)) for r in c.execute(q, tuple(args)).fetchall()]

    @staticmethod
    def _agg(rows: list[tuple]) -> dict:
        """Aggregate (pnl, win) rows into a performance summary block."""
        trades = len(rows)
        wins = [p for p, w in rows if w]
        losses = [p for p, w in rows if not w]
        gross_win = sum(wins)
        gross_loss = -sum(losses)  # positive magnitude
        net = sum(p for p, _ in rows)
        pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
        return {
            "trades": trades, "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / trades, 4) if trades else 0.0,
            "net_pnl": round(net, 2),
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "profit_factor": None if pf == float("inf") else round(pf, 2),
            "expectancy": round(net / trades, 2) if trades else 0.0,
        }

    def performance_summary(self) -> dict:
        """Overall + per-symbol stats over the whole persisted trade log."""
        with self._conn() as c:
            rows = c.execute("SELECT symbol, pnl, win FROM trade_log").fetchall()
        by_symbol: dict[str, list[tuple]] = {}
        for sym, pnl, win in rows:
            by_symbol.setdefault(sym, []).append((pnl, win))
        per = {sym: self._agg(r) for sym, r in by_symbol.items()}
        overall = self._agg([(pnl, win) for _, pnl, win in rows])
        return {"overall": overall,
                "per_symbol": dict(sorted(per.items(), key=lambda kv: kv[1]["net_pnl"], reverse=True))}

    # ---- reads ----
    def load_underlying(
        self, symbol: str, period: str, begin: datetime, end: datetime
    ) -> list[Bar]:
        with self._conn() as c:
            cur = c.execute(
                """SELECT time_ms, open, high, low, close, volume FROM underlying_bars
                   WHERE symbol=? AND period=? AND time_ms BETWEEN ? AND ?
                   ORDER BY time_ms""",
                (symbol, period, _to_ms(begin), _to_ms(end)),
            )
            return [
                Bar(symbol, datetime.fromtimestamp(t / 1000), o, h, l, cl, v)
                for (t, o, h, l, cl, v) in cur.fetchall()
            ]

    def load_option(
        self, identifier: str, period: str, begin: datetime, end: datetime
    ) -> list[Bar]:
        with self._conn() as c:
            cur = c.execute(
                """SELECT time_ms, open, high, low, close, volume FROM option_bars
                   WHERE identifier=? AND period=? AND time_ms BETWEEN ? AND ?
                   ORDER BY time_ms""",
                (identifier, period, _to_ms(begin), _to_ms(end)),
            )
            return [
                Bar(identifier, datetime.fromtimestamp(t / 1000), o, h, l, cl, v)
                for (t, o, h, l, cl, v) in cur.fetchall()
            ]

    def option_identifiers(self, period: Optional[str] = None) -> list[str]:
        q = "SELECT DISTINCT identifier FROM option_bars"
        args: tuple = ()
        if period:
            q += " WHERE period=?"
            args = (period,)
        with self._conn() as c:
            return [r[0] for r in c.execute(q, args).fetchall()]

    def underlying_symbols(self, period: Optional[str] = None) -> list[str]:
        q = "SELECT DISTINCT symbol FROM underlying_bars"
        args: tuple = ()
        if period:
            q += " WHERE period=?"
            args = (period,)
        with self._conn() as c:
            return [r[0] for r in c.execute(q, args).fetchall()]

    def coverage(self) -> dict:
        """Summarize what's stored, per table and symbol."""
        out: dict = {"underlying": [], "options": {}}
        with self._conn() as c:
            for sym, period, n, lo, hi in c.execute(
                """SELECT symbol, period, COUNT(*), MIN(time_ms), MAX(time_ms)
                   FROM underlying_bars GROUP BY symbol, period"""
            ).fetchall():
                out["underlying"].append({
                    "symbol": sym, "period": period, "bars": n,
                    "from": datetime.fromtimestamp(lo / 1000).isoformat() if lo else None,
                    "to": datetime.fromtimestamp(hi / 1000).isoformat() if hi else None,
                })
            row = c.execute(
                "SELECT COUNT(DISTINCT identifier), COUNT(*), MIN(time_ms), MAX(time_ms) FROM option_bars"
            ).fetchone()
            contracts, obars, lo, hi = row if row else (0, 0, None, None)
            out["options"] = {
                "contracts": contracts or 0, "bars": obars or 0,
                "from": datetime.fromtimestamp(lo / 1000).isoformat() if lo else None,
                "to": datetime.fromtimestamp(hi / 1000).isoformat() if hi else None,
            }
        return out
