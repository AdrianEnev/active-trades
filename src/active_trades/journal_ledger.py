"""Durable executions and deterministic position lifecycles for the journal."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from active_trades.journal_executions import Execution, JournalError, number


ZERO = Decimal("0")


@dataclass
class JournalTrade:
    id: str
    exchange: str
    account: int
    market: str
    symbol: str
    side: str
    lane: str
    position_id: str
    opened_ms: int
    quantity: Decimal = ZERO
    entered: Decimal = ZERO
    exited: Decimal = ZERO
    entry_price: Decimal = ZERO
    pnl: Decimal = ZERO
    closed_ms: int | None = None
    complete: bool = True
    calculated_pnl: bool = False
    original_risk: Decimal | None = None
    current_risk: Decimal | None = None
    capital: Decimal | None = None
    potential_rr: Decimal | None = None
    exit_price: Decimal | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.market, self.lane, self.position_id

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side == "long" else -self.quantity


def replay(executions: list[Execution], initial: dict[tuple[str, str, str], Decimal]) -> list[JournalTrade]:
    """Average-cost accounting; scale-ins/partials share a row, reversals split it.

    An initial position that predates available history is deliberately incomplete.
    Its whole cycle is excluded, but subsequent cycles in that market remain valid.
    """
    active: dict[tuple[str, str, str], JournalTrade] = {}
    result: list[JournalTrade] = []
    balances = dict(initial)

    def sort_key(fill: Execution):
        suffix = fill.id.rsplit(":", 1)[-1]
        return fill.time_ms, int(suffix) if suffix.isdigit() else 0, fill.id

    for fill in sorted(executions, key=sort_key):
        key = fill.position_key
        delta, price, fee = number(fill.delta), number(fill.price), number(fill.fee)
        before = balances.get(key, ZERO)
        if fill.before is not None and number(fill.before) != before:
            raise JournalError(f"{fill.exchange.title()} {fill.symbol}: execution history has a quantity gap; sync paused.")
        trade = active.get(key)
        if trade is None and before:
            trade = JournalTrade(
                f"incomplete:{fill.id}", fill.exchange, fill.account, fill.market, fill.symbol,
                "long" if before > 0 else "short", fill.lane, fill.position_id, fill.time_ms,
                quantity=abs(before), entered=abs(before), complete=False,
            )
            active[key] = trade
            result.append(trade)
        after = before + delta
        if fill.lane == "LONG" and after < 0 or fill.lane == "SHORT" and after > 0:
            raise JournalError(f"BingX {fill.symbol}: closing fills exceed the known position; sync paused.")

        remainder = abs(delta)
        if trade is not None and before * delta < 0:
            closing = min(abs(before), abs(delta))
            if fill.pnl is not None:
                profit = number(fill.pnl)
            else:
                profit = (price - trade.entry_price) * closing * (1 if before > 0 else -1)
                trade.calculated_pnl = True
            trade.pnl += profit + fee * closing / abs(delta)
            trade.quantity -= closing
            trade.exited += closing
            remainder -= closing
            if not trade.quantity:
                trade.closed_ms = fill.time_ms
                trade.exit_price = price
                del active[key]
                trade = None

        if remainder:
            if trade is None:
                trade = JournalTrade(
                    f"{fill.id}:{'long' if delta > 0 else 'short'}", fill.exchange, fill.account,
                    fill.market, fill.symbol, "long" if delta > 0 else "short",
                    fill.lane, fill.position_id, fill.time_ms,
                )
                result.append(trade)
                active[key] = trade
            trade.entry_price = (trade.entry_price * trade.quantity + price * remainder) / (trade.quantity + remainder)
            trade.quantity += remainder
            trade.entered += remainder
            trade.pnl += fee * remainder / abs(delta)
        balances[key] = after
    return result


class JournalLedger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, scope TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scopes (scope TEXT PRIMARY KEY, checkpoint INTEGER NOT NULL, initial TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trade_metadata (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)

    def close(self) -> None:
        self.db.close()

    def setting(self, key: str, default: str) -> str:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, default))
        return self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0]

    def scope(self, scope: str):
        row = self.db.execute("SELECT checkpoint, initial FROM scopes WHERE scope=?", (scope,)).fetchone()
        if row is None:
            return None
        return row[0], {tuple(item[0]): number(item[1]) for item in json.loads(row[1])}

    def save(self, scope: str, fills: list[Execution], checkpoint: int, initial: dict) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO executions VALUES (?, ?, ?)", [
                (fill.id, scope, json.dumps(fill.payload())) for fill in fills
            ])
            self.db.execute("INSERT OR REPLACE INTO scopes VALUES (?, ?, ?)", (
                scope, checkpoint, json.dumps([[list(key), str(value)] for key, value in initial.items()]),
            ))

    def executions(self, scope: str) -> list[Execution]:
        return [Execution(**json.loads(row[0])) for row in self.db.execute(
            "SELECT payload FROM executions WHERE scope=?", (scope,),
        )]

    def metadata(self, trade_id: str) -> dict:
        row = self.db.execute("SELECT payload FROM trade_metadata WHERE id=?", (trade_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def remember(self, trade_id: str, metadata: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO trade_metadata VALUES (?, ?)", (trade_id, json.dumps(metadata)))
