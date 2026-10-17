from __future__ import annotations

import re
import shutil
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from textwrap import wrap
from typing import Any

from active_trades.models import AccountSnapshot, ActiveTrade, LimitOrder, TrackerSnapshot
from active_trades.order_cancel import CancelPrompt, exit_kind, exit_price, numbered_limit_orders
from active_trades.risk import limit_order_risk_usd, limit_risk_total, position_risk_total

TradeRow = tuple[ActiveTrade, str]
OrderRow = tuple[LimitOrder, str, Decimal | None]

_HIDE_AMOUNTS: ContextVar[bool] = ContextVar("hide_amounts", default=False)
_MASKED_AMOUNT = "****"
_DOLLAR_AMOUNT = re.compile(r"[+-]?\$\s*[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?")

try:
    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.segment import Segment
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover - plain mode handles this.
    box = None  # type: ignore[assignment]
    Group = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Segment = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]


def render_snapshot(
    snapshot: TrackerSnapshot,
    *,
    refresh_seconds: float,
    pnl_refresh_seconds: float = 0.5,
    live: bool = False,
    page: int | None = None,
    hide_amounts: bool = False,
    email_alerts: bool | None = None,
    cancel_prompt: CancelPrompt | None = None,
) -> Any:
    if not (Group and Panel and Table and Text):
        return render_plain(
            snapshot,
            refresh_seconds=refresh_seconds,
            pnl_refresh_seconds=pnl_refresh_seconds,
            live=live,
            hide_amounts=hide_amounts,
            email_alerts=email_alerts,
            cancel_prompt=cancel_prompt,
        )
    return _SnapshotRenderable(
        snapshot, refresh_seconds, pnl_refresh_seconds, live, page, hide_amounts, email_alerts, cancel_prompt
    )


class _SnapshotRenderable:
    """Choose the layout using the actual console width, including Live renders."""

    def __init__(
        self,
        snapshot: TrackerSnapshot,
        refresh_seconds: float,
        pnl_refresh_seconds: float,
        live: bool,
        page: int | None,
        hide_amounts: bool,
        email_alerts: bool | None = None,
        cancel_prompt: CancelPrompt | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.refresh_seconds = refresh_seconds
        self.pnl_refresh_seconds = pnl_refresh_seconds
        self.live = live
        self.page = page
        self.hide_amounts = hide_amounts
        self.email_alerts = email_alerts
        self.cancel_prompt = cancel_prompt

    def __rich_console__(self, console: Any, options: Any) -> Any:
        token = _HIDE_AMOUNTS.set(self.hide_amounts)
        try:
            dashboard = _render_rich(
                self.snapshot,
                refresh_seconds=self.refresh_seconds,
                pnl_refresh_seconds=self.pnl_refresh_seconds,
                live=self.live,
                width=options.max_width,
                email_alerts=self.email_alerts,
                cancel_prompt=self.cancel_prompt,
            )
            # Finish formatting before yielding, so a paused render cannot leak
            # its privacy mode into a different dashboard in the same context.
            renderables = [dashboard] if self.page is None else list(_render_page(console, options, dashboard, self.page))
        finally:
            _HIDE_AMOUNTS.reset(token)
        yield from renderables


def _render_page(console: Any, options: Any, dashboard: Any, page: int) -> Any:
    """Page body rows without hiding accounts or scrolling each live refresh."""
    unrestricted = options.update(height=None)
    header, *body = dashboard.renderables
    header_lines = console.render_lines(header, unrestricted, pad=False)
    body_lines = console.render_lines(Group(*body), unrestricted, pad=False)
    height = max(2, options.size.height)
    # Even a very short terminal retains one body row and the navigation footer.
    header_lines = header_lines[: max(0, height - 2)]
    body_height = max(1, height - len(header_lines) - 1)
    page_count = max(1, (len(body_lines) + body_height - 1) // body_height)
    page_index = page % page_count
    visible = body_lines[page_index * body_height : (page_index + 1) * body_height]
    for line in (*header_lines, *visible):
        yield from line
        yield Segment.line()
    for _ in range(body_height - len(visible)):
        yield Segment.line()
    action = "show" if _HIDE_AMOUNTS.get() else "hide"
    page_label = f"{page_index + 1}/{page_count}"
    footers = (
        f"Page {page_label} · ←/→ n/p · c cancel · m {action} · q quit",
        f"Page {page_label} · ←/→ n/p · m {action} · q quit",
        f"{page_label} ←/→ n/p m {action} q quit",
        f"{page_label} n/p m {action} q",
    )
    footer = next((footer for footer in footers if len(footer) <= options.max_width), footers[-1])
    yield Text(footer, style="dim", no_wrap=True, overflow="crop")


def _render_rich(
    snapshot: TrackerSnapshot,
    *,
    refresh_seconds: float,
    pnl_refresh_seconds: float,
    live: bool,
    width: int,
    email_alerts: bool | None = None,
    cancel_prompt: CancelPrompt | None = None,
) -> Any:
    renderables: list[Any] = [
        _header_panel(
            snapshot,
            refresh_seconds=refresh_seconds,
            pnl_refresh_seconds=pnl_refresh_seconds,
            live=live,
            width=width,
            email_alerts=email_alerts,
            cancel_prompt=cancel_prompt,
        )
    ]

    for notice in _notices(snapshot):
        renderables.append(Text(f"! {notice}", style="yellow"))

    trades = _trade_rows(snapshot)
    orders = _order_rows(snapshot)
    for title, rows, renderer, kind in (
        (f"POSITIONS · {len(trades)} OPEN", trades, _trades_table, "positions"),
        (f"LIMIT ORDERS · {len(orders)} OPEN", orders, _limit_orders_table, "orders"),
    ):
        contents: list[Any] = []
        if rows:
            contents.append(renderer(rows, width=max(1, width - 4)))
        contents.extend(
            Text(message, style="dim" if available else "yellow")
            for message, available in _source_states(snapshot, kind=kind)
        )
        renderables.append(
            Panel(
                Group(*contents),
                title=title,
                title_align="left",
                box=box.ROUNDED,
                border_style="bright_black",
                padding=(0, 1),
                expand=False,
            )
        )
    return Group(*renderables)


def render_plain(
    snapshot: TrackerSnapshot,
    *,
    refresh_seconds: float,
    pnl_refresh_seconds: float = 0.5,
    live: bool = False,
    width: int | None = None,
    hide_amounts: bool = False,
    email_alerts: bool | None = None,
    cancel_prompt: CancelPrompt | None = None,
) -> str:
    token = _HIDE_AMOUNTS.set(hide_amounts)
    try:
        return _render_plain(
            snapshot,
            refresh_seconds=refresh_seconds,
            pnl_refresh_seconds=pnl_refresh_seconds,
            live=live,
            width=width,
            email_alerts=email_alerts,
            cancel_prompt=cancel_prompt,
        )
    finally:
        _HIDE_AMOUNTS.reset(token)


def _render_plain(
    snapshot: TrackerSnapshot,
    *,
    refresh_seconds: float,
    pnl_refresh_seconds: float,
    live: bool,
    width: int | None,
    email_alerts: bool | None = None,
    cancel_prompt: CancelPrompt | None = None,
) -> str:
    width = width or shutil.get_terminal_size((100, 24)).columns
    width = max(28, width)
    trades = _trade_rows(snapshot)
    orders = _order_rows(snapshot)
    total_trades = len(trades)

    status = "LIVE" if live else "SNAPSHOT"
    lines = ["ACTIVE TRADES"]
    if width < 52:
        lines.append(f"● {status} · {total_trades} POS · {len(snapshot.accounts)} ACCTS")
        lines.append(f"Updated {_fmt_datetime(snapshot.collected_at, seconds=True)}")
    else:
        lines.append(f"● {status}   {total_trades} OPEN POSITIONS   {len(snapshot.accounts)} ACCOUNTS")
        lines.append(f"Updated {_fmt_datetime(snapshot.collected_at, seconds=True)}")
    lines.extend(_cadence_lines(snapshot, refresh_seconds, pnl_refresh_seconds))
    if email_alerts is not None:
        lines.append(_email_alerts_label(email_alerts))
    if cancel_prompt is not None:
        lines.append("")
        lines.extend(text for text, _style in _cancel_prompt_lines(cancel_prompt))
    total_pnl, incomplete = _total_pnl(snapshot)
    lines.extend(("", f"{'TOTAL UPNL':<13}  {_fmt_pnl(total_pnl)}"))
    if incomplete:
        lines.append(_total_pnl_note(total_pnl))
    position_risk, positions_incomplete = position_risk_total(snapshot)
    limit_risk, limits_incomplete = limit_risk_total(snapshot)
    lines.extend((
        f"POSITION RISK  {_risk_total_label(position_risk, positions_incomplete)}",
        f"{'LIMIT RISK':<13}  {_risk_total_label(limit_risk, limits_incomplete)}",
    ))
    if positions_incomplete or limits_incomplete:
        lines.append("Risk incomplete: SL/data")
    notices = _notices(snapshot)
    for notice in notices:
        lines.extend(f"! {part}" for part in wrap(notice, width=max(10, width - 2)))

    lines.extend(("", f"POSITIONS · {len(trades)} OPEN"))
    if trades and (width < 72 or _trade_table_width(trades, compact=False) > width):
        for trade, source in trades:
            lines.append(f"{trade.row_number:>2} {trade.position.symbol} {trade.position.side.upper()}")
            lines.append(f"   {source}")
            values = f"   Risk {_risk_label(trade)} · UPNL {_pnl_label(trade)}"
            if len(values) <= width:
                lines.append(values)
            else:
                lines.extend((f"   Risk {_risk_label(trade)}", f"   UPNL {_pnl_label(trade)}"))
            lines.append(f"   Opened {_fmt_datetime(trade.opened_at)}")
    elif trades:
        lines.append(f"{'#':>2}  {'ASSET':<10} {'OPENED':<15} {'SIDE':<5} {'RISK':>10} {'UPNL':>12}")
        for trade, source in trades:
            lines.append(
                f"{trade.row_number:>2}  {trade.position.symbol:<10} "
                f"{_fmt_datetime(trade.opened_at):<15} "
                f"{trade.position.side.upper():<5} {_risk_label(trade):>10} {_pnl_label(trade):>12}"
            )
            lines.append(f"    {source}")
    lines.extend(message for message, _available in _source_states(snapshot, kind="positions"))
    lines.extend(("", f"LIMIT ORDERS · {len(orders)} OPEN"))
    for number, (order, source, risk) in enumerate(orders, start=1):
        lines.append(f"{number:>2} {order.symbol} {order.side.upper()}")
        lines.append(f"   {source}")
        details = f"   Limit {_fmt_price(order.price)} · Size {_fmt_decimal(order.remaining_base_amount)}"
        if len(details) <= width:
            lines.append(details)
        else:
            lines.extend((f"   Limit {_fmt_price(order.price)}", f"   Size {_fmt_decimal(order.remaining_base_amount)}"))
        lines.append(f"   Risk {_fmt_money(risk)}")
        if order.reduce_only:
            lines.append("   Reduce only")
    lines.extend(message for message, _available in _source_states(snapshot, kind="orders"))
    return "\n".join(part for line in lines for part in (wrap(line, width=width) if line else [""]))


def _header_panel(
    snapshot: TrackerSnapshot,
    *,
    refresh_seconds: float,
    pnl_refresh_seconds: float,
    live: bool,
    width: int,
    email_alerts: bool | None = None,
    cancel_prompt: CancelPrompt | None = None,
) -> Any:
    total_trades = sum(len(account.trades) for account in snapshot.accounts)
    status = Text("● LIVE", style="bold green") if live else Text("● SNAPSHOT", style="bold cyan")
    summary = Text(
        f" · {total_trades} POS · {len(snapshot.accounts)} ACCTS"
        if width < 72
        else f"   {total_trades} OPEN POSITIONS   {len(snapshot.accounts)} ACCOUNTS",
        style="bold",
    )
    first_line = Text.assemble(status, summary)
    second_line = Text(
        f"Updated {_fmt_datetime(snapshot.collected_at, seconds=True)}",
        style="dim",
    )
    cadences = [Text(line, style="dim") for line in _cadence_lines(snapshot, refresh_seconds, pnl_refresh_seconds)]
    if email_alerts is not None:
        cadences.append(Text(_email_alerts_label(email_alerts), style="bold green" if email_alerts else "dim"))
    if cancel_prompt is not None:
        cadences.append(Text(""))
        cadences.extend(Text(text, style=style) for text, style in _cancel_prompt_lines(cancel_prompt))
    return Panel(
        Group(first_line, second_line, *cadences, Text(""), _total_pnl_summary(snapshot), _risk_summary(snapshot)),
        title="ACTIVE TRADES",
        title_align="left",
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 1),
        expand=True,
    )


def _email_alerts_label(enabled: bool) -> str:
    return "Email alerts ON · TP/SL · Ctrl+E off" if enabled else "Email alerts off · Ctrl+E on"


_MAX_LISTED_EXITS = 4


def _cancel_prompt_lines(prompt: CancelPrompt) -> list[tuple[str, str]]:
    """Prompt text and styles. Prices stay visible while amounts are hidden, as in the tables."""
    if prompt.stage == "select":
        hint = f"{prompt.hint} " if prompt.hint else ""
        return [
            (f"{hint}CANCEL LIMIT ORDER #{prompt.digits}_", "bold yellow"),
            ("Type its # · Enter review · Esc back", "dim"),
        ]
    plan = prompt.plan
    assert plan is not None
    entry = plan.entry
    order = (
        f"{_mask_dollar_literals(entry.symbol)} {entry.side.upper()} "
        f"{_fmt_decimal(entry.remaining_base_amount)} @ {_fmt_price(entry.price)}"
    )
    if prompt.stage == "busy":
        return [(f"Cancelling {order}…", "bold yellow")]
    lines = [
        (f"CANCEL #{prompt.number} {order}", "bold yellow"),
        (_mask_dollar_literals(plan.source), "dim"),
    ]
    if plan.exits:
        listed = ", ".join(
            f"{exit_kind(exit_order)} {_fmt_price(exit_price(exit_order))}"
            for exit_order in plan.exits[:_MAX_LISTED_EXITS]
        )
        more = len(plan.exits) - _MAX_LISTED_EXITS
        lines.append((f"Also cancels {listed}{f' +{more} more' if more > 0 else ''}", "yellow"))
    elif not plan.entry.reduce_only:
        lines.append(("No SL/TP to cancel", "dim"))
    if plan.kept:
        reason = "positions unavailable" if plan.positions_unknown else "protect position/other limits"
        lines.append((f"Keeps {plan.kept} SL/TP ({reason})", "dim"))
    if plan.filled:
        lines.append((f"Partly filled {_fmt_decimal(plan.filled)}: check that position's SL", "yellow"))
    lines.append(("y confirm · n back", "bold"))
    return lines


def _total_pnl(snapshot: TrackerSnapshot) -> tuple[Decimal | None, bool]:
    total = Decimal("0")
    valued_positions = 0
    incomplete = not snapshot.accounts_complete or (not snapshot.accounts and bool(snapshot.errors))
    for account in snapshot.accounts:
        incomplete = incomplete or account.positions_state != "available"
        for trade in account.trades:
            pnl = trade.position.unrealized_pnl
            if pnl is None:
                incomplete = True
            else:
                total += pnl
                valued_positions += 1
    return (total if valued_positions or not incomplete else None), incomplete


def _total_pnl_note(total: Decimal | None) -> str:
    return "UPNL unavailable" if total is None else "Partial · missing data"


def _total_pnl_summary(snapshot: TrackerSnapshot) -> Any:
    total, incomplete = _total_pnl(snapshot)
    contents = [Text.assemble(
        (f"{'TOTAL UPNL':<13}  ", "bold"),
        (_fmt_pnl(total), f"bold {_pnl_style(total)}"),
    )]
    if incomplete:
        contents.append(Text(_total_pnl_note(total), style="yellow"))
    return Group(*contents)


def _risk_total_label(total: Decimal | None, incomplete: bool) -> str:
    return f"{'≥' if incomplete and total is not None else ''}{_fmt_money(total)}"


def _risk_total_style(total: Decimal | None, incomplete: bool) -> str:
    return "yellow" if incomplete or total is None else "green" if total == 0 else "white"


def _risk_summary(snapshot: TrackerSnapshot) -> Any:
    position_risk, positions_incomplete = position_risk_total(snapshot)
    limit_risk, limits_incomplete = limit_risk_total(snapshot)
    contents = [
        Text.assemble(
            (f"{label:<13}  ", "bold"),
            (_risk_total_label(total, incomplete), f"bold {_risk_total_style(total, incomplete)}"),
        )
        for label, total, incomplete in (
            ("POSITION RISK", position_risk, positions_incomplete),
            ("LIMIT RISK", limit_risk, limits_incomplete),
        )
    ]
    if positions_incomplete or limits_incomplete:
        contents.append(Text("Risk incomplete: SL/data", style="yellow"))
    return Group(*contents)


def _trades_table(trades: tuple[TradeRow, ...], *, width: int) -> Any:
    compact = width < 68 or _trade_table_width(trades, compact=False) > width
    if _trade_table_width(trades, compact=compact) > width:
        return _stacked_trades(trades)
    table = Table(
        box=box.SIMPLE_HEAD,
        expand=False,
        show_lines=False,
        pad_edge=False,
        padding=(0, 1),
        collapse_padding=True,
    )
    table.add_column("#", justify="right", width=2, no_wrap=True)
    table.add_column("ASSET / SIDE" if compact else "ASSET", min_width=14 if compact else 4)
    if not compact:
        table.add_column("OPENED", min_width=14, no_wrap=True)
        table.add_column("SIDE", min_width=5, no_wrap=True)
    table.add_column("RISK", justify="right", min_width=4, no_wrap=True)
    table.add_column("UPNL", justify="right", min_width=4, no_wrap=True)

    for trade, source in trades:
        side_style = "green" if trade.position.side == "long" else "red"
        side = Text(trade.position.side.upper(), style=side_style)
        symbol = Text(trade.position.symbol, style=_symbol_style(trade))
        if compact:
            asset = Text.assemble(
                symbol, " ", side, "\n", (source, "dim"), "\n", (_fmt_datetime(trade.opened_at), "dim")
            )
            cells = [str(trade.row_number), asset]
        else:
            asset = Text.assemble(symbol, "\n", (source, "dim"))
            cells = [str(trade.row_number), asset, _fmt_datetime(trade.opened_at), side]
        table.add_row(*cells, Text(_risk_label(trade), style=_risk_style(trade)), _pnl_text(trade))
    return table


def _trade_table_width(trades: tuple[TradeRow, ...], *, compact: bool) -> int:
    row_width = max(2, *(len(str(trade.row_number)) for trade, _source in trades))
    risk_width = max(4, *(len(_risk_label(trade)) for trade, _source in trades))
    pnl_width = max(4, *(len(_pnl_label(trade)) for trade, _source in trades))
    if compact:
        asset_width = max(
            14,
            *(max(len(source), len(trade.position.symbol) + len(trade.position.side) + 1) for trade, source in trades),
        )
        return row_width + asset_width + risk_width + pnl_width + 8
    asset_width = max(5, *(max(len(trade.position.symbol), len(source)) for trade, source in trades))
    return row_width + asset_width + 14 + 5 + risk_width + pnl_width + 12


def _stacked_trades(trades: tuple[TradeRow, ...]) -> Any:
    rows: list[Any] = []
    for trade, source in trades:
        side_style = "green" if trade.position.side == "long" else "red"
        rows.extend(
            (
                Text.assemble(
                    f"{trade.row_number} ",
                    (trade.position.symbol, _symbol_style(trade)),
                    " ",
                    (trade.position.side.upper(), side_style),
                ),
                Text(source, style="dim"),
                Text.assemble("Risk ", (_risk_label(trade), _risk_style(trade))),
                Text.assemble("UPNL ", _pnl_text(trade)),
                Text(f"Opened {_fmt_datetime(trade.opened_at)}", style="dim"),
            )
        )
    return Group(*rows)


def _limit_orders_table(orders: tuple[OrderRow, ...], *, width: int) -> Any:
    price_width = max(11, *(len(_fmt_price(order.price)) for order, _source, _risk in orders))
    size_width = max(4, *(len(_fmt_decimal(order.remaining_base_amount)) for order, _source, _risk in orders))
    risk_width = max(4, *(len(_fmt_money(risk)) for _order, _source, risk in orders))
    asset_width = max(
        12,
        *(
            max(len(source), len(order.symbol) + len(order.side) + len(str(number)) + 2)
            for number, (order, source, _risk) in enumerate(orders, start=1)
        ),
    )
    full_width = (
        max(1, *(len(str(number)) for number in range(1, len(orders) + 1)))
        + max(5, *(max(len(order.symbol), len(source)) for order, source, _risk in orders))
        + 4 + price_width + size_width + risk_width + 6 + 14
    )
    compact = width < 68 or full_width > width
    if compact and asset_width + price_width + size_width + risk_width + 8 > width:
        rows: list[Any] = []
        for number, (order, source, risk) in enumerate(orders, start=1):
            rows.append(Text.assemble(f"{number} {order.symbol} ", (order.side.upper(), "green" if order.side == "buy" else "red")))
            rows.append(Text(source, style="dim"))
            rows.append(Text(f"Limit {_fmt_price(order.price)}"))
            rows.append(Text(f"Size {_fmt_decimal(order.remaining_base_amount)}"))
            rows.append(Text.assemble("Risk ", (_fmt_money(risk), _risk_total_style(risk, False))))
            if order.reduce_only:
                rows.append(Text("Reduce only", style="dim"))
        return Group(*rows)
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, padding=(0, 1), collapse_padding=True)
    if compact:
        table.add_column("ASSET / SIDE", min_width=asset_width)
    else:
        table.add_column("#", justify="right", no_wrap=True)
        table.add_column("ASSET")
        table.add_column("SIDE", no_wrap=True)
    table.add_column("LIMIT PRICE", justify="right", no_wrap=True)
    table.add_column("SIZE", justify="right", no_wrap=True)
    table.add_column("RISK", justify="right", no_wrap=True)
    if not compact:
        table.add_column("REDUCE", no_wrap=True)
    for number, (order, source, risk) in enumerate(orders, start=1):
        side = Text(order.side.upper(), style="green" if order.side == "buy" else "red")
        if compact:
            asset = Text.assemble(f"{number} {order.symbol} ", side, "\n", (source, "dim"))
            if order.reduce_only:
                asset.append("\nReduce only", style="dim")
            cells = [asset]
        else:
            cells = [str(number), Text.assemble(order.symbol, "\n", (source, "dim")), side]
        cells.extend((Text(_fmt_price(order.price)), Text(_fmt_decimal(order.remaining_base_amount))))
        cells.append(Text(_fmt_money(risk), style=_risk_total_style(risk, False)))
        if not compact:
            cells.append(Text("Yes" if order.reduce_only else "No", style="dim"))
        table.add_row(*cells)
    return table


def _orders_empty_message(account: AccountSnapshot) -> str:
    if account.orders_state == "auth-needed":
        return "Auth needed to read limit orders."
    if account.orders_state == "unavailable":
        return "Limit orders unavailable."
    if account.orders_state == "disabled":
        return "Order lookup disabled."
    return "No limit orders."


def _source_label(account: AccountSnapshot, *, exchange: str | None = None) -> str:
    exchange = exchange or account.account.exchange
    if exchange == "bingx":
        return "BingX"
    name = "Lighter" if exchange == "lighter" else exchange
    return _mask_dollar_literals(f"{name} · {account.account.label}")


def _trade_rows(snapshot: TrackerSnapshot) -> tuple[TradeRow, ...]:
    rows = (
        (trade, _source_label(account, exchange=trade.position.exchange))
        for account in snapshot.accounts
        for trade in account.trades
    )
    return tuple((replace(
        trade,
        row_number=number,
        position=replace(trade.position, symbol=_mask_dollar_literals(trade.position.symbol)),
    ), source) for number, (trade, source) in enumerate(rows, start=1))


def _order_rows(snapshot: TrackerSnapshot) -> tuple[OrderRow, ...]:
    # Row numbers are what the cancel prompt resolves, so share its ordering.
    return tuple(
        (
            replace(order, symbol=_mask_dollar_literals(order.symbol)),
            _source_label(account, exchange=order.exchange),
            limit_order_risk_usd(order, account),
        )
        for order, account in numbered_limit_orders(snapshot)
    )


def _source_states(snapshot: TrackerSnapshot, *, kind: str) -> list[tuple[str, bool]]:
    """Keep each source's empty or failed lookup visible in the shared panel."""
    if not snapshot.accounts:
        if snapshot.errors:
            message = "Positions unavailable." if kind == "positions" else "Limit orders unavailable."
            return [(message, False)]
        return [("No open positions." if kind == "positions" else "No limit orders.", True)]
    messages: list[tuple[str, bool]] = []
    for account in snapshot.accounts:
        state = account.positions_state if kind == "positions" else account.orders_state
        rows = account.trades if kind == "positions" else account.limit_orders
        if rows and state == "available":
            continue
        if kind == "orders":
            message = _orders_empty_message(account)
        elif state == "auth-needed":
            message = "Auth needed to read positions."
        elif state == "unavailable":
            message = "Positions unavailable."
        elif state == "disabled":
            message = "Position lookup disabled."
        else:
            message = "No open positions."
        messages.append((f"{_source_label(account)}: {message}", state == "available"))
    return messages


def _cadence_lines(snapshot: TrackerSnapshot, refresh_seconds: float, pnl_refresh_seconds: float) -> list[str]:
    exchanges = {account.account.exchange for account in snapshot.accounts}
    if "bingx" not in exchanges:
        return [f"Data {refresh_seconds:g}s · UPNL {pnl_refresh_seconds:g}s"]
    lines = []
    if "lighter" in exchanges:
        lines.append(f"Lighter {refresh_seconds:g}s · UPNL {pnl_refresh_seconds:g}s")
    lines.append(f"BingX REST {refresh_seconds:g}s")
    return lines


def _notices(snapshot: TrackerSnapshot) -> list[str]:
    notices = list(snapshot.errors)
    for account in snapshot.accounts:
        label = _source_label(account)
        notices.extend(f"{label}: {_short_notice(error)}" for error in account.errors)
        if not account.trades:
            continue
        for trade in account.trades:
            if trade.risk_note and trade.risk_state not in {"locked", "auth-needed", "missing"}:
                notices.append(f"{label} {trade.position.symbol}: {trade.risk_note}")
    return [_mask_dollar_literals(notice) for notice in notices]


def _mask_dollar_literals(value: str) -> str:
    return _DOLLAR_AMOUNT.sub(_MASKED_AMOUNT, value) if _HIDE_AMOUNTS.get() else value


def _short_notice(error: str) -> str:
    if "per-account" in error or "authenticated" in error:
        return "auth needed for SL/risk and limit orders"
    if "active stop orders" in error or "active-order" in error:
        return "SL/risk and limit orders unavailable"
    return error


def _risk_label(trade: ActiveTrade) -> str:
    if trade.risk_usd is not None:
        return _fmt_money(trade.risk_usd)
    if trade.risk_state == "auth-needed":
        return "Auth"
    if trade.risk_state == "missing":
        return "No SL"
    return "n/a"


def _symbol_style(trade: ActiveTrade) -> str:
    return "cyan" if trade.profit_target_reached or trade.risk_usd == Decimal("0") else ""


def _risk_style(trade: ActiveTrade) -> str:
    if trade.risk_usd == Decimal("0"):
        return "cyan"
    if trade.risk_usd is not None:
        return "white"
    return "yellow"


def _pnl_label(trade: ActiveTrade) -> str:
    return _fmt_pnl(trade.position.unrealized_pnl)


def _fmt_pnl(pnl: Decimal | None) -> str:
    if pnl is None:
        return "n/a"
    if _HIDE_AMOUNTS.get():
        return _MASKED_AMOUNT
    return f"{'-' if pnl < 0 else '+'}{_fmt_money(abs(pnl))}"


def _pnl_style(pnl: Decimal | None) -> str:
    return "dim" if pnl is None else "red" if pnl < 0 else "green"


def _pnl_text(trade: ActiveTrade) -> Any:
    pnl = trade.position.unrealized_pnl
    return Text(_fmt_pnl(pnl), style=_pnl_style(pnl))


def _fmt_decimal(value: Decimal) -> str:
    rendered = format(value, ",f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _fmt_price(value: Decimal) -> str:
    return f"${_fmt_decimal(value)}"


def _fmt_money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    if _HIDE_AMOUNTS.get():
        return _MASKED_AMOUNT
    quantized = value.quantize(Decimal("0.01"))
    return f"${quantized:,.2f}"


def _fmt_datetime(value: datetime | None, *, seconds: bool = False) -> str:
    if value is None:
        return "n/a"
    fmt = "%d %b · %H:%M:%S" if seconds else "%d %b · %H:%M"
    return value.astimezone(UTC).strftime(fmt)
