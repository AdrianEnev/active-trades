from concurrent.futures import Future
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from active_trades.bingx_api import (
    BingxAuthUnavailable,
    limit_orders_from_dicts as bingx_limit_orders_from_dicts,
    stop_orders_from_dicts as bingx_stop_orders_from_dicts,
)
from active_trades.lighter_api import LighterApiError, limit_orders_from_dicts, stop_orders_from_dicts
from active_trades.models import AccountInfo, AccountSnapshot, ActiveTrade, Position, TrackerSnapshot
from active_trades.order_cancel import (
    CancelController,
    OrderCanceller,
    exit_kind,
    numbered_limit_orders,
    plan_cancel,
)
from active_trades.terminal import DashboardPager
from active_trades.ui import render_plain, render_snapshot

ACCOUNT = 7


def lighter_raw(
    order_index, *, kind="limit", ask=False, price="100", trigger="0", remaining="1", filled="0",
    reduce_only=False, trigger_status="na", parent=0, children=(), market=1,
):
    return {
        "order_index": order_index, "order_id": str(order_index), "market_index": market, "type": kind,
        "status": "open", "is_ask": ask, "price": price, "trigger_price": trigger,
        "remaining_base_amount": remaining, "initial_base_amount": remaining, "filled_base_amount": filled,
        "reduce_only": reduce_only, "trigger_status": trigger_status,
        "parent_order_index": parent, "parent_order_id": str(parent),
        "to_trigger_order_id_0": str(children[0]) if children else "0",
        "to_trigger_order_id_1": str(children[1]) if len(children) > 1 else "0",
    }


def otoco(entry=10, sl=11, tp=12, *, filled="0", child_status="parent-order"):
    """A buy entry at 100 with its linked SL at 90 and TP at 120."""
    return [
        lighter_raw(entry, filled=filled, children=(sl, tp)),
        lighter_raw(sl, kind="stop-loss", ask=True, price="89", trigger="90", remaining="0",
                    reduce_only=True, trigger_status=child_status, parent=entry),
        lighter_raw(tp, kind="take-profit", ask=True, price="121", trigger="120", remaining="0",
                    reduce_only=True, trigger_status=child_status, parent=entry),
    ]


def standalone_sl(order_index=21, trigger="90"):
    return lighter_raw(order_index, kind="stop-loss", ask=True, price="89", trigger=trigger, reduce_only=True)


def long_position(market=1):
    return Position(ACCOUNT, market, "ETH", "long", Decimal("1"), Decimal("100"))


def lighter_account(raw_orders, *, positions=(), positions_state="available", index=ACCOUNT, label="Main"):
    return AccountSnapshot(
        AccountInfo(index, display_name=label),
        tuple(ActiveTrade(n, p, None, "state", None, None) for n, p in enumerate(positions, start=1)),
        limit_orders=tuple(limit_orders_from_dicts(raw_orders, account_index=index, symbols={1: "ETH", 2: "BTC"})),
        stop_orders=tuple(stop_orders_from_dicts(raw_orders, account_index=index)),
        positions_state=positions_state,
    )


def entry_of(account, order_id="10"):
    return next(order for order in account.limit_orders if order.order_id == order_id)


def ids(orders):
    return sorted(order.order_id for order in orders)


# -- Selecting the SL/TP that belong to an entry ---------------------------------------------


def test_linked_sl_and_tp_awaiting_the_entry_are_cancelled_with_it():
    account = lighter_account(otoco())
    plan = plan_cancel(entry_of(account), account)
    assert ids(plan.exits) == ["11", "12"]
    assert sorted(exit_kind(order) for order in plan.exits) == ["SL", "TP"]
    assert plan.kept == 0
    assert plan.source == "Lighter · Main"


def test_standalone_sl_tp_and_reducing_limit_go_when_nothing_else_needs_them():
    raw = [
        lighter_raw(10),
        standalone_sl(21),
        lighter_raw(22, kind="take-profit", ask=True, price="121", trigger="120", reduce_only=True),
        lighter_raw(23, ask=True, price="130", reduce_only=True),  # A reducing limit used as TP.
    ]
    account = lighter_account(raw)
    plan = plan_cancel(entry_of(account), account)
    assert ids(plan.exits) == ["21", "22", "23"]
    assert exit_kind(next(order for order in plan.exits if order.order_id == "23")) == "TP"


def test_standalone_stop_protecting_an_open_position_is_kept():
    account = lighter_account([lighter_raw(10), standalone_sl(21)], positions=(long_position(),))
    plan = plan_cancel(entry_of(account), account)
    assert plan.exits == ()
    assert plan.kept == 1


def test_standalone_stop_shared_with_another_entry_is_kept():
    account = lighter_account([lighter_raw(10), lighter_raw(15, price="95"), standalone_sl(21)])
    plan = plan_cancel(entry_of(account), account)
    assert plan.exits == ()
    assert plan.kept == 1


def test_children_of_another_entry_are_never_touched():
    account = lighter_account([lighter_raw(10), *otoco(entry=30, sl=31, tp=32)])
    plan = plan_cancel(entry_of(account), account)
    assert plan.exits == ()
    assert plan.kept == 0


def test_live_child_of_a_partly_filled_entry_stays_while_the_position_is_open():
    account = lighter_account(
        otoco(filled="0.4", child_status="mark-price"), positions=(long_position(),)
    )
    plan = plan_cancel(entry_of(account), account)
    assert plan.exits == ()
    assert plan.kept == 2
    assert plan.filled == Decimal("0.4")


def test_unreadable_positions_keep_standalone_stops_but_not_dormant_children():
    account = lighter_account([*otoco(), standalone_sl(21)], positions_state="unavailable")
    plan = plan_cancel(entry_of(account), account)
    assert ids(plan.exits) == ["11", "12"]
    assert plan.kept == 1
    assert plan.positions_unknown


def test_other_markets_accounts_and_entry_side_orders_are_ignored():
    raw = [
        lighter_raw(10),
        standalone_sl(21) | {"market_index": 2},
        lighter_raw(22, kind="stop-loss", ask=False, price="111", trigger="110", reduce_only=True),
        lighter_raw(23, kind="stop-loss", ask=True, price="89", trigger="90", reduce_only=False),
    ]
    account = lighter_account(raw)
    assert plan_cancel(entry_of(account), account).exits == ()


def test_cancelling_a_reduce_only_exit_leaves_other_protection_alone():
    account = lighter_account([lighter_raw(10, ask=True, price="130", reduce_only=True), standalone_sl(21)])
    plan = plan_cancel(entry_of(account), account)
    assert plan.exits == ()
    assert plan.kept == 0


def test_bingx_hedge_mode_only_selects_the_entry_legs_stops():
    raw = [
        {"orderId": 1, "symbol": "BTC-USDT", "type": "LIMIT", "side": "BUY", "positionSide": "LONG",
         "price": "100", "origQty": "1", "executedQty": "0", "status": "NEW"},
        {"orderId": 2, "symbol": "BTC-USDT", "type": "STOP_MARKET", "side": "SELL", "positionSide": "LONG",
         "stopPrice": "90", "origQty": "1", "status": "NEW"},
        {"orderId": 3, "symbol": "BTC-USDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "positionSide": "LONG",
         "stopPrice": "120", "origQty": "1", "status": "NEW"},
        # The short leg's stop is a BUY; it is not this long entry's exit.
        {"orderId": 4, "symbol": "BTC-USDT", "type": "STOP_MARKET", "side": "BUY", "positionSide": "SHORT",
         "stopPrice": "130", "origQty": "1", "status": "NEW"},
    ]
    account = AccountSnapshot(
        AccountInfo(0, name="Main", exchange="bingx"), (),
        limit_orders=tuple(bingx_limit_orders_from_dicts(raw)),
        stop_orders=tuple(bingx_stop_orders_from_dicts(raw, include_take_profit=True)),
    )
    plan = plan_cancel(account.limit_orders[0], account)
    assert ids(plan.exits) == ["2", "3"]
    assert plan.source == "BingX"


# -- Carrying out a confirmed cancel ------------------------------------------------------------


class FakeLighter:
    """Lighter REST/auth/signing doubles. Each read returns the next order book state."""

    def __init__(self, *reads, positions=(), cancel_errors=()):
        self.reads = list(reads)
        self.cancel_calls = []
        self.cancel_errors = list(cancel_errors)
        self.client = Mock()
        self.client.fetch_active_orders.side_effect = self._read
        self.client.fetch_account_by_index.return_value = {"positions": list(positions)}
        self.auth = Mock()
        self.auth.token_for.return_value = "read-token"

    def _read(self, _account_index, _token):
        state = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(state, Exception):
            raise state
        return state

    def cancel(self, **kwargs):
        self.cancel_calls.append(kwargs)
        errors = self.cancel_errors.pop(0) if self.cancel_errors else None
        return errors or [None] * len(kwargs["order_indexes"])


def settings(**overrides):
    return SimpleNamespace(
        account_index=ACCOUNT, api_private_key="base-private-key", api_key_index=3,
        base_url="https://lighter.example", **overrides,
    )


def canceller(fake, **kwargs):
    return OrderCanceller(
        settings(), client=fake.client, auth_provider=fake.auth, bingx_client=kwargs.pop("bingx", None),
        lighter_cancel=fake.cancel, sleep=lambda _seconds: None, **kwargs,
    )


RAW_POSITION = {"market_id": 1, "position": "1", "sign": 1, "avg_entry_price": "100", "symbol": "ETH"}


def test_entry_is_cancelled_before_its_sl_tp_and_both_are_confirmed_gone():
    raw = otoco()
    account = lighter_account(raw)
    fake = FakeLighter(raw, raw[1:], raw[1:], [])  # Entry still listed once, then gone, then SL/TP gone.
    outcome = canceller(fake).cancel(plan_cancel(entry_of(account), account))

    assert [call["order_indexes"] for call in fake.cancel_calls] == [[10], [11, 12]]
    assert fake.cancel_calls[0]["api_private_key"] == "base-private-key"
    assert fake.cancel_calls[0]["api_key_index"] == 3
    assert fake.cancel_calls[0]["market_index"] == 1
    assert outcome.ok
    assert outcome.message == "Cancelled ETH BUY limit (Lighter · Main) and 2 SL/TP."


def test_children_cancelled_by_the_exchange_with_their_parent_count_as_removed():
    raw = otoco()
    account = lighter_account(raw)
    fake = FakeLighter([])
    outcome = canceller(fake).cancel(plan_cancel(entry_of(account), account))
    assert [call["order_indexes"] for call in fake.cancel_calls] == [[10]]
    assert outcome.ok
    assert outcome.message.endswith("and 2 SL/TP.")


def test_rejected_entry_cancel_leaves_every_sl_tp_in_place():
    raw = otoco()
    account = lighter_account(raw)
    fake = FakeLighter(raw, cancel_errors=[["invalid signature"]])
    outcome = canceller(fake).cancel(plan_cancel(entry_of(account), account))
    assert len(fake.cancel_calls) == 1
    assert not outcome.ok
    assert outcome.message == "ETH BUY limit (Lighter · Main) not cancelled: invalid signature. SL/TP left in place."
    assert fake.client.fetch_active_orders.call_count == 1  # A rejected cancel is not polled.


def test_accepted_cancel_that_never_leaves_the_book_leaves_sl_tp_in_place():
    raw = otoco()
    account = lighter_account(raw)
    fake = FakeLighter(raw)
    outcome = canceller(fake, attempts=3).cancel(plan_cancel(entry_of(account), account))
    assert len(fake.cancel_calls) == 1
    assert fake.client.fetch_active_orders.call_count == 3
    assert "still open. SL/TP left in place." in outcome.message


def test_entry_that_filled_before_the_cancel_keeps_its_now_live_stops():
    raw = otoco()
    account = lighter_account(raw)
    after_fill = otoco(child_status="mark-price")[1:]
    fake = FakeLighter(after_fill, positions=[RAW_POSITION], cancel_errors=[["order not found"]])
    outcome = canceller(fake).cancel(plan_cancel(entry_of(account), account))
    assert [call["order_indexes"] for call in fake.cancel_calls] == [[10]]
    assert not outcome.message.startswith("Cancelled")
    assert outcome.message == (
        "ETH BUY limit (Lighter · Main) is no longer open. "
        "Kept 2 SL/TP protecting the open position or other limits."
    )


def test_unreadable_account_after_the_entry_cancel_leaves_sl_tp_in_place():
    raw = otoco()
    account = lighter_account(raw)
    fake = FakeLighter(LighterApiError("HTTP 503"))
    outcome = canceller(fake, attempts=2).cancel(plan_cancel(entry_of(account), account))
    assert len(fake.cancel_calls) == 1
    assert not outcome.ok
    assert "could not be re-read, so SL/TP were left in place" in outcome.message


def test_sl_tp_that_appeared_after_confirmation_are_not_cancelled():
    raw = [lighter_raw(10), standalone_sl(21)]
    account = lighter_account(raw)
    plan = plan_cancel(entry_of(account), account)
    fake = FakeLighter([standalone_sl(21), standalone_sl(22, trigger="85")], [standalone_sl(22, trigger="85")])
    outcome = canceller(fake).cancel(plan)
    assert [call["order_indexes"] for call in fake.cancel_calls] == [[10], [21]]
    assert outcome.ok


def test_sl_tp_cancel_that_does_not_take_effect_is_reported():
    raw = [lighter_raw(10), standalone_sl(21)]
    account = lighter_account(raw)
    fake = FakeLighter([standalone_sl(21)], cancel_errors=[None, ["margin check failed"]])
    outcome = canceller(fake, attempts=2).cancel(plan_cancel(entry_of(account), account))
    assert not outcome.ok
    assert outcome.message == (
        "Cancelled ETH BUY limit (Lighter · Main). 1 SL/TP still open: margin check failed"
    )


def test_sub_account_without_its_own_api_key_is_not_touched(monkeypatch):
    monkeypatch.delenv("LIGHTER_ACCOUNT_9_API_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("LIGHTER_ACCOUNT_9_API_KEY_INDEX", raising=False)
    monkeypatch.setenv("LIGHTER_ACCOUNT_9_READ_ONLY_AUTH_TOKEN", "ro-token")
    account = lighter_account(otoco(), index=9, label="Sub 1")
    fake = FakeLighter([])
    outcome = canceller(fake).cancel(plan_cancel(entry_of(account), account))
    assert fake.cancel_calls == []
    assert not outcome.ok
    assert "LIGHTER_ACCOUNT_9_API_PRIVATE_KEY" in outcome.message
    assert "read-only token cannot cancel" in outcome.message


def test_sub_account_signs_with_its_own_api_key(monkeypatch):
    monkeypatch.setenv("LIGHTER_ACCOUNT_9_API_PRIVATE_KEY", "sub-private-key")
    monkeypatch.setenv("LIGHTER_ACCOUNT_9_API_KEY_INDEX", "5")
    account = lighter_account(otoco(), index=9, label="Sub 1")
    fake = FakeLighter([])
    assert canceller(fake).cancel(plan_cancel(entry_of(account), account)).ok
    assert fake.cancel_calls[0]["account_index"] == 9
    assert fake.cancel_calls[0]["api_private_key"] == "sub-private-key"
    assert fake.cancel_calls[0]["api_key_index"] == 5


def bingx_raw():
    return [
        {"orderId": 1, "symbol": "BTC-USDT", "type": "LIMIT", "side": "BUY", "positionSide": "BOTH",
         "price": "100", "origQty": "1", "executedQty": "0", "status": "NEW"},
        {"orderId": 2, "symbol": "BTC-USDT", "type": "STOP_MARKET", "side": "SELL", "positionSide": "BOTH",
         "reduceOnly": True, "stopPrice": "90", "origQty": "1", "status": "NEW"},
    ]


def bingx_account(raw):
    return AccountSnapshot(
        AccountInfo(0, name="Main", exchange="bingx"), (),
        limit_orders=tuple(bingx_limit_orders_from_dicts(raw)),
        stop_orders=tuple(bingx_stop_orders_from_dicts(raw, include_take_profit=True)),
    )


def test_bingx_cancels_entry_then_its_stop_by_symbol_and_order_id():
    raw = bingx_raw()
    account = bingx_account(raw)
    client = Mock()
    client.fetch_active_orders.side_effect = [raw[1:], []]
    client.fetch_positions.return_value = []
    outcome = canceller(FakeLighter([]), bingx=client).cancel(plan_cancel(account.limit_orders[0], account))
    assert [call.kwargs for call in client.cancel_order.call_args_list] == [
        {"symbol": "BTC-USDT", "order_id": "1"}, {"symbol": "BTC-USDT", "order_id": "2"},
    ]
    assert outcome.message == "Cancelled BTC-USDT BUY limit (BingX) and 1 SL/TP."


def test_bingx_key_without_trade_permission_reports_why_and_touches_nothing_else():
    raw = bingx_raw()
    account = bingx_account(raw)
    client = Mock()
    client.cancel_order.side_effect = BingxAuthUnavailable("BingX error 100004: the API key cannot trade.")
    client.fetch_active_orders.return_value = raw
    client.fetch_positions.return_value = []
    outcome = canceller(FakeLighter([]), bingx=client).cancel(plan_cancel(account.limit_orders[0], account))
    assert client.cancel_order.call_count == 1
    assert outcome.message == (
        "BTC-USDT BUY limit (BingX) not cancelled: BingX error 100004: the API key cannot trade. SL/TP left in place."
    )
    assert not outcome.ok


# -- Keyboard prompt and controller ----------------------------------------------------------


def snapshot_with(*accounts):
    return TrackerSnapshot(datetime(2026, 10, 16, tzinfo=UTC), tuple(accounts))


class ImmediateExecutor:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def submit(self, function, *args):
        self.calls.append(args)
        future = Future()
        future.set_result(self.result if self.result is not None else function(*args))
        return future


def controller(result_message="Cancelled ETH BUY limit (Lighter · Main) and 2 SL/TP."):
    pager = DashboardPager()
    inner = Mock()
    inner.cancel.return_value = SimpleNamespace(message=result_message, ok=True)
    executor = ImmediateExecutor()
    return CancelController(inner, pager, executor=executor, clock=lambda: 0.0), pager, inner


def two_order_snapshot():
    main = lighter_account([lighter_raw(5, price="95")])
    sub = lighter_account(otoco(), index=9, label="Sub 1")
    return snapshot_with(main, sub)


def test_row_number_review_and_confirmation_cancel_that_exact_order():
    cancels, pager, inner = controller()
    snapshot = two_order_snapshot()
    pager._consume("c2\n")
    assert not cancels.tick(snapshot)
    prompt = cancels.prompt()
    assert prompt.stage == "confirm"
    assert prompt.plan.entry.order_id == "10"
    assert ids(prompt.plan.exits) == ["11", "12"]
    inner.cancel.assert_not_called()

    pager._consume("y")
    cancels.tick(snapshot)
    inner.cancel.assert_called_once()
    assert inner.cancel.call_args.args[0].entry.order_id == "10"
    assert cancels.tick(snapshot) is True  # Finished: the dashboard should refresh now.
    assert cancels.notice == "Cancelled ETH BUY limit (Lighter · Main) and 2 SL/TP."
    assert cancels.prompt() is None


def test_confirmation_typed_before_the_review_was_shown_does_nothing():
    cancels, pager, inner = controller()
    pager._consume("c2\ny")
    cancels.tick(two_order_snapshot())
    cancels.tick(two_order_snapshot())
    inner.cancel.assert_not_called()


@pytest.mark.parametrize("abort", ["n", "N", "c", "\x7f"])
def test_declining_the_review_cancels_nothing(abort):
    cancels, pager, inner = controller()
    snapshot = two_order_snapshot()
    pager._consume("c1\n")
    cancels.tick(snapshot)
    pager._consume(abort)
    cancels.tick(snapshot)
    pager._consume("y")
    cancels.tick(snapshot)
    inner.cancel.assert_not_called()
    assert cancels.prompt() is None


def test_unknown_row_returns_to_number_entry_with_a_hint():
    cancels, pager, _inner = controller()
    pager._consume("c9\n")
    cancels.tick(two_order_snapshot())
    prompt = cancels.prompt()
    assert prompt.stage == "select"
    assert prompt.hint == "No limit order #9."
    assert prompt.digits == ""


def test_order_that_disappeared_during_review_is_not_cancelled():
    cancels, pager, inner = controller()
    pager._consume("c2\n")
    cancels.tick(two_order_snapshot())
    pager._consume("y")
    cancels.tick(snapshot_with(lighter_account([lighter_raw(5, price="95")])))
    inner.cancel.assert_not_called()
    assert cancels.notice == "ETH BUY limit is no longer open; nothing was cancelled."


def test_prompt_keys_do_not_leak_into_paging_and_q_still_quits():
    pager = DashboardPager()
    pager._consume("c1np\t2")
    assert pager.page == 0
    assert pager.cancel_digits == "12"
    pager._consume("\x7f\n")
    assert pager.cancel_stage == "confirm" and pager.cancel_row == 1
    pager._consume("x\n")  # Neither a typo nor Enter confirms.
    assert not pager.cancel_confirmed
    pager._consume("q")
    assert pager.quit_requested


def test_zero_or_empty_row_cannot_be_submitted():
    pager = DashboardPager()
    pager._consume("c\n00\n")
    assert pager.cancel_stage == "select"
    assert pager.cancel_row is None


def test_y_outside_the_prompt_and_pasted_keys_never_confirm():
    pager = DashboardPager()
    pager._consume("y")
    pager._consume("\x1b[200~c1\ny\x1b[201~")
    assert pager.cancel_stage is None
    assert not pager.cancel_confirmed


def test_lone_escape_closes_the_prompt(monkeypatch):
    from active_trades import terminal

    now = [0.0]
    monkeypatch.setattr(terminal.time, "monotonic", lambda: now[0])
    pager = DashboardPager()
    pager._consume("c1\n\x1b")
    assert pager.cancel_stage == "confirm"
    now[0] += 1
    pager._expire_escape()
    assert pager.cancel_stage is None


def test_arrow_keys_still_page_while_choosing_a_row():
    pager = DashboardPager()
    pager._consume("c\x1b[C3")
    assert pager.page == 1
    assert pager.cancel_digits == "3"


# -- Rendering --------------------------------------------------------------------------------


def test_review_prompt_shows_the_order_and_every_sl_tp_it_removes():
    cancels, pager, _inner = controller()
    snapshot = two_order_snapshot()
    pager._consume("c2\n")
    cancels.tick(snapshot)
    rendered = render_plain(snapshot, refresh_seconds=15, width=100, cancel_prompt=cancels.prompt())
    assert "CANCEL #2 ETH BUY 1 @ $100" in rendered
    assert "Lighter · Sub 1" in rendered
    assert "Also cancels SL $90, TP $120" in rendered
    assert "y confirm · n back" in rendered


def test_review_prompt_keeps_prices_visible_but_masks_amounts():
    cancels, pager, _inner = controller()
    snapshot = two_order_snapshot()
    pager._consume("c2\n")
    cancels.tick(snapshot)
    rendered = render_plain(
        snapshot, refresh_seconds=15, width=100, hide_amounts=True, cancel_prompt=cancels.prompt()
    )
    assert "CANCEL #2 ETH BUY 1 @ $100" in rendered


def test_rich_header_shows_the_prompt_and_wide_footer_shows_the_shortcut():
    from io import StringIO

    from rich.console import Console

    cancels, pager, _inner = controller()
    snapshot = two_order_snapshot()
    pager._consume("c")
    cancels.tick(snapshot)
    output = StringIO()
    Console(file=output, width=120, height=40, color_system=None).print(
        render_snapshot(snapshot, refresh_seconds=15, live=True, page=0, cancel_prompt=cancels.prompt())
    )
    rendered = output.getvalue()
    assert "CANCEL LIMIT ORDER #_" in rendered
    assert "c cancel" in rendered.splitlines()[-1]


def test_dashboard_rows_and_cancel_rows_share_numbering():
    snapshot = two_order_snapshot()
    rendered = render_plain(snapshot, refresh_seconds=15, width=100)
    for number, (order, _account) in enumerate(numbered_limit_orders(snapshot), start=1):
        assert f"{number:>2} {order.symbol} {order.side.upper()}" in rendered
