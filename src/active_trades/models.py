from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal


Side = Literal["long", "short"]


@dataclass(frozen=True)
class AccountInfo:
    index: int
    name: str = ""
    description: str = ""
    display_name: str = ""
    l1_address: str = ""
    status: int | None = None
    collateral: Decimal | None = None
    available_balance: Decimal | None = None
    raw: dict[str, Any] | None = None
    exchange: str = "lighter"

    @property
    def label(self) -> str:
        return self.display_name.strip() or self.name.strip() or self.description.strip() or "Account"


@dataclass(frozen=True)
class Position:
    account_index: int
    market_id: int | str
    symbol: str
    side: Side
    size: Decimal
    avg_entry_price: Decimal
    position_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    raw: dict[str, Any] | None = None
    exchange: str = "lighter"
    position_id: str = ""
    realized_pnl: Decimal | None = None

    @property
    def key(self) -> str:
        key = f"{self.account_index}:{self.market_id}:{self.side}"
        if self.exchange == "lighter":
            return key
        suffix = f":{self.position_id}" if self.position_id else ""
        return f"{self.exchange}:{key}{suffix}"

    @property
    def signed_size(self) -> Decimal:
        return self.size if self.side == "long" else -self.size


@dataclass(frozen=True)
class StopOrder:
    account_index: int
    market_id: int | str
    side: str
    trigger_price: Decimal
    remaining_base_amount: Decimal | None = None
    created_at: datetime | None = None
    order_id: str = ""
    order_type: str = ""
    reduce_only: bool = False
    status: str = ""
    raw: dict[str, Any] | None = None
    exchange: str = "lighter"


@dataclass(frozen=True)
class LimitOrder:
    account_index: int
    market_id: int | str
    symbol: str
    side: Literal["buy", "sell"]
    price: Decimal
    remaining_base_amount: Decimal
    created_at: datetime | None = None
    order_id: str = ""
    reduce_only: bool = False
    raw: dict[str, Any] | None = None
    exchange: str = "lighter"
    stop_loss: Decimal | None = None
    stop_loss_quantity: Decimal | None = None


@dataclass(frozen=True)
class Trade:
    account_index: int
    market_id: int
    size: Decimal
    price: Decimal
    timestamp: datetime | None
    signed_delta: Decimal
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ActiveTrade:
    row_number: int
    position: Position
    opened_at: datetime | None
    opened_at_source: str
    stop_loss: Decimal | None
    risk_usd: Decimal | None
    risk_state: str = ""
    risk_note: str = ""
    profit_target_reached: bool = False


@dataclass(frozen=True)
class AccountSnapshot:
    account: AccountInfo
    trades: tuple[ActiveTrade, ...]
    errors: tuple[str, ...] = ()
    limit_orders: tuple[LimitOrder, ...] = ()
    orders_state: str = "available"
    pnl_collected_at: datetime | None = None
    positions_state: str = "available"
    stop_orders: tuple[StopOrder, ...] = ()


@dataclass(frozen=True)
class TrackerSnapshot:
    collected_at: datetime
    accounts: tuple[AccountSnapshot, ...]
    errors: tuple[str, ...] = ()
    pnl_collected_at: datetime | None = None
    accounts_complete: bool = True


def now_utc() -> datetime:
    return datetime.now(UTC)
