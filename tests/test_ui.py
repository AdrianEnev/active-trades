from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import re

import pytest
from rich.console import Console

from active_trades.models import (
    AccountInfo,
    AccountSnapshot,
    ActiveTrade,
    LimitOrder,
    Position,
    TrackerSnapshot,
)
from active_trades.ui import render_plain, render_snapshot


def _content_text(rendered: str) -> str:
    return " ".join(" ".join(line.strip(" │") for line in rendered.splitlines()).split())


def _trade(
    account_index: int,
    row_number: int,
    symbol: str,
    side: str,
    opened_at: datetime,
    risk: str,
    pnl: str | None = None,
) -> ActiveTrade:
    position = Position(
        account_index=account_index,
        market_id=1,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        size=Decimal("1"),
        avg_entry_price=Decimal("100"),
        unrealized_pnl=Decimal(pnl) if pnl is not None else None,
    )
    return ActiveTrade(
        row_number=row_number,
        position=position,
        opened_at=opened_at,
        opened_at_source="trades",
        stop_loss=None,
        risk_usd=Decimal(risk),
        risk_state="at-risk",
    )


def _snapshot() -> TrackerSnapshot:
    return TrackerSnapshot(
        collected_at=datetime(2026, 10, 12, 17, 57, 3, tzinfo=UTC),
        accounts=(
            AccountSnapshot(
                account=AccountInfo(
                    index=101,
                    display_name="Main",
                    collateral=Decimal("231.87"),
                    available_balance=Decimal("193.12"),
                ),
                trades=(
                    _trade(101, 1, "BTC", "short", datetime(2026, 10, 11, 17, 52, tzinfo=UTC), "11.21", "123.45"),
                    _trade(101, 2, "NEAR", "short", datetime(2026, 10, 12, 8, 11, tzinfo=UTC), "3.91", "-2.50"),
                ),
            ),
            AccountSnapshot(
                account=AccountInfo(index=102, display_name="Sub 1"),
                trades=(
                    _trade(102, 1, "BTC", "long", datetime(2026, 10, 12, 17, 28, tzinfo=UTC), "7.90"),
                ),
            ),
        ),
    )


def test_plain_renderer_combines_accounts_and_uses_day_first_dates_without_balances() -> None:
    output = render_plain(_snapshot(), refresh_seconds=15, live=True)

    assert "3 OPEN POSITIONS   2 ACCOUNTS" in output
    assert "POSITIONS · 3 OPEN" in output
    assert "Lighter · Main" in output
    assert "Lighter · Sub 1" in output
    assert "11 Oct · 17:52" in output
    assert "12 Oct · 17:28" in output
    assert "10-11" not in output
    assert "231.87" not in output
    assert "193.12" not in output
    assert "+$123.45" in output
    assert "-$2.50" in output
    assert "Data 15s · UPNL 0.5s" in output
    assert output.count("LIMIT ORDERS ·") == 1
    assert "No limit orders." in output


@pytest.mark.parametrize("width", [32, 38, 41, 45, 48, 100])
def test_rich_renderer_preserves_money_and_dates_at_actual_terminal_width(width: int) -> None:
    output = StringIO()
    console = Console(file=output, width=width, color_system=None, force_terminal=False)

    console.print(render_snapshot(_snapshot(), refresh_seconds=15, live=True))

    lines = output.getvalue().splitlines()
    assert lines
    assert max(map(len, lines)) <= width
    assert "╭─ POSITIONS · 3 OPEN" in output.getvalue()
    assert "Lighter · Main" in output.getvalue()
    assert "Lighter · Sub 1" in output.getvalue()
    assert output.getvalue().count("╭─ LIMIT ORDERS ·") == 1
    for value in ("$11.21", "$3.91", "$7.90", "+$123.45", "-$2.50", "11 Oct · 17:52"):
        assert value in output.getvalue()
    assert "…" not in output.getvalue()
    assert "UPNL" in output.getvalue()


def test_empty_snapshot_has_clear_empty_state() -> None:
    snapshot = TrackerSnapshot(
        collected_at=datetime(2026, 10, 12, 17, 57, 3, tzinfo=UTC),
        accounts=(),
    )

    output = render_plain(snapshot, refresh_seconds=15)

    assert "0 OPEN POSITIONS   0 ACCOUNTS" in output
    assert "No open positions." in output


def test_plain_renderer_uses_compact_header_at_very_narrow_width() -> None:
    output = render_plain(_snapshot(), refresh_seconds=15, live=True, width=32)

    assert "● LIVE · 3 POS · 2 ACCTS" in output
    assert max(map(len, output.splitlines())) <= 32


def test_auth_errors_and_missing_open_time_render_without_crashing() -> None:
    trade = _trade(101, 1, "BTC", "long", datetime(2026, 10, 12, 17, 28, tzinfo=UTC), "7.90")
    trade = ActiveTrade(
        row_number=trade.row_number,
        position=trade.position,
        opened_at=None,
        opened_at_source="unknown",
        stop_loss=None,
        risk_usd=None,
        risk_state="auth-needed",
    )
    snapshot = TrackerSnapshot(
        collected_at=datetime(2026, 10, 12, 17, 57, 3, tzinfo=UTC),
        accounts=(
            AccountSnapshot(
                account=AccountInfo(index=101, display_name="Main"),
                trades=(trade,),
                errors=("Add per-account Lighter credentials to read orders.",),
            ),
        ),
    )

    output = render_plain(snapshot, refresh_seconds=15)

    assert "Lighter · Main: auth needed for SL/risk" in output
    assert "Auth" in output
    assert "n/a" in output


def test_rich_upnl_colors_match_the_sign_and_missing_value_is_dim() -> None:
    console = Console(width=100)
    segments = list(console.render(render_snapshot(_snapshot(), refresh_seconds=15)))

    assert any(segment.text.strip() == "+$123.45" and segment.style.color.name == "green" for segment in segments)
    assert any(segment.text.strip() == "-$2.50" and segment.style.color.name == "red" for segment in segments)
    assert any(segment.text.strip() == "n/a" and segment.style.dim for segment in segments)


def _order_snapshot(*, price: str = "98765.4321", size: str = "0.0012") -> TrackerSnapshot:
    return TrackerSnapshot(
        collected_at=datetime(2026, 10, 12, 17, 57, 3, tzinfo=UTC),
        accounts=(
            AccountSnapshot(
                account=AccountInfo(index=104, display_name="Sub 3"),
                trades=(),
                limit_orders=(
                    LimitOrder(
                        account_index=104,
                        market_id=1,
                        symbol="BTC",
                        side="buy",
                        price=Decimal(price),
                        remaining_base_amount=Decimal(size),
                        reduce_only=True,
                    ),
                ),
            ),
        ),
    )


@pytest.mark.parametrize("width", [32, 45, 100])
def test_limit_only_account_is_visible_with_exact_price_remaining_size_and_reduce_flag(width: int) -> None:
    snapshot = _order_snapshot()
    plain = render_plain(snapshot, refresh_seconds=15, width=width)
    output = StringIO()
    Console(file=output, width=width, color_system=None).print(render_snapshot(snapshot, refresh_seconds=15))

    for rendered in (plain, output.getvalue()):
        assert "LIMIT ORDERS · 1 OPEN" in rendered
        assert "POSITIONS · 0 OPEN" in rendered
        assert "Lighter · Sub 3" in rendered
        assert "No open positions." in _content_text(rendered)
        assert "BTC BUY" in rendered or ("BTC" in rendered and "BUY" in rendered)
        assert "$98,765.4321" in rendered
        assert "0.0012" in rendered
        assert "Reduce only" in rendered or "Yes" in rendered
        assert max(map(len, rendered.splitlines())) <= width
        assert "…" not in rendered


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("available", "No limit orders."),
        ("auth-needed", "Auth needed to read limit orders."),
        ("disabled", "Order lookup disabled."),
        ("unavailable", "Limit orders unavailable."),
    ],
)
def test_empty_order_table_distinguishes_available_auth_disabled_and_failure(state: str, message: str) -> None:
    snapshot = _order_snapshot()
    snapshot = replace(snapshot, accounts=(replace(snapshot.accounts[0], limit_orders=(), orders_state=state),))
    plain = render_plain(snapshot, refresh_seconds=15, width=100)
    output = StringIO()
    Console(file=output, width=45, color_system=None).print(render_snapshot(snapshot, refresh_seconds=15))

    for rendered in (plain, output.getvalue()):
        assert "LIMIT ORDERS · 0 OPEN" in rendered
        assert "Lighter · Sub 3" in rendered
        assert message in _content_text(rendered)


def test_large_financial_values_fall_back_to_stacked_rows_without_ellipsis() -> None:
    snapshot = _snapshot()
    trade = snapshot.accounts[0].trades[0]
    trade = replace(
        trade,
        risk_usd=Decimal("1234567890.12"),
        position=replace(trade.position, unrealized_pnl=Decimal("-9876543210.12")),
    )
    snapshot = replace(snapshot, accounts=(replace(snapshot.accounts[0], trades=(trade,)),))
    output = StringIO()
    Console(file=output, width=45, color_system=None).print(render_snapshot(snapshot, refresh_seconds=15))

    assert "$1,234,567,890.12" in output.getvalue()
    assert "-$9,876,543,210.12" in output.getvalue()
    assert "…" not in output.getvalue()
    assert max(map(len, output.getvalue().splitlines())) <= 45


def test_sub_cent_limit_price_is_not_rounded_to_zero_and_cadences_are_separate() -> None:
    snapshot = _order_snapshot(price="0.00001234", size="0.1200")
    output = StringIO()
    Console(file=output, width=45, color_system=None).print(
        render_snapshot(snapshot, refresh_seconds=15, pnl_refresh_seconds=0.5, live=True)
    )

    assert "$0.00001234" in output.getvalue()
    assert "0.12" in output.getvalue()
    assert "Data 15s · UPNL 0.5s" in output.getvalue()


def _mixed_snapshot() -> TrackerSnapshot:
    snapshot = _snapshot()
    lighter_order = replace(_order_snapshot().accounts[0].limit_orders[0], account_index=101)
    bingx_trade = _trade(0, 1, "ETH", "long", datetime(2026, 10, 12, 9, 15, tzinfo=UTC), "4.25", "6.75")
    bingx_trade = replace(bingx_trade, position=replace(bingx_trade.position, exchange="bingx"))
    bingx_order = replace(
        lighter_order,
        account_index=0,
        symbol="SOL",
        side="sell",
        price=Decimal("150.25"),
        remaining_base_amount=Decimal("2.5"),
        exchange="bingx",
    )
    return replace(
        snapshot,
        accounts=(
            replace(snapshot.accounts[0], limit_orders=(lighter_order,)),
            snapshot.accounts[1],
            AccountSnapshot(
                account=AccountInfo(index=0, name="Main", exchange="bingx"),
                trades=(bingx_trade,),
                limit_orders=(bingx_order,),
            ),
        ),
    )


def _with_pnl_values(snapshot: TrackerSnapshot, *values: str | None) -> TrackerSnapshot:
    pnls = iter(values)
    accounts = []
    for account in snapshot.accounts:
        trades = []
        for trade in account.trades:
            value = next(pnls)
            trades.append(replace(trade, position=replace(
                trade.position, unrealized_pnl=Decimal(value) if value is not None else None,
            )))
        accounts.append(replace(account, trades=tuple(trades)))
    return replace(snapshot, accounts=tuple(accounts))


@pytest.mark.parametrize("width", [28, 45, 100])
@pytest.mark.parametrize(
    ("values", "expected", "color"),
    [
        (("123.45", "-2.50", "12.34", "6.75"), "+$140.04", "green"),
        (("123.45", "-2.50", "-147.70", "6.75"), "-$20.00", "red"),
        (("123.45", "-2.50", "-127.70", "6.75"), "+$0.00", "green"),
        (("0.004", "0.004", "0.004", "0.004"), "+$0.02", "green"),
    ],
)
def test_total_upnl_sums_all_accounts_and_exchanges_before_rounding(
    width: int, values: tuple[str, ...], expected: str, color: str,
) -> None:
    snapshot = _with_pnl_values(_mixed_snapshot(), *values)
    plain = render_plain(snapshot, refresh_seconds=15, width=width)
    output = StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(render_snapshot(snapshot, refresh_seconds=15))

    for rendered in (plain, output.getvalue()):
        top = rendered.partition("POSITIONS ·")[0]
        assert top.count("TOTAL UPNL") == 1
        assert expected in top
        assert "Partial" not in top
        assert max(map(len, rendered.splitlines())) <= width
    # General data, including the total, shares a single top panel.
    top = output.getvalue().partition("╭─ POSITIONS")[0]
    assert top.count("╭") == 1
    assert "TOTAL UPNL" in top.partition("╰")[0]
    segments = list(console.render(render_snapshot(snapshot, refresh_seconds=15)))
    assert any(
        segment.text.strip() == expected and segment.style.bold and segment.style.color.name == color
        for segment in segments
    )


@pytest.mark.parametrize(
    ("case", "expected", "note"),
    [
        ("missing-pnl", "+$127.70", "Partial · missing data"),
        ("unavailable-account", "+$120.95", "Partial · missing data"),
        ("incomplete-discovery", "+$140.04", "Partial · missing data"),
        ("unknown-pnl", "n/a", "UPNL unavailable"),
        ("discovery-failed", "n/a", "UPNL unavailable"),
        ("no-positions", "+$0.00", None),
        ("orders-unavailable", "+$140.04", None),
        ("stream-disconnected", "+$140.04", None),
    ],
)
def test_total_upnl_distinguishes_partial_unknown_and_empty_data(
    case: str, expected: str, note: str | None,
) -> None:
    snapshot = _with_pnl_values(_mixed_snapshot(), "123.45", "-2.50", "12.34", "6.75")
    if case == "missing-pnl":
        snapshot = _mixed_snapshot()
    elif case == "unavailable-account":
        snapshot = replace(snapshot, accounts=(
            snapshot.accounts[0],
            replace(snapshot.accounts[1], trades=(), positions_state="unavailable"),
        ))
    elif case == "incomplete-discovery":
        snapshot = replace(snapshot, accounts_complete=False)
    elif case == "unknown-pnl":
        snapshot = _with_pnl_values(snapshot, None, None, None, None)
    elif case == "discovery-failed":
        snapshot = replace(snapshot, accounts=(), errors=("Lighter account discovery failed.",))
    elif case == "no-positions":
        snapshot = replace(snapshot, accounts=tuple(replace(account, trades=()) for account in snapshot.accounts))
    elif case == "orders-unavailable":
        snapshot = replace(snapshot, accounts=tuple(
            replace(account, orders_state="unavailable", errors=("Could not fetch active orders.",))
            for account in snapshot.accounts
        ))
    elif case == "stream-disconnected":
        snapshot = replace(snapshot, errors=("UPNL stream disconnected; retrying.",))
    plain = render_plain(snapshot, refresh_seconds=15, width=28)
    output = StringIO()
    Console(file=output, width=28, color_system=None).print(render_snapshot(snapshot, refresh_seconds=15))

    for rendered in (plain, output.getvalue()):
        top = rendered.partition("TOTAL UPNL")[2].partition("POSITIONS ·")[0]
        assert expected in top
        if note:
            assert note in top
        else:
            assert "Partial" not in top
            assert "UPNL unavailable" not in top


def test_total_upnl_tracks_updated_and_closed_positions_on_rerender() -> None:
    snapshot = _with_pnl_values(_mixed_snapshot(), "123.45", "-2.50", "12.34", "6.75")
    updated = _with_pnl_values(snapshot, "120.00", "-1.00", "10.00", "6.75")
    closed = replace(updated, accounts=tuple(replace(account, trades=()) for account in updated.accounts))

    for current, expected in ((snapshot, "+$140.04"), (updated, "+$135.75"), (closed, "+$0.00")):
        output = StringIO()
        Console(file=output, width=45, color_system=None).print(render_snapshot(current, refresh_seconds=15))
        assert expected in output.getvalue().partition("POSITIONS ·")[0]


def _risk_snapshot() -> TrackerSnapshot:
    snapshot = _mixed_snapshot()
    accounts = []
    for account in snapshot.accounts:
        trades = tuple(replace(
            trade,
            stop_loss=(
                trade.position.avg_entry_price - trade.risk_usd
                if trade.position.side == "long"
                else trade.position.avg_entry_price + trade.risk_usd
            ),
        ) for trade in account.trades)
        limits = tuple(replace(
            order,
            market_id=99 if order.exchange == "lighter" else "SOL-USDT",
            price=Decimal("100") if order.side == "buy" else Decimal("150"),
            remaining_base_amount=Decimal("2") if order.side == "buy" else Decimal("2.5"),
            reduce_only=False,
            stop_loss=Decimal("90") if order.side == "buy" else Decimal("160"),
            stop_loss_quantity=Decimal("2") if order.side == "buy" else Decimal("2.5"),
        ) for order in account.limit_orders)
        accounts.append(replace(account, trades=trades, limit_orders=limits))
    return replace(snapshot, accounts=tuple(accounts))


@pytest.mark.parametrize("width", [28, 45, 100])
def test_general_header_shows_position_and_additional_limit_risk_across_sources(width: int) -> None:
    snapshot = _risk_snapshot()
    plain = render_plain(snapshot, refresh_seconds=15, width=width)
    output = StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(render_snapshot(snapshot, refresh_seconds=15))

    for rendered in (plain, output.getvalue()):
        header = _content_text(rendered.partition("POSITIONS ·")[0])
        assert "POSITION RISK $27.27" in header
        assert "LIMIT RISK $45.00" in header
        assert "All limit fills → SL" in header
        assert "Risk incomplete" not in header
        assert max(map(len, rendered.splitlines())) <= width
        assert "…" not in rendered
    segments = list(console.render(render_snapshot(snapshot, refresh_seconds=15)))
    for amount in ("$27.27", "$45.00"):
        assert any(
            segment.text.strip() == amount and segment.style.bold and segment.style.color.name == "white"
            for segment in segments
        )
    assert any(segment.text.strip() == "All limit fills → SL" and segment.style.dim for segment in segments)


@pytest.mark.parametrize(
    ("case", "position_label", "limit_label"),
    [
        ("missing-position-sl", "≥$16.06", "$45.00"),
        ("missing-limit-sl", "$27.27", "≥$25.00"),
        ("all-risk-unknown", "n/a", "n/a"),
        ("orders-disabled", "$0.00", "n/a"),
        ("source-unavailable", "≥$23.02", "≥$20.00"),
        ("discovery-incomplete", "≥$27.27", "≥$45.00"),
    ],
)
def test_risk_header_labels_incomplete_or_unavailable_estimates(
    case: str, position_label: str, limit_label: str,
) -> None:
    snapshot = _risk_snapshot()
    if case == "missing-position-sl":
        account = snapshot.accounts[0]
        missing = replace(account.trades[0], risk_usd=None, stop_loss=None, risk_state="missing")
        snapshot = replace(snapshot, accounts=(replace(account, trades=(missing, *account.trades[1:])), *snapshot.accounts[1:]))
    elif case == "missing-limit-sl":
        account = snapshot.accounts[0]
        missing = replace(account.limit_orders[0], stop_loss=None, stop_loss_quantity=None)
        snapshot = replace(snapshot, accounts=(replace(account, limit_orders=(missing,)), *snapshot.accounts[1:]))
    elif case == "all-risk-unknown":
        snapshot = replace(snapshot, accounts=tuple(replace(
            account,
            trades=tuple(replace(trade, risk_usd=None, stop_loss=None, risk_state="missing") for trade in account.trades),
            limit_orders=tuple(replace(order, stop_loss=None, stop_loss_quantity=None) for order in account.limit_orders),
        ) for account in snapshot.accounts))
    elif case == "orders-disabled":
        snapshot = replace(_order_snapshot(), accounts=(replace(
            _order_snapshot().accounts[0], limit_orders=(), orders_state="disabled",
        ),))
    elif case == "source-unavailable":
        snapshot = replace(snapshot, accounts=(*snapshot.accounts[:-1], replace(
            snapshot.accounts[-1], trades=(), limit_orders=(), positions_state="unavailable", orders_state="unavailable",
        )))
    elif case == "discovery-incomplete":
        snapshot = replace(snapshot, accounts_complete=False)
    plain = render_plain(snapshot, refresh_seconds=15, width=28)
    output = StringIO()
    console = Console(file=output, width=28, color_system=None)
    console.print(render_snapshot(snapshot, refresh_seconds=15))

    for rendered in (plain, output.getvalue()):
        header = _content_text(rendered.partition("POSITIONS ·")[0])
        assert f"POSITION RISK {position_label}" in header
        assert f"LIMIT RISK {limit_label}" in header
        assert "Risk incomplete: SL/data" in header
        assert max(map(len, rendered.splitlines())) <= 28
    segments = list(console.render(render_snapshot(snapshot, refresh_seconds=15)))
    for label in (position_label, limit_label):
        if label.startswith("≥") or label == "n/a":
            assert any(
                segment.text.strip() == label and segment.style.bold and segment.style.color.name == "yellow"
                for segment in segments
            )


def test_reducing_limits_and_empty_positions_have_confirmed_zero_risk() -> None:
    snapshot = _order_snapshot()
    output = StringIO()
    console = Console(file=output, width=45, color_system=None)
    console.print(render_snapshot(snapshot, refresh_seconds=15))

    for rendered in (render_plain(snapshot, refresh_seconds=15, width=45), output.getvalue()):
        header = _content_text(rendered.partition("POSITIONS ·")[0])
        assert "POSITION RISK $0.00" in header
        assert "LIMIT RISK $0.00" in header
        assert "Risk incomplete" not in header
    segments = list(console.render(render_snapshot(snapshot, refresh_seconds=15)))
    assert sum(
        segment.text.strip() == "$0.00" and segment.style.bold and segment.style.color.name == "green"
        for segment in segments
    ) == 2


@pytest.mark.parametrize("width", [28, 32, 45, 100])
def test_both_exchanges_share_exactly_two_tables_with_source_on_every_row(width: int) -> None:
    snapshot = _mixed_snapshot()
    plain = render_plain(snapshot, refresh_seconds=15, live=True, width=width)
    output = StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(render_snapshot(snapshot, refresh_seconds=15, live=True))

    for rendered in (plain, output.getvalue()):
        assert rendered.count("POSITIONS · 4 OPEN") == 1
        assert rendered.count("LIMIT ORDERS · 2 OPEN") == 1
        positions, _, orders = rendered.partition("LIMIT ORDERS ·")
        assert positions.count("Lighter · Main") == 2
        assert positions.count("Lighter · Sub 1") == 1
        assert positions.count("BingX") == 2  # Cadence plus position source.
        assert orders.count("Lighter · Main") == 1
        assert orders.count("BingX") == 1
        assert "Lighter 15s · UPNL 0.5s" in _content_text(rendered)
        assert "BingX REST 15s" in rendered
        for value in ("$4.25", "+$6.75", "$150.25", "2.5", "12 Oct · 09:15"):
            assert value in rendered
        assert max(map(len, rendered.splitlines())) <= width
        assert "…" not in rendered


def test_exchange_source_subtitles_are_dim_and_combined_rows_are_numbered_once() -> None:
    snapshot = _mixed_snapshot()
    console = Console(width=100)
    segments = list(console.render(render_snapshot(snapshot, refresh_seconds=15)))

    for source in ("Lighter · Main", "Lighter · Sub 1", "BingX"):
        assert any(segment.text.strip() == source and segment.style.dim for segment in segments)
    plain = render_plain(snapshot, refresh_seconds=15, width=45)
    positions = plain.partition("LIMIT ORDERS ·")[0]
    assert re.findall(r"^\s*(\d+) (?:BTC|NEAR|ETH) (?:LONG|SHORT)$", positions, re.MULTILINE) == ["1", "2", "3", "4"]


@pytest.mark.parametrize(
    ("positions_state", "orders_state", "position_message", "order_message"),
    [
        ("available", "available", "No open positions.", "No limit orders."),
        ("auth-needed", "auth-needed", "Auth needed to read positions.", "Auth needed to read limit orders."),
        ("disabled", "disabled", "Position lookup disabled.", "Order lookup disabled."),
        ("unavailable", "unavailable", "Positions unavailable.", "Limit orders unavailable."),
    ],
)
def test_empty_or_failed_exchange_keeps_its_own_status_inside_shared_tables(
    positions_state: str, orders_state: str, position_message: str, order_message: str
) -> None:
    snapshot = _mixed_snapshot()
    snapshot = replace(
        snapshot,
        accounts=(
            snapshot.accounts[0],
            replace(
                snapshot.accounts[-1],
                trades=(),
                limit_orders=(),
                positions_state=positions_state,
                orders_state=orders_state,
            ),
        ),
    )
    plain = render_plain(snapshot, refresh_seconds=15, width=45)
    output = StringIO()
    Console(file=output, width=32, color_system=None).print(render_snapshot(snapshot, refresh_seconds=15))

    for rendered in (plain, output.getvalue()):
        text = _content_text(rendered)
        assert f"BingX: {position_message}" in text
        assert f"BingX: {order_message}" in text
        assert "POSITIONS · 2 OPEN" in rendered
        assert "LIMIT ORDERS · 1 OPEN" in rendered
        if positions_state != "available":
            assert "No open positions." not in text
            assert "No limit orders." not in text


def test_global_discovery_failure_is_not_presented_as_an_empty_account() -> None:
    snapshot = TrackerSnapshot(
        collected_at=datetime(2026, 10, 12, 17, 57, 3, tzinfo=UTC),
        accounts=(),
        errors=("Lighter account discovery failed.",),
    )
    output = render_plain(snapshot, refresh_seconds=15)

    assert "Positions unavailable." in output
    assert "Limit orders unavailable." in output
    assert "No open positions." not in output
    assert "No limit orders." not in output


def _paged_snapshot() -> TrackerSnapshot:
    snapshot = _snapshot()
    order = _order_snapshot().accounts[0].limit_orders[0]
    return replace(
        snapshot,
        accounts=(
            replace(snapshot.accounts[0], limit_orders=(replace(order, account_index=101),)),
            replace(snapshot.accounts[1], trades=()),
        ),
    )


def _render_dashboard_page(snapshot: TrackerSnapshot, page: int | None, *, height: int = 24) -> str:
    output = StringIO()
    Console(file=output, width=45, height=height, color_system=None).print(
        render_snapshot(snapshot, refresh_seconds=15, live=True, page=page)
    )
    return output.getvalue()


def test_live_pager_keeps_header_and_navigation_fixed_at_45_by_24() -> None:
    snapshot = _paged_snapshot()
    first = _render_dashboard_page(snapshot, 0)
    page_match = re.search(r"Page 1/(\d+)", first.splitlines()[-1])
    assert page_match is not None
    page_count = int(page_match[1])
    pages = [_render_dashboard_page(snapshot, page) for page in range(page_count)]

    assert len(_render_dashboard_page(snapshot, None).splitlines()) > 24
    assert page_count > 1
    for page, rendered in enumerate(pages, start=1):
        assert len(rendered.splitlines()) == 24
        assert max(map(len, rendered.splitlines())) <= 45
        assert "ACTIVE TRADES" in rendered.splitlines()[0]
        assert "Data 15s · UPNL 0.5s" in rendered
        assert "TOTAL UPNL" in rendered
        assert "POSITION RISK" in rendered
        assert "LIMIT RISK" in rendered
        assert "All limit fills → SL" in rendered
        assert "+$120.95" in rendered
        assert f"Page {page}/{page_count}" in rendered
        assert "←/→ n/p · q quit" in rendered.splitlines()[-1]
    assert "$11.21" in first
    assert "+$123.45" in first
    assert "Lighter · Sub 1" in "\n".join(pages[1:])


def test_live_pager_exposes_every_body_row_and_wraps_forward_and_backward() -> None:
    snapshot = _paged_snapshot()
    complete = _render_dashboard_page(snapshot, None).splitlines()
    first = _render_dashboard_page(snapshot, 0).splitlines()
    header_height = next(number for number, line in enumerate(complete) if line.startswith("╭─ POSITIONS"))
    page_match = re.search(r"Page 1/(\d+)", first[-1])
    assert page_match is not None
    page_count = int(page_match[1])

    # The general section stays fixed; body rows appear once across all pages.
    reconstructed = [
        line
        for page in range(page_count)
        for line in _render_dashboard_page(snapshot, page).splitlines()[header_height:-1]
    ]
    assert reconstructed[: len(complete) - header_height] == complete[header_height:]
    assert _render_dashboard_page(snapshot, page_count) == _render_dashboard_page(snapshot, 0)
    assert _render_dashboard_page(snapshot, -1) == _render_dashboard_page(snapshot, page_count - 1)


def test_live_pager_respects_shorter_terminal_height() -> None:
    rendered = _render_dashboard_page(_paged_snapshot(), 0, height=12)

    assert len(rendered.splitlines()) == 12
    assert "Page 1/" in rendered.splitlines()[-1]


def test_mixed_exchange_pager_preserves_every_row_and_both_refresh_cadences() -> None:
    snapshot = _mixed_snapshot()
    complete = _render_dashboard_page(snapshot, None).splitlines()
    first = _render_dashboard_page(snapshot, 0).splitlines()
    header_height = next(number for number, line in enumerate(complete) if line.startswith("╭─ POSITIONS"))
    page_match = re.search(r"Page 1/(\d+)", first[-1])
    assert page_match is not None
    page_count = int(page_match[1])
    assert page_count > 1
    body: list[str] = []
    for page in range(page_count):
        rendered = _render_dashboard_page(snapshot, page).splitlines()
        assert len(rendered) == 24
        assert max(map(len, rendered)) <= 45
        assert rendered[:header_height] == complete[:header_height]
        assert "Lighter 15s · UPNL 0.5s" in "\n".join(rendered[:header_height])
        assert "BingX REST 15s" in "\n".join(rendered[:header_height])
        for metric in ("TOTAL UPNL", "POSITION RISK", "LIMIT RISK", "All limit fills → SL"):
            assert metric in "\n".join(rendered[:header_height])
        body.extend(rendered[header_height:-1])
    assert body[: len(complete) - header_height] == complete[header_height:]
    assert _render_dashboard_page(snapshot, page_count) == _render_dashboard_page(snapshot, 0)
    assert _render_dashboard_page(snapshot, -1) == _render_dashboard_page(snapshot, page_count - 1)
