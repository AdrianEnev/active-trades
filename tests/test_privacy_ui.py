from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import re

import pytest
from rich.console import Console

from active_trades import ui
from active_trades.models import AccountInfo, AccountSnapshot, ActiveTrade, LimitOrder, Position, TrackerSnapshot


VISIBLE_LIMIT_PRICES = ("$98,765.4321", "$0.00001234", "$0.03032")


def without_limit_prices(rendered: str) -> str:
    for price in VISIBLE_LIMIT_PRICES:
        rendered = rendered.replace(price, "")
    return rendered


def privacy_snapshot() -> TrackerSnapshot:
    opened = datetime(2026, 10, 16, 9, 15, tzinfo=UTC)
    trades = (
        ActiveTrade(
            1, Position(1, 1, "BTC", "long", Decimal("1"), Decimal("100"), unrealized_pnl=Decimal("1234.56")),
            opened, "state", Decimal("95"), Decimal("5"), "at-risk", "Cached metadata unchanged",
        ),
        ActiveTrade(
            2, Position(1, 2, "ETH", "short", Decimal("0.2"), Decimal("2000"), unrealized_pnl=Decimal("-0.000012")),
            opened, "state", Decimal("2100"), Decimal("20"), "at-risk",
        ),
    )
    unknown = ActiveTrade(
        1, Position(0, "SOL-USDT", "SOL-USDT", "long", Decimal("2"), Decimal("150"), exchange="bingx"),
        opened, "exchange", None, None, "auth-needed",
    )
    orders = (
        LimitOrder(
            1, 99, "BTC", "buy", Decimal("98765.4321"), Decimal("0.0012"),
            stop_loss=Decimal("98000"), raw={"unchanged": "$98765.4321"},
        ),
        LimitOrder(
            1, 98, "SMALL", "buy", Decimal("0.00001234"), Decimal("14000"),
            stop_loss=Decimal("0.000011"),
        ),
    )
    return TrackerSnapshot(
        opened,
        (
            AccountSnapshot(AccountInfo(1, display_name="Main"), trades, limit_orders=orders),
            AccountSnapshot(
                AccountInfo(0, exchange="bingx"), (unknown,),
                limit_orders=(LimitOrder(
                    0, "OGN-USDT", "OGN-USDT", "buy", Decimal("0.03032"), Decimal("14000"), exchange="bingx",
                ),),
            ),
        ),
    )


def rich_output(snapshot, *, width, hide_amounts, page=None, height=24):
    output = StringIO()
    Console(file=output, width=width, height=height, color_system=None).print(ui.render_snapshot(
        snapshot, refresh_seconds=15, live=True, page=page, hide_amounts=hide_amounts,
    ))
    return output.getvalue()


@pytest.mark.parametrize("width", [28, 45, 72, 100, 140])
def test_privacy_hides_risk_pnl_totals_and_keeps_limit_prices_and_other_data_visible(width):
    snapshot = privacy_snapshot()
    original = deepcopy(snapshot)
    shown = rich_output(snapshot, width=width, hide_amounts=False)
    for price in VISIBLE_LIMIT_PRICES:
        assert price in shown
    for hidden in (
        ui.render_plain(snapshot, refresh_seconds=15, width=width, hide_amounts=True),
        rich_output(snapshot, width=width, hide_amounts=True),
    ):
        assert "$" not in without_limit_prices(hidden)
        for price in VISIBLE_LIMIT_PRICES:
            assert price in hidden
        header, _, positions = hidden.partition("POSITIONS ·")
        positions = positions.partition("LIMIT ORDERS ·")[0]
        assert "$" not in header
        assert "$" not in positions
        # Three totals, two position risk/PNL pairs, and two known order risks.
        assert hidden.count("****") == 9
        assert "+****" not in hidden
        assert "-****" not in hidden
        for label in ("TOTAL UPNL", "POSITION RISK", "LIMIT RISK", "BTC", "ETH", "SOL-USDT", "BingX", "LONG", "SHORT"):
            assert label in hidden
        assert "16 Oct · 09:15" in hidden
        assert "14,000" in hidden
        assert "0.0012" in hidden
        assert "Auth" in hidden
        assert "n/a" in hidden
        assert max(map(len, hidden.splitlines())) <= width
    assert snapshot == original


@pytest.mark.parametrize("width", [28, 45, 72, 100, 140])
def test_privacy_redacts_currency_literals_before_wrapping_notices_names_and_symbols(width):
    snapshot = privacy_snapshot()
    account = snapshot.accounts[0]
    trade = replace(account.trades[0], position=replace(account.trades[0].position, symbol="COIN-$0.000012"),
                    risk_note="Slippage $1e3")
    order = replace(account.limit_orders[0], symbol="VALUE$-1.5")
    snapshot = replace(snapshot, errors=("Requested +$1,234.56; available -$0.000012",), accounts=(
        replace(
            account,
            account=replace(account.account, display_name="Main $-1.5"),
            trades=(trade, account.trades[1]),
            limit_orders=(order, account.limit_orders[1]),
            errors=("Minimum fee $ 50",),
        ),
        snapshot.accounts[1],
    ))
    original = deepcopy(snapshot)
    for rendered in (
        ui.render_plain(snapshot, refresh_seconds=15, width=width, hide_amounts=True),
        rich_output(snapshot, width=width, hide_amounts=True),
    ):
        other_text = without_limit_prices(rendered)
        assert "$" not in other_text
        assert "1,234.56" not in other_text
        assert "0.000012" not in other_text
        assert "1.5" not in other_text
        assert "1e3" not in other_text
        for price in VISIBLE_LIMIT_PRICES:
            assert price in rendered
        assert "COIN****" in rendered
        assert "VALUE****" in rendered
        assert "Main ****" in rendered
        assert "Slippage ****" in rendered
        assert "Minimum fee ****" in rendered
        assert max(map(len, rendered.splitlines())) <= width
    assert snapshot == original


@pytest.mark.parametrize("width", [28, 45, 72, 100, 140])
@pytest.mark.parametrize("hide_amounts", [False, True])
def test_every_page_keeps_privacy_mode_and_all_shortcut_hints(width, hide_amounts):
    snapshot = privacy_snapshot()
    first = rich_output(snapshot, width=width, hide_amounts=hide_amounts, page=0)
    page_match = re.search(r"(?:Page )?1/(\d+)", first.splitlines()[-1])
    assert page_match is not None
    page_count = int(page_match[1])
    all_pages = []
    for page in range(page_count):
        rendered = rich_output(snapshot, width=width, hide_amounts=hide_amounts, page=page)
        footer = rendered.splitlines()[-1]
        assert f"{page + 1}/{page_count}" in footer
        assert "n/p" in footer
        assert f"m {'show' if hide_amounts else 'hide'}" in footer
        assert "q quit" in footer
        assert len(rendered.splitlines()) == 24
        assert max(map(len, rendered.splitlines())) <= width
        if hide_amounts:
            assert "$" not in without_limit_prices(rendered)
        all_pages.append(rendered)
    joined = "\n".join(all_pages)
    for price in VISIBLE_LIMIT_PRICES:
        assert price in joined


def test_deferred_render_modes_are_independent_and_plain_mode_resets():
    snapshot = privacy_snapshot()
    hidden = ui.render_snapshot(snapshot, refresh_seconds=15, hide_amounts=True)
    shown = ui.render_snapshot(snapshot, refresh_seconds=15)
    output = StringIO()
    console = Console(file=output, width=100, color_system=None)
    iterator = hidden.__rich_console__(console, console.options)
    next(iterator)
    # Even a render suspended at its first yield must restore the caller's mode.
    assert ui._fmt_money(Decimal("12.34")) == "$12.34"
    iterator.close()
    for renderable, expected_hidden in ((hidden, True), (shown, False), (hidden, True), (shown, False)):
        output.seek(0)
        output.truncate()
        console.print(renderable)
        rendered = output.getvalue()
        assert ("$5.00" not in rendered) is expected_hidden
        for price in VISIBLE_LIMIT_PRICES:
            assert price in rendered
        if expected_hidden:
            assert "$" not in without_limit_prices(rendered)
    assert "$" not in without_limit_prices(ui.render_plain(snapshot, refresh_seconds=15, hide_amounts=True))
    assert "$5.00" in ui.render_plain(snapshot, refresh_seconds=15)


@pytest.mark.parametrize("renderer", ["_render_plain", "_render_rich"])
def test_privacy_context_resets_after_render_failure(monkeypatch, renderer):
    snapshot = privacy_snapshot()
    original = getattr(ui, renderer)

    def fail(*args, **kwargs):
        raise RuntimeError("Render failed")

    monkeypatch.setattr(ui, renderer, fail)
    with pytest.raises(RuntimeError, match="Render failed"):
        if renderer == "_render_plain":
            ui.render_plain(snapshot, refresh_seconds=15, hide_amounts=True)
        else:
            rich_output(snapshot, width=45, hide_amounts=True)
    assert ui._fmt_money(Decimal("12.34")) == "$12.34"
    monkeypatch.setattr(ui, renderer, original)
    assert "$5.00" in ui.render_plain(snapshot, refresh_seconds=15)


def test_rich_unavailable_fallback_preserves_privacy(monkeypatch):
    monkeypatch.setattr(ui, "Group", None)
    rendered = ui.render_snapshot(privacy_snapshot(), refresh_seconds=15, hide_amounts=True)
    assert isinstance(rendered, str)
    assert "$" not in without_limit_prices(rendered)
    for price in VISIBLE_LIMIT_PRICES:
        assert price in rendered
    assert "****" in rendered


def test_hidden_amounts_do_not_format_or_reveal_extremely_large_values():
    snapshot = privacy_snapshot()
    account = snapshot.accounts[0]
    trade = replace(
        account.trades[0],
        position=replace(account.trades[0].position, unrealized_pnl=Decimal("1e1000")),
        risk_usd=Decimal("1e1000"),
    )
    snapshot = replace(snapshot, accounts=(replace(account, trades=(trade,), limit_orders=(account.limit_orders[0],)),))
    for rendered in (
        ui.render_plain(snapshot, refresh_seconds=15, hide_amounts=True),
        rich_output(snapshot, width=45, hide_amounts=True),
    ):
        assert "$" not in without_limit_prices(rendered)
        assert "$98,765.4321" in rendered
