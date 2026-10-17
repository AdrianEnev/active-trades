"""Capture the initial stop/target plan separately from realized trade results."""
from __future__ import annotations

from decimal import Decimal

from active_trades.journal_ledger import JournalTrade
from active_trades.models import AccountSnapshot, ActiveTrade, LimitOrder, Position, StopOrder
from active_trades.risk import (
    ACTIVE_ORDER_STATUSES, _awaits_entry, _closes_current_position, _matches_position,
)


def potential_rr(entry: Decimal, stop: Decimal, target: Decimal, side: str) -> Decimal | None:
    if side not in {"long", "short"} or any(not value.is_finite() or value <= 0 for value in (entry, stop, target)):
        return None
    direction = 1 if side == "long" else -1
    risk = (entry - stop) * direction
    reward = (target - entry) * direction
    return reward / risk if risk > 0 and reward > 0 else None


def _belongs_to_position(order: StopOrder | LimitOrder, position: Position, account: AccountSnapshot) -> bool:
    if (order.exchange, order.account_index, str(order.market_id)) != (position.exchange, position.account_index, str(position.market_id)):
        return False
    if not order.reduce_only or order.side != ("sell" if position.side == "long" else "buy") or not _matches_position(position, order):
        return False
    raw = order.raw or {}
    # A shared hedge-leg order cannot identify one of several separate positions.
    if position.exchange == "bingx" and not (raw.get("positionId") or raw.get("positionID")):
        matching = [trade for trade in account.trades if trade.position.market_id == position.market_id
                    and trade.position.side == position.side]
        if len(matching) > 1:
            return False
    return True


def _target_price(position: Position, account: AccountSnapshot, entry: Decimal) -> Decimal | None:
    prices = []
    for order in account.stop_orders:
        if (order.order_type not in {"take-profit", "take-profit-limit"}
                or order.status not in ACTIVE_ORDER_STATUSES or _awaits_entry(order)
                or not _belongs_to_position(order, position, account)):
            continue
        if order.remaining_base_amount is not None and order.remaining_base_amount <= 0 and not _closes_current_position(order):
            continue
        prices.append(order.trigger_price)
    for order in account.limit_orders:
        if order.remaining_base_amount > 0 and _belongs_to_position(order, position, account):
            prices.append(order.price)
    profitable = [price for price in prices if price.is_finite() and price > 0
                  and (price > entry if position.side == "long" else price < entry)]
    # Potential RR describes the final target when there are several partial TPs.
    return (max(profitable) if position.side == "long" else min(profitable)) if profitable else None


def capture_plan(trade: JournalTrade, active: ActiveTrade, account: AccountSnapshot, metadata: dict) -> None:
    """Keep the first valid plan across stop moves, partial exits and restarts."""
    if account.orders_state != "available":
        return
    if not metadata.get("plan_entry") and not trade.exited and active.risk_state != "unknown":
        entry, stop = active.position.avg_entry_price, active.stop_loss
        if (stop is not None and all(value.is_finite() and value > 0 for value in (entry, stop))
                and (entry - stop) * (1 if trade.side == "long" else -1) > 0):
            metadata.update(plan_entry=str(entry), plan_stop=str(stop))
    if metadata.get("potential_rr") is not None or not metadata.get("plan_entry"):
        return
    entry, stop = Decimal(metadata["plan_entry"]), Decimal(metadata["plan_stop"])
    target = _target_price(active.position, account, entry)
    ratio = potential_rr(entry, stop, target, trade.side) if target is not None else None
    if ratio is not None:
        metadata.update(plan_target=str(target), potential_rr=str(ratio))
