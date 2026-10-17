from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from active_trades.close_alerts import (
    CloseAlertMonitor,
    ClosedTrade,
    alert_message,
    bingx_closed_trades,
    lighter_close_orders,
    lighter_closed_trades,
)
from active_trades.email_alerts import EmailConfigError, EmailSender, EmailSettings
from active_trades.models import AccountInfo
from active_trades.terminal import DashboardPager
from active_trades.tracker import ActiveTradesTracker
from active_trades.ui import render_plain
from active_trades.models import TrackerSnapshot


SINCE = datetime(2026, 10, 14, tzinfo=UTC)
ACCOUNT = AccountInfo(index=748853, display_name="Main")


def _lighter_order(**overrides):
    order = {
        "market_index": 1, "market_symbol": "BTC", "type": "stop-loss", "status": "filled",
        "order_index": 562953566352406, "trigger_price": "80027.0", "is_ask": True,
        "filled_base_amount": "0.01300", "filled_quote_amount": "1040.334100",
        "reduce_only": True, "updated_at": int((SINCE + timedelta(minutes=5)).timestamp()),
    }
    return {**order, **overrides}


def _lighter_fill(**overrides):
    fill = {
        "market_id": 1, "price": "80025.7", "size": "0.01300",
        "ask_id": 562953566352406, "ask_account_id": 748853, "ask_account_pnl": "-7.619300",
        "bid_id": 844421284959343, "bid_account_id": 281474976499586,
    }
    return {**fill, **overrides}


def _bingx_order(**overrides):
    order = {
        "symbol": "ATOM-USDT", "type": "STOP_MARKET", "side": "BUY", "positionSide": "SHORT",
        "status": "FILLED", "avgPrice": "2.055", "executedQty": "410.00", "profit": "-16.4000",
        "commission": "-0.421275", "stopPrice": "2.056", "orderId": 2108605560895270999,
        "time": int((SINCE + timedelta(minutes=1)).timestamp() * 1000),
        "updateTime": int((SINCE + timedelta(minutes=1)).timestamp() * 1000),
    }
    return {**order, **overrides}


def _closed(alert_id="bingx:1", realized_pnl=Decimal("12.5"), **overrides):
    values = dict(
        alert_id=alert_id, exchange="bingx", account_label="BingX", symbol="TRX-USDT", side="long",
        kind="TP", size=Decimal("3000"), fill_price=Decimal("0.33181"), trigger_price=Decimal("0.33181"),
        realized_pnl=realized_pnl, fees=Decimal("-0.497715"), closed_at=SINCE + timedelta(minutes=2),
    )
    return ClosedTrade(**{**values, **overrides})


class ImmediateExecutor:
    def submit(self, function, *args):
        future = Future()
        future.set_result(function(*args))
        return future

    def shutdown(self, **_kwargs):
        pass


def _monitor(fetch, sender=None, *, clock=None, sender_factory=None):
    clock = clock or [0.0]
    sender = sender or Mock()
    monitor = CloseAlertMonitor(
        fetch, interval_seconds=15, sender_factory=sender_factory or (lambda: sender),
        now=lambda: SINCE, monotonic=lambda: clock[0], executor=ImmediateExecutor(),
    )
    return monitor, sender, clock


# Lighter parsing


def test_lighter_stop_loss_fill_reports_realized_pnl_from_its_fills():
    orders = lighter_close_orders([_lighter_order()], since=SINCE)
    [close] = lighter_closed_trades(orders, [_lighter_fill()], account=ACCOUNT)
    assert close.kind == "SL"
    assert close.side == "long"
    assert close.symbol == "BTC"
    assert close.realized_pnl == Decimal("-7.619300")
    assert close.size == Decimal("0.01300")
    assert close.fill_price == Decimal("1040.334100") / Decimal("0.01300")
    assert close.trigger_price == Decimal("80027.0")
    assert close.alert_id == "lighter:748853:562953566352406"
    assert close.account_label == "Lighter · Main (#748853)"


def test_lighter_take_profit_sums_partial_fills_for_short():
    order = _lighter_order(type="take-profit", is_ask=False, order_index=7)
    fills = [
        _lighter_fill(bid_id=7, bid_account_id=748853, bid_account_pnl="3.5", ask_id=1, ask_account_id=2),
        _lighter_fill(bid_id=7, bid_account_id=748853, bid_account_pnl="1.25", ask_id=3, ask_account_id=4),
        _lighter_fill(bid_id=8, bid_account_id=748853, bid_account_pnl="99", ask_id=5, ask_account_id=6),
    ]
    [close] = lighter_closed_trades(lighter_close_orders([order], since=SINCE), fills, account=ACCOUNT)
    assert (close.kind, close.side, close.realized_pnl) == ("TP", "short", Decimal("4.75"))


def test_lighter_ignores_unfilled_manual_and_earlier_orders():
    orders = [
        _lighter_order(status="canceled-reduce-only", filled_base_amount="0"),
        _lighter_order(type="market"),
        _lighter_order(type="limit"),
        _lighter_order(updated_at=int((SINCE - timedelta(seconds=1)).timestamp())),
    ]
    assert lighter_close_orders(orders, since=SINCE) == []


def test_lighter_missing_fill_leaves_pnl_unknown():
    [close] = lighter_closed_trades(lighter_close_orders([_lighter_order()], since=SINCE), [], account=ACCOUNT)
    assert close.realized_pnl is None


# BingX parsing


def test_bingx_stop_market_fill_uses_reported_profit_and_commission():
    [close] = bingx_closed_trades([_bingx_order()], since=SINCE)
    assert (close.kind, close.side, close.symbol) == ("SL", "short", "ATOM-USDT")
    assert close.realized_pnl == Decimal("-16.4000")
    assert close.fees == Decimal("-0.421275")
    assert close.fill_price == Decimal("2.055")
    assert close.alert_id == "bingx:2108605560895270999"


def test_bingx_take_profit_in_one_way_mode_infers_long_from_sell():
    [close] = bingx_closed_trades(
        [_bingx_order(type="TAKE_PROFIT_MARKET", side="SELL", positionSide="BOTH", profit="7.7999")],
        since=SINCE,
    )
    assert (close.kind, close.side, close.realized_pnl) == ("TP", "long", Decimal("7.7999"))


def test_bingx_ignores_entries_manual_closes_unfilled_and_old_orders():
    orders = [
        _bingx_order(type="MARKET"),
        _bingx_order(type="LIMIT"),
        _bingx_order(type="TAKE_PROFIT_MARKET", status="CANCELLED", executedQty="0"),
        _bingx_order(status="PARTIALLY_FILLED"),
        _bingx_order(
            time=int((SINCE - timedelta(minutes=1)).timestamp() * 1000),
            updateTime=int((SINCE - timedelta(minutes=1)).timestamp() * 1000),
        ),
    ]
    assert bingx_closed_trades(orders, since=SINCE) == []


# Message


def test_alert_message_is_short_and_leads_with_realized_pnl():
    subject, body = alert_message(_closed(realized_pnl=Decimal("-1234.5"), kind="SL", side="short"))
    assert subject == "SL hit: TRX-USDT SHORT · PNL -$1,234.50"
    assert body.startswith("Stop loss hit\n\nRealized PNL:  -$1,234.50\nFees:          -$0.50\n")
    assert "Fill price:    0.33181" in body
    assert "Closed at:     2026-10-14 00:02:00 UTC" in body


def test_alert_message_marks_unknown_pnl():
    subject, body = alert_message(_closed(realized_pnl=None, fees=None))
    assert subject.endswith("PNL n/a")
    assert "Fees" not in body
    assert "did not report realized PNL" in body


# Monitor


def test_monitor_stays_off_and_explains_when_email_is_not_configured():
    def missing():
        raise EmailConfigError("SES_SMTP_HOST must be set for email alerts.")

    monitor, _sender, _clock = _monitor(Mock(), sender_factory=missing)
    assert monitor.set_enabled(True) is False
    assert monitor.enabled is False
    assert "SES_SMTP_HOST" in monitor.notice


def test_monitor_emails_each_close_once_and_only_after_enabling():
    fetch = Mock(return_value=([_closed("a"), _closed("b")], []))
    monitor, sender, clock = _monitor(fetch)
    monitor.tick([ACCOUNT])
    fetch.assert_not_called()  # Disabled: no exchange reads.

    assert monitor.toggle() is True
    monitor.tick([ACCOUNT])
    fetch.assert_not_called()  # First check waits one interval.
    clock[0] = 15
    monitor.tick([ACCOUNT, ACCOUNT])
    accounts, since = fetch.call_args.args
    assert accounts == (ACCOUNT,)
    assert since == SINCE - timedelta(seconds=10)
    assert sender.send.call_count == 2
    assert monitor.notice.startswith("Email sent: TRX-USDT TP hit")

    clock[0] = 30
    monitor.tick([ACCOUNT])
    assert sender.send.call_count == 2  # Already sent.


def test_monitor_does_not_check_more_often_than_interval():
    fetch = Mock(return_value=([], []))
    monitor, _sender, clock = _monitor(fetch)
    monitor.set_enabled(True)
    clock[0] = 15
    monitor.tick([ACCOUNT])
    clock[0] = 20
    monitor.tick([ACCOUNT])
    assert fetch.call_count == 1


def test_monitor_disabling_stops_sending():
    fetch = Mock(return_value=([_closed()], []))
    monitor, sender, clock = _monitor(fetch)
    monitor.set_enabled(True)
    monitor.set_enabled(False)
    clock[0] = 100
    monitor.tick([ACCOUNT])
    fetch.assert_not_called()
    sender.send.assert_not_called()


def test_monitor_waits_for_pnl_before_sending_without_it():
    fetch = Mock(return_value=([_closed(realized_pnl=None)], []))
    monitor, sender, clock = _monitor(fetch)
    monitor.set_enabled(True)
    for poll in range(1, 4):
        clock[0] = 15 * poll
        monitor.tick([ACCOUNT])
    sender.send.assert_not_called()
    clock[0] = 60
    monitor.tick([ACCOUNT])
    sender.send.assert_called_once()

    # Realized PNL arriving on a later check is used as soon as it is available.
    monitor2, sender2, clock2 = _monitor(Mock(side_effect=[
        ([_closed("late", realized_pnl=None)], []),
        ([_closed("late")], []),
    ]))
    monitor2.set_enabled(True)
    clock2[0] = 15
    monitor2.tick([ACCOUNT])
    clock2[0] = 30
    monitor2.tick([ACCOUNT])
    assert "PNL +$12.50" in sender2.send.call_args.args[0]


def test_monitor_retries_failed_sends_then_gives_up():
    fetch = Mock(return_value=([_closed()], []))
    sender = Mock()
    sender.send.side_effect = [OSError("down"), None]
    monitor, _sender, clock = _monitor(fetch, sender)
    monitor.set_enabled(True)
    clock[0] = 15
    monitor.tick([ACCOUNT])
    assert monitor.notice == "Email alert failed for TRX-USDT TP: OSError"
    clock[0] = 30
    monitor.tick([ACCOUNT])
    assert sender.send.call_count == 2
    assert monitor.notice.startswith("Email sent")

    sender.send.side_effect = OSError("down")
    fetch.return_value = ([_closed("other")], [])
    for poll in range(3, 8):
        clock[0] = 15 * poll
        monitor.tick([ACCOUNT])
    assert sender.send.call_count == 5  # Three attempts for "other", then dropped.


def test_monitor_reports_read_errors_and_failed_checks():
    fetch = Mock(side_effect=[([], ["BingX: HTTP 500 from BingX."]), RuntimeError("boom")])
    monitor, _sender, clock = _monitor(fetch)
    monitor.set_enabled(True)
    clock[0] = 15
    monitor.tick([ACCOUNT])
    assert monitor.notice == "Email alert check incomplete: BingX: HTTP 500 from BingX."
    clock[0] = 30
    monitor.tick([ACCOUNT])
    assert monitor.notice == "Email alert check failed: RuntimeError"


# Tracker reads


def test_tracker_fetches_lighter_fills_only_when_close_orders_exist_and_reads_bingx():
    tracker = ActiveTradesTracker.__new__(ActiveTradesTracker)
    tracker.auth_provider = Mock()
    tracker.auth_provider.token_for.return_value = "token"
    tracker.client = Mock()
    tracker.client.fetch_inactive_orders.side_effect = [[_lighter_order()], [_lighter_order(type="limit")]]
    tracker.client.fetch_trades.return_value = [_lighter_fill()]
    tracker.bingx_client = Mock()
    tracker.bingx_client.fetch_order_history.return_value = [_bingx_order()]
    sub = AccountInfo(index=5, display_name="Sub 1")
    closes, errors = tracker.fetch_closed_trades(
        [ACCOUNT, sub, AccountInfo(index=0, name="Main", exchange="bingx")], SINCE
    )
    assert errors == []
    assert [close.alert_id for close in closes] == ["lighter:748853:562953566352406", "bingx:2108605560895270999"]
    tracker.client.fetch_trades.assert_called_once_with(
        account_index=748853, market_id=None, authorization="token", limit=100
    )
    window = tracker.bingx_client.fetch_order_history.call_args.kwargs
    # Starts at `since`, clamped to BingX's bounded history range.
    assert window["start_ms"] >= int(SINCE.timestamp() * 1000)
    assert window["end_ms"] - window["start_ms"] <= 24 * 60 * 60 * 1000


def test_tracker_skips_lighter_accounts_without_credentials_and_reports_api_errors():
    from active_trades.bingx_api import BingxApiError
    from active_trades.lighter_api import AuthUnavailable

    tracker = ActiveTradesTracker.__new__(ActiveTradesTracker)
    tracker.auth_provider = Mock()
    tracker.auth_provider.token_for.side_effect = AuthUnavailable("needs key")
    tracker.client = Mock()
    tracker.bingx_client = Mock()
    tracker.bingx_client.fetch_order_history.side_effect = BingxApiError("HTTP 500 from BingX.")
    closes, errors = tracker.fetch_closed_trades([ACCOUNT, AccountInfo(index=0, exchange="bingx")], SINCE)
    assert closes == []
    assert errors == ["BingX: HTTP 500 from BingX."]
    tracker.client.fetch_inactive_orders.assert_not_called()


# Key, rendering and email transport


def test_ctrl_e_toggles_email_alerts():
    pager = DashboardPager()
    pager._consume("\x05")
    assert pager.email_alerts is True
    pager._consume("e\x05")
    assert pager.email_alerts is False
    assert pager.hide_amounts is False


def test_plain_header_shows_email_alert_state_only_when_known():
    snapshot = TrackerSnapshot(SINCE, ())
    assert "Email alerts ON" in render_plain(snapshot, refresh_seconds=15, email_alerts=True, width=100)
    assert "Email alerts off" in render_plain(snapshot, refresh_seconds=15, email_alerts=False, width=100)
    assert "Email alerts" not in render_plain(snapshot, refresh_seconds=15, width=100)


def test_email_settings_use_shared_ses_variables():
    settings = EmailSettings.from_env({
        "SES_SMTP_HOST": "email-smtp.eu-west-1.amazonaws.com", "SES_IAM_USER": "user",
        "SES_SMTP_PASS": "secret", "EMAIL_FROM": "bot@example.com", "EMAIL_FROM_NAME": "Trades",
        "ALERT_EMAIL_TO": "a@example.com; b@example.com",
    })
    assert settings.port == 587
    assert settings.username == "user"
    assert settings.recipients == ("a@example.com", "b@example.com")
    assert "secret" not in repr(settings)
    with pytest.raises(EmailConfigError, match="ALERT_EMAIL_TO"):
        EmailSettings.from_env({"SES_SMTP_HOST": "h", "SES_SMTP_USER": "u", "SES_SMTP_PASS": "p", "EMAIL_FROM": "f@x.io"})


def test_email_sender_uses_starttls_and_login():
    server = MagicMock()
    server.__enter__.return_value = server
    smtp = Mock(return_value=server)
    settings = EmailSettings("smtp.example.com", "user", "pass", "bot@example.com", ("me@example.com",), "Trades")
    EmailSender(settings, smtp_factory=smtp).send("SL hit: BTC", "body\n")
    smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("user", "pass")
    message = server.send_message.call_args.args[0]
    assert message["Subject"] == "SL hit: BTC"
    assert message["To"] == "me@example.com"
    assert message["From"] == "Trades <bot@example.com>"


# CLI


def test_email_alerts_flag_is_opt_in():
    from active_trades.cli import parse_args

    assert parse_args([]).email_alerts is False
    assert parse_args(["--email-alerts"]).email_alerts is True


def test_live_loop_ticks_alerts_with_snapshot_accounts_and_shows_notice():
    from unittest.mock import patch

    from active_trades.cli import _live_loop
    from active_trades.models import AccountSnapshot

    snapshot = TrackerSnapshot(SINCE, (AccountSnapshot(account=ACCOUNT, trades=()),))
    tracker = Mock()
    tracker.apply_live_positions.side_effect = lambda current, _updates: current
    stream = Mock(notice=None)
    stream.latest_positions.return_value = {}
    alerts = Mock(notice="Email sent: BTC SL hit (Lighter · Main (#748853))")
    rendered = []
    settings = SimpleNamespace(refresh_seconds=15, pnl_refresh_seconds=0.5)
    with patch("active_trades.cli._sleep"):
        _live_loop(
            tracker, snapshot, settings, stream, Mock(), rendered.append,
            lambda: len(rendered) == 1, alerts=alerts,
        )
    assert list(alerts.tick.call_args.args[0]) == [ACCOUNT]
    assert rendered[0].errors == ("Email sent: BTC SL hit (Lighter · Main (#748853))",)


def test_bingx_order_history_signs_sorted_query_as_sent():
    import hashlib
    import hmac
    import io
    import json
    from unittest.mock import patch
    from urllib.parse import urlsplit

    from active_trades.bingx_api import BingxRestClient

    body = io.BytesIO(json.dumps({"code": 0, "data": {"orders": [_bingx_order()]}}).encode())
    response = MagicMock()
    response.__enter__.return_value = body
    client = BingxRestClient(api_key="key", api_secret="secret", base_url="https://example.test")
    with patch("active_trades.bingx_api.urlopen", return_value=response) as send:
        orders = client.fetch_order_history(start_ms=1000, end_ms=2000, limit=500)
    assert orders[0]["symbol"] == "ATOM-USDT"
    query = urlsplit(send.call_args.args[0].full_url).query
    unsigned, signature = query.rsplit("&signature=", 1)
    keys = [part.split("=", 1)[0] for part in unsigned.split("&")]
    assert keys == sorted(keys) == ["endTime", "limit", "recvWindow", "startTime", "timestamp"]
    assert signature == hmac.new(b"secret", unsigned.encode(), hashlib.sha256).hexdigest()
