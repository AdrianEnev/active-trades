from datetime import UTC, datetime
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from active_trades.lighter_api import (
    LighterApiError,
    LighterRestClient,
    limit_orders_from_dicts,
    position_from_dict,
    stop_order_from_dict,
    trade_from_dict,
)


class TestLighterApiParsing(TestCase):
    def test_position_from_dict_uses_sign_and_absolute_size(self):
        position = position_from_dict(
            {
                "market_id": 3,
                "symbol": "SOL",
                "sign": -1,
                "position": "12.5",
                "avg_entry_price": "180.25",
            },
            account_index=9,
        )

        self.assertEqual(position.side, "short")
        self.assertEqual(position.size, Decimal("12.5"))
        self.assertEqual(position.avg_entry_price, Decimal("180.25"))

    def test_stop_order_from_dict_uses_trigger_and_is_ask_side(self):
        order = stop_order_from_dict(
            {
                "market_index": 3,
                "is_ask": True,
                "trigger_price": "175",
                "reduce_only": True,
                "type": "stop-loss",
                "status": "open",
            },
            account_index=9,
        )

        self.assertEqual(order.side, "sell")
        self.assertEqual(order.trigger_price, Decimal("175"))

    def test_position_preserves_zero_unrealized_pnl(self):
        for value in (0, "0", "0.00"):
            with self.subTest(value=value):
                position = position_from_dict(
                    {
                        "market_id": 3,
                        "position": "2",
                        "avg_entry_price": "180",
                        "unrealized_pnl": value,
                        "pnl": "99",
                    },
                    account_index=9,
                )
                self.assertEqual(position.unrealized_pnl, Decimal("0"))

    def test_position_rejects_non_finite_pnl(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                position = position_from_dict(
                    {"market_id": 3, "position": "2", "avg_entry_price": "180", "unrealized_pnl": value},
                    account_index=9,
                )
                self.assertIsNone(position.unrealized_pnl)

    def test_limit_order_uses_remaining_size_is_ask_symbol_and_creation_time(self):
        raw = {
            "market_index": 3,
            "type": "limit",
            "status": "open",
            "price": "180.2500",
            "initial_base_amount": "2.5",
            "remaining_base_amount": "1.25",
            "is_ask": True,
            "side": "buy",  # Deprecated SDK field can disagree with is_ask.
            "reduce_only": True,
            "created_at": 1_728_000_000_123,
            "order_id": "12345",
        }
        orders = limit_orders_from_dicts([raw], account_index=9, symbols={3: "SOL"})

        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order.account_index, 9)
        self.assertEqual(order.market_id, 3)
        self.assertEqual(order.symbol, "SOL")
        self.assertEqual(order.side, "sell")
        self.assertEqual(order.price, Decimal("180.2500"))
        self.assertEqual(order.remaining_base_amount, Decimal("1.25"))
        self.assertEqual(order.created_at, datetime.fromtimestamp(1_728_000_000, UTC))
        self.assertEqual(order.order_id, "12345")
        self.assertTrue(order.reduce_only)

    def test_limit_orders_exclude_conditional_market_and_inactive_orders(self):
        base = {"market_index": 3, "price": "180", "remaining_base_amount": "1", "is_ask": False}
        excluded = [
            {**base, "type": order_type, "status": "open"}
            for order_type in (
                "stop-loss-limit", "take-profit-limit", "stop-loss", "take-profit", "market", "twap", ""
            )
        ]
        excluded.extend(
            {**base, "type": "limit", "status": status}
            for status in ("filled", "canceled", "canceled-expired", "unknown")
        )
        included = [
            {**base, "type": "limit", "status": status}
            for status in ("open", "pending", "in-progress", "")
        ]

        orders = limit_orders_from_dicts(excluded + included, account_index=9)

        self.assertEqual(len(orders), 4)
        self.assertTrue(all(order.side == "buy" for order in orders))

    def test_limit_orders_skip_invalid_or_fully_filled_values(self):
        base = {
            "market_index": 3, "type": "limit", "price": "180", "remaining_base_amount": "1", "is_ask": False
        }
        invalid = [None, {}, {**base, "market_index": None}]
        for field in ("price", "remaining_base_amount"):
            invalid.extend({**base, field: value} for value in (None, "bad", "NaN", "Infinity", "0", "-1"))
        invalid.append({**base, "is_ask": None, "side": "unknown"})

        self.assertEqual(limit_orders_from_dicts(invalid, account_index=9), [])

    def test_limit_order_handles_missing_metadata_and_invalid_timestamp(self):
        raw = {
            "market_id": 3,
            "type": "limit",
            "price": "180",
            "remaining_base_amount": "1",
            "side": "sell",
            "order_index": 0,
            "created_at": 10 ** 500,
        }
        order = limit_orders_from_dicts([raw], account_index=9)[0]

        self.assertEqual(order.symbol, "3")
        self.assertEqual(order.side, "sell")
        self.assertEqual(order.order_id, "0")
        self.assertIsNone(order.created_at)
        self.assertFalse(order.reduce_only)

    def test_limit_order_timestamp_accepts_supported_precisions(self):
        raw = {"market_id": 3, "type": "limit", "price": "180", "remaining_base_amount": "1", "is_ask": False}
        for multiplier in (1, 1_000, 1_000_000, 1_000_000_000):
            with self.subTest(multiplier=multiplier):
                order = limit_orders_from_dicts(
                    [{**raw, "timestamp": 1_728_000_000 * multiplier}], account_index=9
                )[0]
                self.assertEqual(order.created_at, datetime.fromtimestamp(1_728_000_000, UTC))

    def test_trade_from_dict_sets_signed_delta_for_ask_and_bid(self):
        ask_trade = trade_from_dict(
            {"market_id": 3, "size": "2", "price": "10", "ask_account_id": 9, "bid_account_id": 10},
            account_index=9,
            market_id=3,
        )
        bid_trade = trade_from_dict(
            {"market_id": 3, "size": "2", "price": "10", "ask_account_id": 9, "bid_account_id": 10},
            account_index=10,
            market_id=3,
        )

        self.assertEqual(ask_trade.signed_delta, Decimal("-2"))
        self.assertEqual(bid_trade.signed_delta, Decimal("2"))


class TestMarketSymbols(TestCase):
    def test_fetch_market_symbols_uses_public_perpetual_metadata(self):
        client = LighterRestClient(base_url="https://example.test")
        with patch.object(client, "get_json", return_value={
            "code": 200,
            "order_books": [{"market_id": 0, "symbol": "ETH"}, {"market_id": 3, "symbol": "SOL"}, {}],
        }) as request:
            self.assertEqual(client.fetch_market_symbols(), {0: "ETH", 3: "SOL"})
        request.assert_called_once_with("/api/v1/orderBooks", query={"filter": "perp"})

    def test_fetch_market_symbols_rejects_failure_and_malformed_payload(self):
        client = LighterRestClient(base_url="https://example.test")
        for payload in ({"code": 400, "message": "bad request"}, {"code": 200, "order_books": {}}):
            with self.subTest(payload=payload), patch.object(client, "get_json", return_value=payload):
                with self.assertRaises(LighterApiError):
                    client.fetch_market_symbols()
