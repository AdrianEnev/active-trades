import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from active_trades.bingx_api import BingxApiError, BingxAuthUnavailable
from active_trades.lighter_api import AuthUnavailable, LighterApiError
from active_trades.models import AccountInfo, AccountSnapshot, ActiveTrade, LimitOrder, Position, StopOrder, TrackerSnapshot
from active_trades.risk import find_protective_stop
from active_trades.settings import Settings
from active_trades.tracker import ActiveTradesTracker


@dataclass
class FakeAuthProvider:
    token: str = "auth"

    def token_for(self, account_index):
        return self.token


class FakeClient:
    def fetch_accounts(self, *, by, value, active_only):
        return []

    def fetch_account_by_index(self, account_index, *, active_only=True):
        return {
            "index": account_index,
            "l1_address": "0x1234567890abcdef1234567890abcdef12345678",
            "name": "main",
            "positions": [
                {
                    "market_id": 1,
                    "symbol": "BTC",
                    "sign": 1,
                    "position": "0.2",
                    "avg_entry_price": "100000",
                }
            ],
        }

    def fetch_sub_accounts_by_l1_address(self, l1_address):
        return [{"index": 1, "name": "main", "l1_address": l1_address}]

    def fetch_active_orders(self, account_index, authorization):
        return [
            {
                "market_index": 1,
                "side": "sell",
                "trigger_price": "99000",
                "reduce_only": True,
                "type": "stop-loss",
                "status": "open",
            }
        ]

    def fetch_trades(self, *, account_index, market_id, authorization, limit):
        return []

    def fetch_market_symbols(self):
        return {1: "BTC", 2: "ETH", 3: "SOL"}


def make_settings(state_path, **overrides):
    return Settings(**{
        "base_url": "https://example.invalid",
        "explorer_url": "https://explorer.invalid",
        "account_index": 1,
        "api_key_index": None,
        "api_private_key": None,
        "read_only_auth_token": None,
        "refresh_seconds": 15,
        "timeout_seconds": 15,
        "state_path": state_path,
        "explicit_account_indexes": (1,),
        "trade_lookback_limit": 100,
        "auth_token_ttl_seconds": 3600,
        **overrides,
    })


def make_position(*, account_index=1, market_id=1, side="long", size="0.2", entry="100000", pnl="10"):
    return Position(
        account_index=account_index,
        market_id=market_id,
        symbol={1: "BTC", 2: "ETH", 3: "SOL"}[market_id],
        side=side,
        size=Decimal(size),
        avg_entry_price=Decimal(entry),
        unrealized_pnl=Decimal(pnl),
    )


REST_TIME = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


def make_snapshot(*, orders_state="available", accounts=(1,)):
    sections = []
    for account_index in accounts:
        position = make_position(account_index=account_index)
        trade = ActiveTrade(
            row_number=1,
            position=position,
            opened_at=REST_TIME - timedelta(days=1),
            opened_at_source="state",
            stop_loss=Decimal("99000"),
            risk_usd=Decimal("200"),
            risk_state="at-risk",
            risk_note="cached note",
        )
        limit = LimitOrder(account_index, 2, "ETH", "buy", Decimal("2000"), Decimal("1"))
        sections.append(AccountSnapshot(
            AccountInfo(account_index, name=f"Account {account_index}"),
            (trade,),
            errors=("cached warning",),
            limit_orders=(limit,),
            orders_state=orders_state,
        ))
    return TrackerSnapshot(REST_TIME, tuple(sections), pnl_collected_at=REST_TIME)


def bingx_position(*, side="LONG", position_id="long-position", created=True):
    row = {
        "symbol": "BTC-USDT",
        "positionId": position_id,
        "positionSide": side,
        "positionAmt": "0.3" if side == "LONG" else "-0.4",
        "avgPrice": "100000",
        "unrealizedProfit": "12.5",
    }
    if created:
        row["createTime"] = int(REST_TIME.timestamp() * 1000)
    return row


def bingx_stop(*, side="SELL", position_side="LONG", position_id="long-position", price="98000"):
    return {
        "symbol": "BTC-USDT",
        "orderId": f"stop-{position_id}",
        "positionID": position_id,
        "side": side,
        "positionSide": position_side,
        "type": "STOP_MARKET",
        "stopPrice": price,
        "origQty": "0.3",
        "executedQty": "0",
        "status": "NEW",
        "reduceOnly": True,
    }


def bingx_limit():
    return {
        "symbol": "ETH-USDT",
        "orderId": "bingx-limit",
        "side": "BUY",
        "positionSide": "LONG",
        "type": "LIMIT",
        "price": "2000",
        "origQty": "2",
        "executedQty": "0.5",
        "status": "PARTIALLY_FILLED",
    }


def fake_bingx(*, positions=None, orders=None):
    return Mock(
        fetch_positions=Mock(return_value=[bingx_position()] if positions is None else positions),
        fetch_active_orders=Mock(return_value=[bingx_stop(), bingx_limit()] if orders is None else orders),
    )


class TestTracker(TestCase):
    def test_collect_snapshot_builds_trade_risk(self):
        with TemporaryDirectory() as tmp:
            settings = Settings(
                base_url="https://example.invalid",
                explorer_url="https://explorer.invalid",
                account_index=1,
                api_key_index=4,
                api_private_key="secret",
                read_only_auth_token=None,
                refresh_seconds=15,
                timeout_seconds=15,
                state_path=Path(tmp) / "state.json",
                explicit_account_indexes=(1,),
                trade_lookback_limit=100,
                auth_token_ttl_seconds=3600,
            )
            tracker = ActiveTradesTracker(
                settings,
                client=FakeClient(),
                auth_provider=FakeAuthProvider(),
            )

            snapshot = tracker.collect_snapshot()

            self.assertEqual(len(snapshot.accounts), 1)
            trade = snapshot.accounts[0].trades[0]
            self.assertEqual(trade.position.symbol, "BTC")
            self.assertEqual(trade.risk_usd, Decimal("200.0"))
            self.assertTrue(settings.state_path.exists())

    def test_limit_orders_are_fetched_for_accounts_without_positions(self):
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp) / "state.json", explicit_account_indexes=(1, 2))
            client = FakeClient()
            with patch.object(client, "fetch_account_by_index", side_effect=lambda index, **kw: {
                "index": index, "positions": []
            }), patch.object(client, "fetch_active_orders", return_value=[{
                "market_index": 2,
                "type": "limit",
                "status": "open",
                "is_ask": False,
                "price": "2000",
                "remaining_base_amount": "0.5",
            }]) as orders, patch.object(client, "fetch_market_symbols", wraps=client.fetch_market_symbols) as symbols:
                snapshot = ActiveTradesTracker(
                    settings, client=client, auth_provider=FakeAuthProvider()
                ).collect_snapshot()

            self.assertEqual([section.account.index for section in snapshot.accounts], [1, 2])
            self.assertTrue(all(section.trades == () for section in snapshot.accounts))
            self.assertEqual([section.limit_orders[0].symbol for section in snapshot.accounts], ["ETH", "ETH"])
            self.assertEqual([section.limit_orders[0].account_index for section in snapshot.accounts], [1, 2])
            self.assertEqual(orders.call_count, 2)
            symbols.assert_called_once_with()

    def test_limits_and_protective_stops_share_one_order_read(self):
        with TemporaryDirectory() as tmp:
            client = FakeClient()
            raw_orders = client.fetch_active_orders(1, "auth") + [{
                "market_index": 1, "type": "limit", "status": "open", "is_ask": False,
                "price": "95000", "remaining_base_amount": "0.1",
            }]
            auth = FakeAuthProvider()
            with patch.object(client, "fetch_active_orders", return_value=raw_orders) as orders, \
                    patch.object(client, "fetch_market_symbols", wraps=client.fetch_market_symbols) as symbols, \
                    patch.object(auth, "token_for", wraps=auth.token_for) as token:
                snapshot = ActiveTradesTracker(
                    make_settings(Path(tmp) / "state.json"), client=client, auth_provider=auth
                ).collect_snapshot()

            section = snapshot.accounts[0]
            self.assertEqual(section.trades[0].risk_usd, Decimal("200"))
            self.assertEqual(section.limit_orders[0].symbol, "BTC")
            self.assertEqual(section.limit_orders[0].remaining_base_amount, Decimal("0.1"))
            orders.assert_called_once_with(1, "auth")
            token.assert_called_once_with(1)
            symbols.assert_not_called()

    def test_missing_auth_is_distinguished_from_an_order_request_failure(self):
        for error, expected_state in (
            (AuthUnavailable("Credentials missing"), "auth-needed"),
            (LighterApiError("Request failed"), "unavailable"),
        ):
            with self.subTest(expected_state=expected_state), TemporaryDirectory() as tmp:
                client = FakeClient()
                auth = FakeAuthProvider()
                method = auth if expected_state == "auth-needed" else client
                method_name = "token_for" if expected_state == "auth-needed" else "fetch_active_orders"
                with patch.object(method, method_name, side_effect=error):
                    snapshot = ActiveTradesTracker(
                        make_settings(Path(tmp) / "state.json"), client=client, auth_provider=auth
                    ).collect_snapshot()

                section = snapshot.accounts[0]
                self.assertEqual(section.orders_state, expected_state)
                self.assertEqual(section.limit_orders, ())
                self.assertEqual(section.trades[0].risk_state, expected_state)
                self.assertIsNone(section.trades[0].risk_usd)
                self.assertTrue(section.errors)

    def test_disabled_order_reads_skip_authentication_and_market_metadata(self):
        with TemporaryDirectory() as tmp:
            client = FakeClient()
            auth = FakeAuthProvider()
            with patch.object(client, "fetch_active_orders") as orders, \
                    patch.object(client, "fetch_market_symbols") as symbols, \
                    patch.object(auth, "token_for") as token:
                snapshot = ActiveTradesTracker(
                    make_settings(Path(tmp) / "state.json", fetch_orders=False),
                    client=client,
                    auth_provider=auth,
                ).collect_snapshot()

            section = snapshot.accounts[0]
            self.assertEqual(section.orders_state, "disabled")
            self.assertEqual(section.trades[0].risk_state, "disabled")
            self.assertEqual(section.limit_orders, ())
            self.assertEqual(section.errors, ())
            orders.assert_not_called()
            symbols.assert_not_called()
            token.assert_not_called()

    def test_market_lookup_failure_keeps_limit_order_with_market_id(self):
        with TemporaryDirectory() as tmp:
            client = FakeClient()
            with patch.object(client, "fetch_active_orders", return_value=[{
                "market_index": 2, "type": "limit", "is_ask": True,
                "price": "2000", "remaining_base_amount": "1",
            }]), patch.object(client, "fetch_market_symbols", side_effect=LighterApiError("Offline")):
                snapshot = ActiveTradesTracker(
                    make_settings(Path(tmp) / "state.json"), client=client, auth_provider=FakeAuthProvider()
                ).collect_snapshot()

            self.assertEqual(snapshot.accounts[0].orders_state, "available")
            self.assertEqual(snapshot.accounts[0].limit_orders[0].symbol, "2")
            self.assertIn("showing market IDs", snapshot.accounts[0].errors[0])


class TestBingxTracker(TestCase):
    def test_snapshot_combines_positions_and_limits_from_both_exchanges(self):
        with TemporaryDirectory() as tmp:
            lighter = FakeClient()
            lighter_orders = lighter.fetch_active_orders(1, "auth") + [{
                "market_index": 1, "type": "limit", "status": "open", "is_ask": False,
                "price": "95000", "remaining_base_amount": "0.1",
            }]
            bingx = fake_bingx(
                positions=[bingx_position(), bingx_position(side="SHORT", position_id="short-position")],
                orders=[
                    bingx_stop(),
                    bingx_stop(side="BUY", position_side="SHORT", position_id="short-position", price="101000"),
                    bingx_limit(),
                ],
            )
            with patch.object(lighter, "fetch_active_orders", return_value=lighter_orders):
                snapshot = ActiveTradesTracker(
                    make_settings(Path(tmp) / "state.json"), client=lighter,
                    auth_provider=FakeAuthProvider(), bingx_client=bingx,
                ).collect_snapshot()

            self.assertEqual([section.account.exchange for section in snapshot.accounts], ["lighter", "bingx"])
            lighter_section, bingx_section = snapshot.accounts
            self.assertEqual(lighter_section.trades[0].risk_usd, Decimal("200"))
            self.assertEqual([trade.position.side for trade in bingx_section.trades], ["long", "short"])
            self.assertEqual([trade.risk_usd for trade in bingx_section.trades], [Decimal("600"), Decimal("400")])
            self.assertTrue(all(trade.opened_at == REST_TIME for trade in bingx_section.trades))
            self.assertTrue(all(trade.opened_at_source == "exchange" for trade in bingx_section.trades))
            self.assertEqual([section.limit_orders[0].exchange for section in snapshot.accounts], ["lighter", "bingx"])
            self.assertEqual(bingx_section.limit_orders[0].remaining_base_amount, Decimal("1.5"))
            bingx.fetch_positions.assert_called_once_with()
            bingx.fetch_active_orders.assert_called_once_with()

    def test_protective_stops_do_not_cross_exchanges_or_bingx_position_ids(self):
        position = replace(
            make_position(account_index=0), market_id="BTC-USDT", symbol="BTC-USDT",
            exchange="bingx", position_id="expected-position",
        )
        candidates = [
            StopOrder(
                account_index=0, market_id="BTC-USDT", side="sell", trigger_price=Decimal("99000"),
                order_type="stop-loss", reduce_only=True, status="open", exchange=exchange,
                raw={"positionSide": "LONG", "positionID": position_id},
            )
            for exchange, position_id in (("lighter", "expected-position"), ("bingx", "other-position"))
        ]

        stop, state, _ = find_protective_stop(position, candidates)

        self.assertIsNone(stop)
        self.assertEqual(state, "missing")

    def test_state_preserves_legacy_lighter_keys_and_bingx_dates_across_failed_refresh(self):
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            old_date = REST_TIME - timedelta(days=5)
            state_path.write_text(json.dumps({"positions": {
                "1:1:long": old_date.isoformat(), "closed-position": old_date.isoformat(),
            }}))
            bingx = fake_bingx()
            tracker = ActiveTradesTracker(
                make_settings(state_path), client=FakeClient(),
                auth_provider=FakeAuthProvider(), bingx_client=bingx,
            )

            first = tracker.collect_snapshot()
            position = first.accounts[1].trades[0].position
            saved = json.loads(state_path.read_text())["positions"]
            self.assertEqual(set(saved), {"1:1:long", position.key})
            self.assertTrue(position.key.startswith("bingx:0:BTC-USDT:long:"))
            self.assertEqual(first.accounts[0].trades[0].opened_at, old_date)
            self.assertEqual(first.accounts[1].trades[0].opened_at, REST_TIME)

            bingx.fetch_positions.side_effect = BingxApiError("temporarily unavailable")
            failed = tracker.collect_snapshot()
            self.assertEqual(failed.accounts[1].positions_state, "unavailable")
            self.assertEqual(json.loads(state_path.read_text())["positions"], saved)

            bingx.fetch_positions.side_effect = None
            bingx.fetch_positions.return_value = [bingx_position(created=False)]
            recovered = tracker.collect_snapshot()
            self.assertEqual(recovered.accounts[1].trades[0].opened_at, REST_TIME)
            self.assertEqual(recovered.accounts[1].trades[0].opened_at_source, "state")

    def test_bingx_order_reads_are_independent_of_position_reads(self):
        for error in (None, BingxApiError("positions offline"), BingxAuthUnavailable("credentials missing")):
            with self.subTest(error=error), TemporaryDirectory() as tmp:
                bingx = fake_bingx(positions=[])
                bingx.fetch_positions.side_effect = error
                snapshot = ActiveTradesTracker(
                    make_settings(Path(tmp) / "state.json"), client=FakeClient(),
                    auth_provider=FakeAuthProvider(), bingx_client=bingx,
                ).collect_snapshot()

                self.assertEqual(snapshot.accounts[0].trades[0].position.exchange, "lighter")
                section = snapshot.accounts[1]
                self.assertEqual(section.trades, ())
                self.assertEqual(section.orders_state, "available")
                self.assertEqual(len(section.limit_orders), 1)
                bingx.fetch_active_orders.assert_called_once_with()

    def test_bingx_order_failure_keeps_positions_and_lighter_orders(self):
        for error, expected_state in (
            (BingxApiError("orders offline"), "unavailable"),
            (BingxAuthUnavailable("credentials missing"), "auth-needed"),
        ):
            with self.subTest(expected_state=expected_state), TemporaryDirectory() as tmp:
                bingx = fake_bingx()
                bingx.fetch_active_orders.side_effect = error
                snapshot = ActiveTradesTracker(
                    make_settings(Path(tmp) / "state.json"), client=FakeClient(),
                    auth_provider=FakeAuthProvider(), bingx_client=bingx,
                ).collect_snapshot()

                self.assertEqual(snapshot.accounts[0].orders_state, "available")
                self.assertEqual(snapshot.accounts[0].trades[0].risk_usd, Decimal("200"))
                section = snapshot.accounts[1]
                self.assertEqual(section.positions_state, "available")
                self.assertEqual(section.orders_state, expected_state)
                self.assertEqual(section.trades[0].risk_state, expected_state)
                self.assertIsNone(section.trades[0].risk_usd)
                self.assertEqual(section.limit_orders, ())

    def test_lighter_discovery_failure_still_collects_bingx(self):
        with TemporaryDirectory() as tmp:
            lighter = FakeClient()
            bingx = fake_bingx()
            with patch.object(lighter, "fetch_account_by_index", side_effect=LighterApiError("Lighter offline")):
                snapshot = ActiveTradesTracker(
                    make_settings(Path(tmp) / "state.json", explicit_account_indexes=()),
                    client=lighter, auth_provider=FakeAuthProvider(), bingx_client=bingx,
                ).collect_snapshot()

            self.assertEqual([section.account.exchange for section in snapshot.accounts], ["bingx"])
            self.assertEqual(len(snapshot.accounts[0].trades), 1)
            self.assertEqual(len(snapshot.accounts[0].limit_orders), 1)
            self.assertIn("Lighter account discovery failed", snapshot.errors[0])

    def test_disabled_orders_apply_to_both_exchanges(self):
        with TemporaryDirectory() as tmp:
            lighter = FakeClient()
            bingx = fake_bingx()
            with patch.object(lighter, "fetch_active_orders") as lighter_orders:
                snapshot = ActiveTradesTracker(
                    make_settings(Path(tmp) / "state.json", fetch_orders=False),
                    client=lighter, auth_provider=FakeAuthProvider(), bingx_client=bingx,
                ).collect_snapshot()

            self.assertEqual([section.orders_state for section in snapshot.accounts], ["disabled", "disabled"])
            self.assertTrue(all(section.limit_orders == () for section in snapshot.accounts))
            self.assertTrue(all(section.trades[0].risk_state == "disabled" for section in snapshot.accounts))
            lighter_orders.assert_not_called()
            bingx.fetch_active_orders.assert_not_called()

    def test_partial_bingx_credentials_report_auth_needed_without_network(self):
        with TemporaryDirectory() as tmp:
            snapshot = ActiveTradesTracker(
                make_settings(Path(tmp) / "state.json", bingx_api_key="test-key"),
                client=FakeClient(), auth_provider=FakeAuthProvider(),
            ).collect_snapshot()

            section = snapshot.accounts[1]
            self.assertEqual(section.account.exchange, "bingx")
            self.assertEqual(section.positions_state, "auth-needed")
            self.assertEqual(section.orders_state, "auth-needed")
            self.assertEqual(section.trades, ())
            self.assertEqual(section.limit_orders, ())


class TestLivePositionsMerge(TestCase):
    def setUp(self):
        self.client = Mock()
        self.auth = Mock()
        self.store = Mock()
        self.tracker = ActiveTradesTracker(
            make_settings(Path("unused-state.json")),
            client=self.client,
            auth_provider=self.auth,
            state_store=self.store,
        )

    def update(self, positions, *, seconds=1):
        return SimpleNamespace(positions=tuple(positions), received_at=REST_TIME + timedelta(seconds=seconds))

    def test_live_size_and_entry_changes_recompute_risk_with_cached_stop(self):
        snapshot = make_snapshot()
        position = make_position(size="0.35", entry="100500", pnl="0")

        result = self.tracker.apply_live_positions(snapshot, {1: self.update([position])})

        old = snapshot.accounts[0].trades[0]
        trade = result.accounts[0].trades[0]
        self.assertEqual(trade.position.unrealized_pnl, Decimal("0"))
        self.assertEqual(trade.risk_usd, Decimal("525"))
        self.assertEqual(trade.opened_at, old.opened_at)
        self.assertEqual(trade.opened_at_source, "state")
        self.assertEqual(trade.stop_loss, old.stop_loss)
        self.assertEqual(trade.risk_note, "cached note")
        self.assertEqual(result.accounts[0].limit_orders, snapshot.accounts[0].limit_orders)
        self.assertEqual(result.accounts[0].errors, snapshot.accounts[0].errors)
        self.assertEqual(result.collected_at, REST_TIME)
        self.assertEqual(result.pnl_collected_at, REST_TIME + timedelta(seconds=1))
        self.assertEqual(snapshot.accounts[0].trades[0].position.size, Decimal("0.2"))

    def test_live_short_risk_and_profit_lock(self):
        snapshot = make_snapshot()
        old = replace(snapshot.accounts[0].trades[0], position=make_position(side="short"), stop_loss=Decimal("101000"))
        snapshot = replace(snapshot, accounts=(replace(snapshot.accounts[0], trades=(old,)),))
        for entry, risk in (("100500", "175"), ("102000", "0")):
            with self.subTest(entry=entry):
                result = self.tracker.apply_live_positions(snapshot, {
                    1: self.update([make_position(side="short", size="0.35", entry=entry)])
                })
                self.assertEqual(result.accounts[0].trades[0].risk_usd, Decimal(risk))

    def test_new_closed_and_reversed_positions_update_rows_without_reusing_stop(self):
        snapshot = make_snapshot()
        old = snapshot.accounts[0].trades[0]
        closed = replace(old, row_number=2, position=make_position(market_id=2, size="1", entry="2000"))
        snapshot = replace(snapshot, accounts=(replace(snapshot.accounts[0], trades=(old, closed)),))
        result = self.tracker.apply_live_positions(snapshot, {1: self.update([
            make_position(side="short", pnl="-10"),
            make_position(market_id=3, size="2", entry="180"),
        ])})

        trades = result.accounts[0].trades
        self.assertEqual([trade.position.market_id for trade in trades], [1, 3])
        self.assertEqual([trade.row_number for trade in trades], [1, 2])
        self.assertEqual(trades[0].position.side, "short")
        for trade in trades:
            self.assertIsNone(trade.stop_loss)
            self.assertIsNone(trade.risk_usd)
            self.assertEqual(trade.risk_state, "unavailable")
            self.assertEqual(trade.opened_at_source, "first-seen")
            self.assertEqual(trade.opened_at, REST_TIME + timedelta(seconds=1))

    def test_new_positions_keep_auth_needed_or_disabled_order_state(self):
        for state in ("auth-needed", "disabled", "unavailable"):
            with self.subTest(state=state):
                result = self.tracker.apply_live_positions(make_snapshot(orders_state=state), {
                    1: self.update([make_position(market_id=2)])
                })
                self.assertEqual(result.accounts[0].trades[0].risk_state, state)

    def test_empty_live_positions_remove_closed_positions(self):
        result = self.tracker.apply_live_positions(make_snapshot(), {1: self.update([])})
        self.assertEqual(result.accounts[0].trades, ())
        self.assertEqual(len(result.accounts[0].limit_orders), 1)

    def test_live_merge_performs_no_rest_history_auth_or_state_operations(self):
        self.tracker.apply_live_positions(make_snapshot(), {1: self.update([make_position(pnl="15")])})
        self.assertEqual(self.client.mock_calls, [])
        self.assertEqual(self.auth.mock_calls, [])
        self.assertEqual(self.store.mock_calls, [])

    def test_updates_at_or_before_rest_snapshot_are_ignored(self):
        snapshot = make_snapshot()
        for seconds in (-1, 0):
            with self.subTest(seconds=seconds):
                result = self.tracker.apply_live_positions(snapshot, {
                    1: self.update([make_position(pnl="999")], seconds=seconds)
                })
                self.assertEqual(result, snapshot)

    def test_older_stream_update_cannot_overwrite_an_already_merged_update(self):
        snapshot = make_snapshot()
        newer = self.tracker.apply_live_positions(snapshot, {
            1: self.update([make_position(pnl="20")], seconds=2)
        })
        result = self.tracker.apply_live_positions(newer, {
            1: self.update([make_position(pnl="10")], seconds=1)
        })
        self.assertEqual(result.accounts[0].trades[0].position.unrealized_pnl, Decimal("20"))
        self.assertEqual(result.pnl_collected_at, REST_TIME + timedelta(seconds=2))

    def test_stream_freshness_is_independent_for_each_account(self):
        snapshot = make_snapshot(accounts=(1, 2))
        first = self.tracker.apply_live_positions(snapshot, {
            1: self.update([make_position(pnl="20")], seconds=2)
        })
        result = self.tracker.apply_live_positions(first, {
            2: self.update([make_position(account_index=2, pnl="15")], seconds=1)
        })
        self.assertEqual(result.accounts[0].trades[0].position.unrealized_pnl, Decimal("20"))
        self.assertEqual(result.accounts[1].trades[0].position.unrealized_pnl, Decimal("15"))
        self.assertEqual(result.pnl_collected_at, REST_TIME + timedelta(seconds=2))

    def test_lighter_stream_does_not_overwrite_bingx_with_the_same_account_index(self):
        snapshot = make_snapshot(accounts=(0,))
        lighter_section = snapshot.accounts[0]
        bingx_position = replace(
            lighter_section.trades[0].position,
            exchange="bingx", market_id="BTC-USDT", position_id="bingx-position",
        )
        bingx_section = replace(
            lighter_section,
            account=replace(lighter_section.account, exchange="bingx"),
            trades=(replace(lighter_section.trades[0], position=bingx_position),),
        )
        snapshot = replace(snapshot, accounts=(lighter_section, bingx_section))

        result = self.tracker.apply_live_positions(snapshot, {
            0: self.update([make_position(account_index=0, pnl="999")]),
        })

        self.assertEqual(result.accounts[0].trades[0].position.unrealized_pnl, Decimal("999"))
        self.assertIs(result.accounts[1], bingx_section)
        self.assertEqual(result.accounts[1].trades[0].position.unrealized_pnl, Decimal("10"))
