"""Cancel a limit order from the dashboard, together with the SL/TP orders that exist only for it.

The entry is cancelled first. SL/TP orders are cancelled only after a fresh read shows the entry has
left the book, and they are re-selected from that read, so a rejected cancel or an entry that filled
in the meantime never strips the stop from an open position.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from active_trades.bingx_api import (
    BingxApiError,
    BingxRestClient,
    limit_orders_from_dicts as bingx_limit_orders_from_dicts,
    positions_from_dicts as bingx_positions_from_dicts,
    stop_orders_from_dicts as bingx_stop_orders_from_dicts,
)
from active_trades.lighter_api import (
    AuthProvider,
    LighterApiError,
    LighterRestClient,
    cancel_orders as lighter_cancel_orders,
    limit_orders_from_dicts,
    positions_from_account,
    signing_key_for,
    stop_orders_from_dicts,
)
from active_trades.models import AccountSnapshot, LimitOrder, Position, StopOrder, TrackerSnapshot
from active_trades.risk import (
    ACTIVE_ORDER_STATUSES,
    _awaits_entry,
    _has_parent,
    _linked_to_entry,
    _matches_entry_leg,
    _same_entry_leg,
    _same_leg,
)
from active_trades.settings import Settings


ExitOrder = StopOrder | LimitOrder
EXIT_KINDS = {"stop-loss": "SL", "stop-loss-limit": "SL", "take-profit": "TP", "take-profit-limit": "TP"}
_EXCHANGE_ERRORS = (LighterApiError, BingxApiError)


@dataclass(frozen=True)
class CancelPlan:
    """What the confirmation prompt shows, and the most the cancel may remove."""

    entry: LimitOrder
    source: str
    exits: tuple[ExitOrder, ...] = ()
    kept: int = 0
    positions_unknown: bool = False
    filled: Decimal = Decimal("0")


@dataclass(frozen=True)
class CancelOutcome:
    message: str
    ok: bool


@dataclass(frozen=True)
class AccountOrders:
    stop_orders: list[StopOrder]
    limit_orders: list[LimitOrder]
    active_ids: set[str]
    positions: list[Position] | None  # None when the position read failed.


def numbered_limit_orders(snapshot: TrackerSnapshot) -> list[tuple[LimitOrder, AccountSnapshot]]:
    """Limit orders in dashboard row order: row N is element N - 1."""
    return [(order, account) for account in snapshot.accounts for order in account.limit_orders]


def plan_cancel(order: LimitOrder, account: AccountSnapshot) -> CancelPlan:
    exits, kept, unknown = select_exits(
        order,
        stop_orders=account.stop_orders,
        limit_orders=account.limit_orders,
        positions=[trade.position for trade in account.trades],
        positions_known=account.positions_state == "available",
    )
    return CancelPlan(
        entry=order,
        source="BingX" if account.account.exchange == "bingx" else f"Lighter · {account.account.label}",
        exits=tuple(exits),
        kept=kept,
        positions_unknown=unknown,
        filled=_filled_amount(order),
    )


def select_exits(
    entry: LimitOrder,
    *,
    stop_orders: Iterable[StopOrder],
    limit_orders: Iterable[LimitOrder],
    positions: Iterable[Position],
    positions_known: bool,
) -> tuple[list[ExitOrder], int, bool]:
    """SL/TP orders that exist only for `entry`, how many related ones stay, and whether
    unreadable positions were the reason any stayed.

    An exit that protects an open position on the entry's leg, or that may serve another entry
    on that leg, stays. Exits linked to a different order are never touched.
    """
    if entry.reduce_only:
        # Cancelling an exit order leaves the position's other protection alone.
        return [], 0, False
    limit_orders = list(limit_orders)
    candidates: dict[str, ExitOrder] = {}
    for order in (*stop_orders, *limit_orders):
        if _is_exit_for(entry, order):
            candidates.setdefault(order.order_id, order)
    others = [order for order in limit_orders if order.order_id != entry.order_id]
    other_entries = [order for order in others if not order.reduce_only and _same_entry_leg(entry, order)]
    position_open = any(_same_leg(entry, position) for position in positions)

    exits: list[ExitOrder] = []
    kept = 0
    unknown = False
    for order in candidates.values():
        linked = _linked_to_entry(entry, order)
        if linked and _awaits_entry(order):
            # Dormant until this entry fills, so it protects nothing else.
            exits.append(order)
            continue
        if not linked and (_has_parent(order) or any(_linked_to_entry(other, order) for other in others)):
            continue
        if not positions_known:
            kept += 1
            unknown = True
        elif position_open or (not linked and other_entries):
            kept += 1
        else:
            exits.append(order)
    return exits, kept, unknown


def exit_kind(order: ExitOrder) -> str:
    # A reducing limit on the closing side can only take profit.
    return "TP" if isinstance(order, LimitOrder) else EXIT_KINDS.get(order.order_type, "SL/TP")


def exit_price(order: ExitOrder) -> Decimal:
    return order.price if isinstance(order, LimitOrder) else order.trigger_price


def _is_exit_for(entry: LimitOrder, order: ExitOrder) -> bool:
    if not order.order_id or order.order_id == entry.order_id:
        return False
    if (order.exchange, order.account_index, order.market_id) != (entry.exchange, entry.account_index, entry.market_id):
        return False
    if order.side != ("sell" if entry.side == "buy" else "buy") or not _matches_entry_leg(entry, order):
        return False
    if isinstance(order, StopOrder) and (
        order.order_type not in EXIT_KINDS or order.status not in ACTIVE_ORDER_STATUSES
    ):
        return False
    return order.reduce_only or _linked_to_entry(entry, order)


def _filled_amount(order: LimitOrder) -> Decimal:
    raw = order.raw or {}
    try:
        filled = Decimal(str(raw.get("filled_base_amount" if order.exchange == "lighter" else "executedQty") or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return filled if filled.is_finite() and filled > 0 else Decimal("0")


class _LighterGateway:
    def __init__(
        self,
        *,
        settings: Settings,
        client: LighterRestClient,
        auth_provider: AuthProvider,
        entry: LimitOrder,
        cancel_fn: Callable[..., list[str | None]],
    ) -> None:
        self.account_index = entry.account_index
        self.market_index = int(entry.market_id)
        self.api_private_key, self.api_key_index = signing_key_for(
            entry.account_index,
            default_account_index=settings.account_index,
            api_private_key=settings.api_private_key,
            api_key_index=settings.api_key_index,
        )
        self.base_url = settings.base_url
        self.client = client
        self.auth_provider = auth_provider
        self.cancel_fn = cancel_fn

    def cancel(self, orders: list[ExitOrder]) -> list[str | None]:
        return self.cancel_fn(
            base_url=self.base_url,
            account_index=self.account_index,
            api_private_key=self.api_private_key,
            api_key_index=self.api_key_index,
            market_index=self.market_index,
            order_indexes=[_lighter_order_index(order) for order in orders],
        )

    def read(self) -> AccountOrders:
        token = self.auth_provider.token_for(self.account_index)
        raw_orders = self.client.fetch_active_orders(self.account_index, token)
        try:
            raw_account = self.client.fetch_account_by_index(self.account_index, active_only=True)
            positions: list[Position] | None = positions_from_account(raw_account, self.account_index)
        except LighterApiError:
            positions = None
        return AccountOrders(
            stop_orders=stop_orders_from_dicts(raw_orders, account_index=self.account_index),
            limit_orders=limit_orders_from_dicts(raw_orders, account_index=self.account_index),
            active_ids={_raw_id(raw, "order_id", "order_index") for raw in raw_orders},
            positions=positions,
        )


class _BingxGateway:
    def __init__(self, client: BingxRestClient) -> None:
        self.client = client

    def cancel(self, orders: list[ExitOrder]) -> list[str | None]:
        errors: list[str | None] = []
        for order in orders:
            try:
                self.client.cancel_order(symbol=str(order.market_id), order_id=order.order_id)
                errors.append(None)
            except BingxApiError as exc:
                errors.append(str(exc))
        return errors

    def read(self) -> AccountOrders:
        raw_orders = self.client.fetch_active_orders()
        try:
            positions: list[Position] | None = bingx_positions_from_dicts(self.client.fetch_positions())
        except BingxApiError:
            positions = None
        return AccountOrders(
            stop_orders=bingx_stop_orders_from_dicts(raw_orders, include_take_profit=True),
            limit_orders=bingx_limit_orders_from_dicts(raw_orders),
            active_ids={_raw_id(raw, "orderId", "orderID") for raw in raw_orders},
            positions=positions,
        )


class OrderCanceller:
    """Carry out a confirmed plan, re-reading the account between the entry and its SL/TP."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: LighterRestClient,
        auth_provider: AuthProvider,
        bingx_client: BingxRestClient | None,
        lighter_cancel: Callable[..., list[str | None]] = lighter_cancel_orders,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 8,
        interval_seconds: float = 0.5,
    ) -> None:
        self.settings = settings
        self.client = client
        self.auth_provider = auth_provider
        self.bingx_client = bingx_client
        self.lighter_cancel = lighter_cancel
        self.sleep = sleep
        self.attempts = attempts
        self.interval_seconds = interval_seconds

    def cancel(self, plan: CancelPlan) -> CancelOutcome:
        entry = plan.entry
        name = f"{entry.symbol} {entry.side.upper()} limit ({plan.source})"
        try:
            gateway = self._gateway(entry)
            [entry_error] = gateway.cancel([entry])
        except _EXCHANGE_ERRORS as exc:
            return CancelOutcome(f"{name} not cancelled: {exc}", False)

        # Lighter applies an accepted cancel asynchronously; a rejected one needs no waiting.
        state = self._settle(gateway, {entry.order_id}, wait=entry_error is None)
        if state is None:
            sent = "sent" if entry_error is None else f"failed ({entry_error})"
            return CancelOutcome(
                f"{name} cancel {sent}, but the account could not be re-read, so SL/TP were left in place.",
                False,
            )
        if entry.order_id in state.active_ids:
            reason = f": {entry_error.rstrip('.')}" if entry_error else "; it is still open"
            return CancelOutcome(f"{name} not cancelled{reason}. SL/TP left in place.", False)
        # A rejected cancel of an order that has left the book means it filled or expired.
        headline = f"Cancelled {name}" if entry_error is None else f"{name} is no longer open"

        # Re-select from fresh data: an entry that filled first now has a position to protect.
        exits, kept, unknown = select_exits(
            entry,
            stop_orders=state.stop_orders,
            limit_orders=state.limit_orders,
            positions=state.positions or [],
            positions_known=state.positions is not None,
        )
        confirmed = {order.order_id for order in plan.exits}
        targets = [order for order in exits if order.order_id in confirmed]
        target_ids = {order.order_id for order in targets}
        failed: list[tuple[ExitOrder, str | None]] = []
        if targets:
            try:
                errors = gateway.cancel(targets)
            except _EXCHANGE_ERRORS as exc:
                return CancelOutcome(f"{headline}, but its SL/TP could not be cancelled: {exc}", False)
            after = self._settle(gateway, target_ids, wait=True)
            if after is None:
                return CancelOutcome(
                    f"{headline}. SL/TP cancels sent but not confirmed; check the exchange.", False
                )
            failed = [(order, error) for order, error in zip(targets, errors) if order.order_id in after.active_ids]
        retained = [
            order for order in plan.exits
            if order.order_id in state.active_ids and order.order_id not in target_ids
        ]
        removed = len(plan.exits) - len(retained) - len(failed)

        parts = [headline + (f" and {removed} SL/TP." if removed else ".")]
        if kept:
            parts.append(f"Kept {kept} SL/TP protecting the open position or other limits.")
        if unknown:
            parts.append("Positions could not be read, so related SL/TP were kept.")
        if failed:
            reason = next((error for _order, error in failed if error), "check the exchange")
            parts.append(f"{len(failed)} SL/TP still open: {reason}")
        return CancelOutcome(" ".join(parts), not failed)

    def _gateway(self, entry: LimitOrder) -> _LighterGateway | _BingxGateway:
        if entry.exchange == "bingx":
            if self.bingx_client is None:
                raise BingxApiError("BingX is not configured.")
            return _BingxGateway(self.bingx_client)
        return _LighterGateway(
            settings=self.settings,
            client=self.client,
            auth_provider=self.auth_provider,
            entry=entry,
            cancel_fn=self.lighter_cancel,
        )

    def _settle(self, gateway: Any, order_ids: set[str], *, wait: bool) -> AccountOrders | None:
        """Re-read until none of `order_ids` is active, or attempts run out; None if never readable."""
        latest: AccountOrders | None = None
        for attempt in range(self.attempts if wait else 1):
            if attempt:
                self.sleep(self.interval_seconds)
            try:
                latest = gateway.read()
            except _EXCHANGE_ERRORS:
                continue
            if not order_ids & latest.active_ids:
                break
        return latest


CancelStage = Literal["select", "confirm", "busy"]


@dataclass(frozen=True)
class CancelPrompt:
    stage: CancelStage
    digits: str = ""
    number: int | None = None
    plan: CancelPlan | None = None
    hint: str = ""


class CancelController:
    """Turn the pager's cancel keys into one reviewed and confirmed cancel at a time."""

    NOTICE_SECONDS = 60.0

    def __init__(
        self,
        canceller: OrderCanceller,
        pager: Any,
        *,
        executor: Executor,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.canceller = canceller
        self.pager = pager
        self.executor = executor
        self.clock = clock
        self._plan: CancelPlan | None = None
        self._running: tuple[Future, CancelPlan] | None = None
        self._notice: tuple[str, float] | None = None
        self._hint = ""

    @property
    def notice(self) -> str | None:
        if self._notice is None or self.clock() - self._notice[1] >= self.NOTICE_SECONDS:
            return None
        return self._notice[0]

    def tick(self, snapshot: TrackerSnapshot) -> bool:
        """Advance the prompt; True when a finished cancel has made the snapshot stale."""
        finished = self._collect()
        pager = self.pager
        if pager.cancel_confirmed:
            pager.cancel_confirmed = False
            self._submit(snapshot)
        elif pager.cancel_stage == "confirm" and self._plan is None:
            self._choose(snapshot)
        elif pager.cancel_stage != "confirm":
            self._plan = None
        if pager.cancel_stage is None:
            self._hint = ""
        return finished

    def prompt(self) -> CancelPrompt | None:
        if self._running is not None:
            return CancelPrompt("busy", plan=self._running[1])
        if self.pager.cancel_stage == "select":
            return CancelPrompt("select", digits=self.pager.cancel_digits, hint=self._hint)
        if self.pager.cancel_stage == "confirm" and self._plan is not None:
            return CancelPrompt("confirm", number=self.pager.cancel_row, plan=self._plan)
        return None

    def _choose(self, snapshot: TrackerSnapshot) -> None:
        if self._running is not None:
            self._set_notice("Wait for the current cancel to finish.")
            self.pager.close_cancel()
            return
        number = self.pager.cancel_row
        rows = numbered_limit_orders(snapshot)
        if number is None or not 1 <= number <= len(rows):
            self._hint = f"No limit order #{number}."
            self.pager.reopen_cancel()
            return
        self._hint = ""
        self._plan = plan_cancel(*rows[number - 1])

    def _submit(self, snapshot: TrackerSnapshot) -> None:
        plan, self._plan = self._plan, None
        if plan is None:
            # Confirmed before the order was shown; require a reviewed confirmation.
            return
        entry = plan.entry
        if self._running is not None:
            self._set_notice("Wait for the current cancel to finish.")
        elif not any(
            (order.exchange, order.account_index, order.order_id)
            == (entry.exchange, entry.account_index, entry.order_id)
            for order, _account in numbered_limit_orders(snapshot)
        ):
            self._set_notice(f"{entry.symbol} {entry.side.upper()} limit is no longer open; nothing was cancelled.")
        else:
            self._running = (self.executor.submit(self.canceller.cancel, plan), plan)

    def _collect(self) -> bool:
        if self._running is None or not self._running[0].done():
            return False
        future, plan = self._running
        self._running = None
        try:
            message = future.result().message
        except Exception:
            message = f"Cancelling {plan.entry.symbol} failed unexpectedly; check the exchange."
        self._set_notice(message)
        return True

    def _set_notice(self, message: str) -> None:
        self._notice = (message, self.clock())


def _lighter_order_index(order: ExitOrder) -> int:
    raw = order.raw or {}
    for value in (raw.get("order_index"), order.order_id):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    raise LighterApiError(f"Order {order.order_id or '?'} has no Lighter order index.")


def _raw_id(raw: dict[str, Any], *names: str) -> str:
    for name in names:
        value = raw.get(name)
        if value is not None and value != "":
            return str(value)
    return ""
