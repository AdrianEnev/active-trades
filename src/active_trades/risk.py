from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from active_trades.models import AccountSnapshot, ActiveTrade, LimitOrder, Position, StopOrder, TrackerSnapshot, Trade


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
        and not _awaits_entry(order)
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
    if order_position_id and order_position_id != position.position_id:
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


def position_risk_total(snapshot: TrackerSnapshot) -> tuple[Decimal | None, bool]:
    values: list[Decimal | None] = []
    incomplete = not snapshot.accounts_complete or (not snapshot.accounts and bool(snapshot.errors))
    for account in snapshot.accounts:
        incomplete = incomplete or account.positions_state != "available"
        values.extend(_position_risk_value(trade, account) for trade in account.trades)
    return _risk_total(values, incomplete)


def limit_risk_total(snapshot: TrackerSnapshot) -> tuple[Decimal | None, bool]:
    """Additional entry risk if every remaining opening limit fills and hits SL."""
    values: list[Decimal | None] = []
    incomplete = not snapshot.accounts_complete or (not snapshot.accounts and bool(snapshot.errors))
    for account in snapshot.accounts:
        incomplete = incomplete or account.orders_state != "available"
        values.extend(
            _limit_risk_value(order, account)
            for order in account.limit_orders
            if not order.reduce_only
        )
    return _risk_total(values, incomplete)


def _risk_total(values: list[Decimal | None], incomplete: bool) -> tuple[Decimal | None, bool]:
    known = [value for value in values if value is not None]
    incomplete = incomplete or len(known) != len(values)
    return (sum(known, Decimal("0")) if known or not incomplete else None), incomplete


def _position_risk_value(trade: ActiveTrade, account: AccountSnapshot) -> Decimal | None:
    if trade.risk_usd is None or trade.risk_state == "unknown":
        return None
    matching = [
        stop for stop in account.stop_orders
        if _valid_stop(stop, trade.position.account_index, trade.position.exchange, trade.position.market_id)
        and _matches_position(trade.position, stop)
        and stop.side == ("sell" if trade.position.side == "long" else "buy")
        and stop.trigger_price == trade.stop_loss
        and not _awaits_entry(stop)
    ]
    if account.stop_orders and trade.stop_loss is not None and not matching:
        return None
    if matching and not any(_closes_whole_position(stop) for stop in matching):
        capacity = sum((stop.remaining_base_amount or Decimal("0") for stop in matching), Decimal("0"))
        position = trade.position
        exposure = sum((
            other.position.size for other in account.trades
            if (other.position.account_index, other.position.exchange, other.position.market_id, other.position.side)
            == (position.account_index, position.exchange, position.market_id, position.side)
            and any(_matches_position(other.position, stop) for stop in matching)
        ), Decimal("0"))
        if capacity < exposure:
            return None
    return trade.risk_usd


def _limit_risk_value(order: LimitOrder, account: AccountSnapshot) -> Decimal | None:
    if order.stop_loss is not None:
        if order.stop_loss_quantity is not None and order.stop_loss_quantity < _entry_quantity(order):
            return None
        return _entry_risk(order, order.stop_loss)

    candidates = [
        stop for stop in account.stop_orders
        if _valid_stop(stop, order.account_index, order.exchange, order.market_id)
        and stop.side == ("sell" if order.side == "buy" else "buy")
        and _matches_entry_leg(order, stop)
    ]
    linked = [stop for stop in candidates if _linked_to_entry(order, stop)]
    if linked:
        # A linked OTO child on Lighter deliberately has zero size until entry fills.
        covered = any(
            _closes_whole_position(stop)
            or (order.exchange == "lighter" and stop.remaining_base_amount == 0)
            or (stop.remaining_base_amount is not None and stop.remaining_base_amount >= _entry_quantity(order))
            for stop in linked
        )
        if not covered or len({stop.trigger_price for stop in linked}) != 1:
            return None
        return _entry_risk(order, linked[0].trigger_price)

    # A standalone stop must survive until the limit fills and cover the whole
    # resulting leg. Reserve its capacity for current positions and ALL entries.
    shared = [
        stop for stop in candidates
        if not _has_parent(stop)
        and not _awaits_entry(stop)
        and not any(_linked_to_entry(entry, stop) for entry in account.limit_orders)
        and (stop.trigger_price < order.price if order.side == "buy" else stop.trigger_price > order.price)
    ]
    if not shared or len({stop.trigger_price for stop in shared}) != 1:
        return None
    if not any(_closes_whole_position(stop) for stop in shared):
        if account.positions_state != "available":
            return None
        exposure = sum((
            trade.position.size for trade in account.trades
            if _same_leg(order, trade.position)
        ), Decimal("0")) + sum((
            entry.remaining_base_amount for entry in account.limit_orders
            if not entry.reduce_only and _same_entry_leg(order, entry)
        ), Decimal("0"))
        capacity = sum((stop.remaining_base_amount or Decimal("0") for stop in shared), Decimal("0"))
        if capacity < exposure:
            return None
    return _entry_risk(order, shared[0].trigger_price)


def _entry_risk(order: LimitOrder, stop_price: Decimal) -> Decimal:
    distance = order.price - stop_price if order.side == "buy" else stop_price - order.price
    return max(distance, Decimal("0")) * order.remaining_base_amount


def _entry_quantity(order: LimitOrder) -> Decimal:
    """An attached sized SL must cover fills already made by the same entry too."""
    raw = order.raw or {}
    names = ("initial_base_amount",) if order.exchange == "lighter" else ("origQty", "quantity")
    for name in names:
        try:
            quantity = Decimal(str(raw.get(name)))
        except (ArithmeticError, ValueError):
            continue
        if quantity.is_finite() and quantity > 0:
            return max(quantity, order.remaining_base_amount)
    return order.remaining_base_amount


def _valid_stop(stop: StopOrder, account_index: int, exchange: str, market_id: int | str) -> bool:
    return (
        stop.account_index == account_index and stop.exchange == exchange and stop.market_id == market_id
        and stop.reduce_only and stop.status in ACTIVE_ORDER_STATUSES
        and stop.order_type in STOP_ORDER_TYPES and stop.trigger_price > 0
    )


def _closes_whole_position(stop: StopOrder) -> bool:
    raw = stop.raw or {}
    return stop.exchange == "bingx" and str(raw.get("closePosition", "")).lower() in {"true", "1"}


def _awaits_entry(stop: StopOrder) -> bool:
    return str((stop.raw or {}).get("trigger_status") or "").lower() == "parent-order"


def _same_leg(order: LimitOrder, position: Position) -> bool:
    return (
        order.account_index == position.account_index and order.exchange == position.exchange
        and order.market_id == position.market_id
        and position.side == ("long" if order.side == "buy" else "short")
    )


def _same_entry_leg(first: LimitOrder, second: LimitOrder) -> bool:
    return (
        first.account_index == second.account_index and first.exchange == second.exchange
        and first.market_id == second.market_id and first.side == second.side
    )


def _matches_entry_leg(order: LimitOrder, stop: StopOrder) -> bool:
    if order.exchange != "bingx":
        return True
    raw = stop.raw or {}
    side = "long" if order.side == "buy" else "short"
    if str(raw.get("positionSide") or "BOTH").lower() not in {"both", side}:
        return False
    stop_position = str(raw.get("positionId") or raw.get("positionID") or "")
    entry_raw = order.raw or {}
    entry_position = str(entry_raw.get("positionId") or entry_raw.get("positionID") or "")
    if not stop_position:
        return True
    return entry_position == stop_position


def _identifiers(order: LimitOrder | StopOrder) -> set[str]:
    raw = order.raw or {}
    return {
        str(value) for value in (order.order_id, raw.get("order_id"), raw.get("order_index"))
        if value is not None and str(value) not in {"", "0", "-1"}
    }


def _has_parent(stop: StopOrder) -> bool:
    return any(str((stop.raw or {}).get(name) or "0") not in {"0", "-1"}
               for name in ("parent_order_id", "parent_order_index"))


def _linked_to_entry(entry: LimitOrder, stop: StopOrder) -> bool:
    raw = stop.raw or {}
    parents = {str(raw.get(name)) for name in ("parent_order_id", "parent_order_index") if raw.get(name)}
    entry_raw = entry.raw or {}
    children = {str(entry_raw.get(name)) for name in ("to_trigger_order_id_0", "to_trigger_order_id_1")
                if entry_raw.get(name)}
    return bool((_identifiers(entry) & parents) or (_identifiers(stop) & children))


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
