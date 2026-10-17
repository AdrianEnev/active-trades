from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any

from active_trades.models import ActiveTrade


_PROFIT_THRESHOLD = Decimal("0.49")


@dataclass(frozen=True)
class _PositionProfit:
    exchange: str
    account_index: int
    original_risk_usd: Decimal | None
    last_size: Decimal
    last_realized_pnl: Decimal | None
    cumulative_partial_profit: Decimal
    partial_close_seen: bool
    profit_data_complete: bool
    profit_target_reached: bool
    observed_at: datetime


class PositionProfitTracker:
    """Remember original risk and realized profits from observed partial closes.

    Exchange realized PnL can be a lifetime counter. Its initial value and
    changes without a size reduction are baselines, never closing profits.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._positions: dict[str, _PositionProfit] = {}
        self._account_times: dict[tuple[str, int], datetime] = {}

    def restore(self, payload: dict) -> None:
        """Load valid records, ignoring corrupt entries without guessing values."""
        positions: dict[str, _PositionProfit] = {}
        account_times: dict[tuple[str, int], datetime] = {}
        if isinstance(payload, dict):
            raw_positions = payload.get("positions")
            if isinstance(raw_positions, dict):
                for key, raw in raw_positions.items():
                    if not isinstance(key, str) or not key or not isinstance(raw, dict):
                        continue
                    record = _restore_record(raw)
                    if record is None:
                        continue
                    positions[key] = record
                    scope = (record.exchange, record.account_index)
                    account_times[scope] = max(record.observed_at, account_times.get(scope, record.observed_at))
            raw_accounts = payload.get("accounts")
            if isinstance(raw_accounts, list):
                for raw in raw_accounts:
                    if not isinstance(raw, dict):
                        continue
                    scope = _scope(raw)
                    observed_at = _restore_datetime(raw.get("observed_at"))
                    if scope is not None and observed_at is not None:
                        account_times[scope] = max(observed_at, account_times.get(scope, observed_at))
        with self._lock:
            self._positions = positions
            self._account_times = account_times

    def dump(self) -> dict[str, Any]:
        """Return an independent JSON-compatible snapshot, including flat accounts."""
        with self._lock:
            return {
                "positions": {
                    key: {
                        "exchange": record.exchange,
                        "account_index": record.account_index,
                        "original_risk_usd": _decimal_text(record.original_risk_usd),
                        "last_size": str(record.last_size),
                        "last_realized_pnl": _decimal_text(record.last_realized_pnl),
                        "cumulative_partial_profit": str(record.cumulative_partial_profit),
                        "partial_close_seen": record.partial_close_seen,
                        "profit_data_complete": record.profit_data_complete,
                        "profit_target_reached": record.profit_target_reached,
                        "observed_at": record.observed_at.isoformat(),
                    }
                    for key, record in sorted(self._positions.items())
                },
                "accounts": [
                    {
                        "exchange": scope[0],
                        "account_index": scope[1],
                        "observed_at": observed_at.isoformat(),
                    }
                    for scope, observed_at in sorted(self._account_times.items())
                ],
            }

    def observe_account(
        self,
        trades: tuple[ActiveTrade, ...],
        *,
        exchange: str,
        account_index: int,
        observed_at: datetime,
    ) -> tuple[ActiveTrade, ...]:
        """Observe one complete account position set and annotate its titles."""
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Profit observations require a timezone-aware timestamp.")
        observed_at = observed_at.astimezone(UTC)
        scope = (exchange, account_index)
        with self._lock:
            latest = self._account_times.get(scope)
            if latest is not None and observed_at <= latest:
                return tuple(self._annotate(trade) for trade in trades)

            active_keys: set[str] = set()
            result: list[ActiveTrade] = []
            for trade in trades:
                position = trade.position
                if (position.exchange, position.account_index) != scope:
                    result.append(trade)
                    continue
                active_keys.add(position.key)
                size = _decimal(position.size)
                if size is None or size <= 0:
                    result.append(self._annotate(trade))
                    continue
                risk = _decimal(trade.risk_usd)
                if risk is not None and risk < 0:
                    risk = None
                realized_pnl = _decimal(position.realized_pnl)
                previous = self._positions.get(position.key)
                if previous is None:
                    record = _PositionProfit(
                        exchange=exchange,
                        account_index=account_index,
                        original_risk_usd=risk,
                        last_size=size,
                        last_realized_pnl=realized_pnl,
                        cumulative_partial_profit=Decimal("0"),
                        partial_close_seen=False,
                        profit_data_complete=True,
                        profit_target_reached=False,
                        observed_at=observed_at,
                    )
                else:
                    original_risk = previous.original_risk_usd
                    if original_risk is None and not previous.partial_close_seen and size >= previous.last_size:
                        original_risk = risk
                    cumulative_profit = previous.cumulative_partial_profit
                    partial_close_seen = previous.partial_close_seen
                    profit_data_complete = previous.profit_data_complete
                    if size < previous.last_size:
                        partial_close_seen = True
                        if realized_pnl is not None and previous.last_realized_pnl is not None:
                            cumulative_profit += realized_pnl - previous.last_realized_pnl
                        else:
                            # An unknown partial could be a loss; a known subtotal
                            # cannot establish the cumulative profit target.
                            profit_data_complete = False
                    reached = previous.profit_target_reached or (
                        partial_close_seen
                        and profit_data_complete
                        and original_risk is not None
                        and cumulative_profit > original_risk * _PROFIT_THRESHOLD
                    )
                    record = replace(
                        previous,
                        original_risk_usd=original_risk,
                        last_size=size,
                        last_realized_pnl=realized_pnl,
                        cumulative_partial_profit=cumulative_profit,
                        partial_close_seen=partial_close_seen,
                        profit_data_complete=profit_data_complete,
                        profit_target_reached=reached,
                        observed_at=observed_at,
                    )
                self._positions[position.key] = record
                result.append(replace(trade, profit_target_reached=record.profit_target_reached))

            self._positions = {
                key: record
                for key, record in self._positions.items()
                if (
                    (record.exchange, record.account_index) != scope
                    or key in active_keys
                    or record.observed_at > observed_at
                )
            }
            self._account_times[scope] = observed_at
            return tuple(result)

    def _annotate(self, trade: ActiveTrade) -> ActiveTrade:
        record = self._positions.get(trade.position.key)
        return replace(trade, profit_target_reached=record.profit_target_reached if record is not None else False)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _scope(raw: dict[str, Any]) -> tuple[str, int] | None:
    exchange = raw.get("exchange")
    account_index = raw.get("account_index")
    if (
        not isinstance(exchange, str)
        or not exchange.strip()
        or not isinstance(account_index, int)
        or isinstance(account_index, bool)
        or account_index < 0
    ):
        return None
    return exchange, account_index


def _restore_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _restore_record(raw: dict[str, Any]) -> _PositionProfit | None:
    scope = _scope(raw)
    observed_at = _restore_datetime(raw.get("observed_at"))
    size = _decimal(raw.get("last_size"))
    cumulative_profit = _decimal(raw.get("cumulative_partial_profit"))
    risk = _decimal(raw.get("original_risk_usd"))
    realized_pnl = _decimal(raw.get("last_realized_pnl"))
    partial_close_seen = raw.get("partial_close_seen")
    profit_data_complete = raw.get("profit_data_complete")
    reached = raw.get("profit_target_reached")
    if (
        scope is None
        or observed_at is None
        or size is None
        or size <= 0
        or cumulative_profit is None
        or (raw.get("original_risk_usd") is not None and (risk is None or risk < 0))
        or (raw.get("last_realized_pnl") is not None and realized_pnl is None)
        or not isinstance(partial_close_seen, bool)
        or not isinstance(profit_data_complete, bool)
        or not isinstance(reached, bool)
        or (not partial_close_seen and (cumulative_profit != 0 or not profit_data_complete))
        or (reached and (not partial_close_seen or risk is None))
    ):
        return None
    return _PositionProfit(
        exchange=scope[0],
        account_index=scope[1],
        original_risk_usd=risk,
        last_size=size,
        last_realized_pnl=realized_pnl,
        cumulative_partial_profit=cumulative_profit,
        partial_close_seen=partial_close_seen,
        profit_data_complete=profit_data_complete,
        profit_target_reached=reached,
        observed_at=observed_at,
    )
