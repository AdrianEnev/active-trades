from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from active_trades.models import Position, StopOrder, Trade


STOP_ORDER_TYPES = {"stop-loss", "stop-loss-limit"}
ACTIVE_ORDER_STATUSES = {"in-progress", "pending", "open"}


def find_protective_stop(position: Position, orders: list[StopOrder]) -> tuple[StopOrder | None, str, str]:
    candidates = [
        order
        for order in orders
        if order.market_id == position.market_id
        and order.account_index == position.account_index
        and order.exchange == position.exchange
        and _matches_position(position, order)
        and order.reduce_only
        and order.status in ACTIVE_ORDER_STATUSES
        and order.order_type in STOP_ORDER_TYPES
        and order.trigger_price > 0
    ]
    if not candidates:
        return None, "missing", "No SL"

    expected_side = "sell" if position.side == "long" else "buy"
    side_matches = [order for order in candidates if order.side == expected_side]
    if position.side == "long":
        protective = [order for order in side_matches if order.trigger_price < position.avg_entry_price]
        if protective:
            return max(protective, key=lambda order: order.trigger_price), "at-risk", ""
        profit_lock = [order for order in side_matches if order.trigger_price >= position.avg_entry_price]
        if profit_lock:
            return min(profit_lock, key=lambda order: order.trigger_price), "locked", "SL beyond breakeven"
    else:
        protective = [order for order in side_matches if order.trigger_price > position.avg_entry_price]
        if protective:
            return min(protective, key=lambda order: order.trigger_price), "at-risk", ""
        profit_lock = [order for order in side_matches if order.trigger_price <= position.avg_entry_price]
        if profit_lock:
            return max(profit_lock, key=lambda order: order.trigger_price), "locked", "SL beyond breakeven"

    fallback = min(candidates, key=lambda order: abs(order.trigger_price - position.avg_entry_price))
    return fallback, "unknown", "SL side unclear"


def _matches_position(position: Position, order: StopOrder) -> bool:
    if position.exchange != "bingx":
        return True
    raw = order.raw or {}
    position_side = str(raw.get("positionSide") or "BOTH").lower()
    if position_side not in {"both", position.side}:
        return False
    order_position_id = str(raw.get("positionId") or raw.get("positionID") or "")
    if order_position_id and position.position_id and order_position_id != position.position_id:
        return False
    # An opening hedge order or a stop for the other leg cannot protect this position.
    return order.side == ("sell" if position.side == "long" else "buy")


def risk_usd(position: Position, stop_order: StopOrder | None) -> Decimal | None:
    if stop_order is None:
        return None
    if position.side == "long":
        per_unit = position.avg_entry_price - stop_order.trigger_price
    else:
        per_unit = stop_order.trigger_price - position.avg_entry_price
    if per_unit <= 0:
        return Decimal("0")
    return per_unit * position.size


def infer_opened_at_from_trades(position: Position, trades: list[Trade]) -> datetime | None:
    after_size = position.signed_size
    if after_size == 0:
        return None

    target_is_long = after_size > 0
    for trade in sorted(
        (trade for trade in trades if trade.market_id == position.market_id),
        key=lambda trade: trade.timestamp or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    ):
        before_size = after_size - trade.signed_delta
        if target_is_long and before_size <= 0 < after_size:
            return trade.timestamp
        if not target_is_long and before_size >= 0 > after_size:
            return trade.timestamp
        after_size = before_size

    return None
