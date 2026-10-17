from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import pytest

from active_trades.lighter_api import (
    AuthUnavailable,
    LighterApiError,
    LighterRestClient,
    cancel_orders,
    limit_orders_from_dicts,
    position_from_dict,
    signing_key_for,
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

    def test_stop_order_is_ask_overrides_deprecated_side(self):
        for is_ask, deprecated_side, expected in ((True, "buy", "sell"), (False, "sell", "buy")):
            with self.subTest(is_ask=is_ask):
                order = stop_order_from_dict(
                    {
                        "market_index": 3,
                        "is_ask": is_ask,
                        "side": deprecated_side,
                        "trigger_price": "175",
                        "reduce_only": True,
                        "type": "stop-loss",
                        "status": "pending",
                    },
                    account_index=9,
                )
                self.assertEqual(order.side, expected)

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

    def test_position_parses_realized_pnl_counter_without_zero_fallback(self):
        for value in (0, "0", "0.00", "123.456789", "-123.456789"):
            with self.subTest(value=value):
                position = position_from_dict(
                    {
                        "market_id": 3, "position": "2", "avg_entry_price": "180",
                        "realized_pnl": value, "unrealized_pnl": "99",
                    },
                    account_index=9,
                )
                self.assertEqual(position.realized_pnl, Decimal(str(value)))

    def test_position_missing_or_invalid_realized_pnl_is_unknown(self):
        base = {"market_id": 3, "position": "2", "avg_entry_price": "180"}
        self.assertIsNone(position_from_dict(base, account_index=9).realized_pnl)
        for value in (None, "", "invalid", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                position = position_from_dict({**base, "realized_pnl": value}, account_index=9)
                self.assertIsNone(position.realized_pnl)

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


class FakeSigner:
    instances = []

    def __init__(self, *, url, account_index, api_private_keys):
        self.url = url
        self.account_index = account_index
        self.api_private_keys = api_private_keys
        self.cancelled = []
        self.closed = False
        FakeSigner.instances.append(self)

    async def cancel_order(self, *, market_index, order_index):
        self.cancelled.append((market_index, order_index))
        if order_index == 2:
            return None, SimpleNamespace(code=21500, message="order not found"), None
        if order_index == 3:
            raise RuntimeError("HTTP 400\nBad Request\ninvalid nonce")
        if order_index == 4:
            return None, None, "invalid signature"
        return None, SimpleNamespace(code=200, message=None), None

    async def close(self):
        self.closed = True


def test_cancel_orders_reports_each_submission_and_closes_the_signer(monkeypatch):
    import lighter

    FakeSigner.instances.clear()
    monkeypatch.setattr(lighter, "SignerClient", FakeSigner)
    errors = cancel_orders(
        base_url="https://lighter.example", account_index=7, api_private_key="private",
        api_key_index=3, market_index=1, order_indexes=[1, 2, 3, 4],
    )
    assert errors == [None, "order not found", "invalid nonce", "invalid signature"]
    [signer] = FakeSigner.instances
    assert signer.account_index == 7
    assert signer.api_private_keys == {3: "private"}
    assert signer.cancelled == [(1, 1), (1, 2), (1, 3), (1, 4)]
    assert signer.closed


def test_cancel_orders_wraps_signer_setup_failures(monkeypatch):
    import lighter

    def broken(**_kwargs):
        raise ValueError("private key does not match api key index")

    monkeypatch.setattr(lighter, "SignerClient", broken)
    with pytest.raises(LighterApiError, match="could not be signed or sent: private key does not match"):
        cancel_orders(
            base_url="https://lighter.example", account_index=7, api_private_key="private",
            api_key_index=3, market_index=1, order_indexes=[1],
        )


def test_signing_key_is_per_account_and_read_only_tokens_cannot_sign(monkeypatch):
    for name in ("LIGHTER_ACCOUNT_7_API_PRIVATE_KEY", "LIGHTER_ACCOUNT_7_API_KEY_INDEX",
                 "LIGHTER_ACCOUNT_9_API_PRIVATE_KEY", "LIGHTER_ACCOUNT_9_API_KEY_INDEX"):
        monkeypatch.delenv(name, raising=False)
    base = {"default_account_index": 7, "api_private_key": "base", "api_key_index": 3}
    assert signing_key_for(7, **base) == ("base", 3)
    monkeypatch.setenv("LIGHTER_ACCOUNT_9_READ_ONLY_AUTH_TOKEN", "ro")
    with pytest.raises(AuthUnavailable, match="LIGHTER_ACCOUNT_9_API_PRIVATE_KEY"):
        signing_key_for(9, **base)
    monkeypatch.setenv("LIGHTER_ACCOUNT_9_API_PRIVATE_KEY", "sub")
    monkeypatch.setenv("LIGHTER_ACCOUNT_9_API_KEY_INDEX", "5")
    assert signing_key_for(9, **base) == ("sub", 5)
