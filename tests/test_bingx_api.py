import hashlib
import hmac
import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest import TestCase
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from active_trades.bingx_api import (
    BingxApiError,
    BingxAuthUnavailable,
    BingxRestClient,
    limit_orders_from_dicts,
    positions_from_dicts,
    stop_orders_from_dicts,
)


class TestBingxRestClient(TestCase):
    def setUp(self):
        self.client = BingxRestClient(
            api_key="test-api-key", api_secret="test-api-secret", timeout_seconds=8
        )

    @staticmethod
    def response(payload):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return response

    def test_position_read_signs_sorted_millisecond_params_and_sets_api_key_header(self):
        with (
            patch("active_trades.bingx_api.time.time", return_value=1_728_000_000.123),
            patch("active_trades.bingx_api.urlopen", return_value=self.response({"code": 0, "data": []})) as send,
        ):
            self.assertEqual(self.client.fetch_positions(), [])

        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("X-bx-apikey"), "test-api-key")
        self.assertEqual(request.get_header("X-source-key"), "BX-AI-SKILL")
        self.assertEqual(urlsplit(request.full_url).path, "/openApi/swap/v2/user/positions")
        canonical = "recvWindow=5000&timestamp=1728000000123"
        signature = hmac.new(b"test-api-secret", canonical.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(urlsplit(request.full_url).query, f"{canonical}&signature={signature}")
        self.assertEqual(send.call_args.kwargs["timeout"], 8)

    def test_open_orders_accepts_current_wrapped_response_and_preserves_large_ids(self):
        raw = {"orderId": 12345678901234567890, "type": "LIMIT"}
        with patch("active_trades.bingx_api.urlopen", return_value=self.response({"code": 0, "data": {"orders": [raw]}})) as send:
            self.assertEqual(self.client.fetch_active_orders(), [raw])
        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(urlsplit(request.full_url).path, "/openApi/swap/v2/trade/openOrders")
        self.assertNotIn("symbol", parse_qs(urlsplit(request.full_url).query))

    def test_open_orders_accepts_documented_array_response(self):
        with patch("active_trades.bingx_api.urlopen", return_value=self.response({"code": "0", "data": []})):
            self.assertEqual(self.client.fetch_active_orders(), [])

    def test_network_fallback_refreshes_timestamp_and_signature(self):
        with (
            patch("active_trades.bingx_api.time.time", side_effect=[1_728_000_000, 1_728_000_020]),
            patch("active_trades.bingx_api.urlopen", side_effect=[URLError("unreachable"), self.response({"code": 0, "data": []})]) as send,
        ):
            self.assertEqual(self.client.fetch_positions(), [])
        first, second = (call.args[0] for call in send.call_args_list)
        self.assertEqual(urlsplit(first.full_url).netloc, "open-api.bingx.com")
        self.assertEqual(urlsplit(second.full_url).netloc, "open-api.bingx.pro")
        first_query, second_query = (parse_qs(urlsplit(request.full_url).query) for request in (first, second))
        self.assertEqual(second_query["timestamp"], ["1728000020000"])
        self.assertNotEqual(first_query["signature"], second_query["signature"])

    def test_custom_base_url_does_not_retry_on_an_unrelated_domain(self):
        client = BingxRestClient(api_key="key", api_secret="secret", base_url="https://example.test")
        with patch("active_trades.bingx_api.urlopen", side_effect=URLError("unreachable")) as send:
            with self.assertRaises(BingxApiError):
                client.fetch_positions()
        self.assertEqual(send.call_count, 1)

    def test_business_and_http_errors_never_use_fallback(self):
        with patch("active_trades.bingx_api.urlopen", return_value=self.response({"code": 100410})) as send:
            with self.assertRaises(BingxApiError):
                self.client.fetch_positions()
        self.assertEqual(send.call_count, 1)
        with patch("active_trades.bingx_api.urlopen", side_effect=HTTPError("https://example.test", 500, "", {}, None)) as send:
            with self.assertRaises(BingxApiError):
                self.client.fetch_positions()
        self.assertEqual(send.call_count, 1)

    def test_missing_either_credential_never_makes_a_request(self):
        for api_key, api_secret in ((None, "secret"), ("key", None), ("", "secret")):
            with self.subTest(api_key=api_key, api_secret=api_secret):
                client = BingxRestClient(api_key=api_key, api_secret=api_secret)
                with patch("active_trades.bingx_api.urlopen") as send:
                    with self.assertRaises(BingxAuthUnavailable):
                        client.fetch_positions()
                send.assert_not_called()

    def test_auth_business_errors_are_safe_even_if_upstream_echoes_credentials(self):
        secret_text = "test-api-key test-api-secret signature=private-signature"
        for code in (100001, 100004, 100412, 100413, 100419):
            with self.subTest(code=code):
                with patch("active_trades.bingx_api.urlopen", return_value=self.response({"code": code, "msg": secret_text})):
                    with self.assertRaises(BingxAuthUnavailable) as caught:
                        self.client.fetch_positions()
                self.assertIn(str(code), str(caught.exception))
                for secret in secret_text.split():
                    self.assertNotIn(secret, str(caught.exception))

    def test_non_auth_errors_and_malformed_responses_are_rejected(self):
        for payload in (
            {"code": 100410, "msg": "private-signature"},
            {"code": 100421},
            {"code": 999999},
            {"data": []},
            {"code": "invalid", "data": []},
            {"code": 0, "data": None},
            {"code": 0, "data": [None]},
            {"code": 0, "data": {"positions": {}}},
            [],
        ):
            with self.subTest(payload=payload):
                with patch("active_trades.bingx_api.urlopen", return_value=self.response(payload)):
                    with self.assertRaises(BingxApiError) as caught:
                        self.client.fetch_positions()
                self.assertNotIn("private-signature", str(caught.exception))

    def test_transport_errors_never_expose_signed_url_or_error_body(self):
        private_url = "https://example.test?signature=private-signature"
        for exception in (
            URLError(f"failed {private_url} test-api-key test-api-secret"),
            HTTPError(private_url, 500, "private-signature", {}, io.BytesIO(b"test-api-secret")),
            TimeoutError("test-api-secret"),
            ValueError("Invalid header value test-api-key"),
        ):
            with self.subTest(exception=type(exception).__name__):
                with patch("active_trades.bingx_api.urlopen", side_effect=exception):
                    with self.assertRaises(BingxApiError) as caught:
                        self.client.fetch_positions()
                for secret in ("private-signature", "test-api-key", "test-api-secret"):
                    self.assertNotIn(secret, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)

    def test_http_auth_errors_are_distinguishable(self):
        for status in (401, 403):
            with self.subTest(status=status), patch(
                "active_trades.bingx_api.urlopen", side_effect=HTTPError("https://example.test", status, "", {}, None)
            ):
                with self.assertRaises(BingxAuthUnavailable):
                    self.client.fetch_positions()

    def test_invalid_json_is_reported_without_response_data(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"not JSON test-api-secret"
        with patch("active_trades.bingx_api.urlopen", return_value=response):
            with self.assertRaisesRegex(BingxApiError, "invalid JSON") as caught:
                self.client.fetch_positions()
        self.assertNotIn("test-api-secret", str(caught.exception))


class TestBingxParsing(TestCase):
    def setUp(self):
        self.position = {
            "symbol": "BTC-USDT", "positionId": "12345678901234567890", "positionSide": "SHORT",
            "positionAmt": "0.125", "avgPrice": "64000.25", "unrealizedProfit": "-10.5",
            "positionValue": "8000.03125", "createTime": 1_728_000_000_123,
        }
        self.limit = {
            "symbol": "BTC-USDT", "orderId": 12345678901234567890, "type": "LIMIT", "status": "PARTIALLY_FILLED",
            "side": "SELL", "positionSide": "LONG", "price": "65000.25", "origQty": "1.25",
            "executedQty": "0.5", "time": 1_728_000_000_123, "updateTime": 1_728_001_000_123,
        }
        self.stop = {
            "symbol": "BTC-USDT", "type": "STOP_MARKET", "status": "NEW", "side": "BUY",
            "positionSide": "SHORT", "stopPrice": "66000", "origQty": "0.125", "executedQty": "0",
            "positionID": "12345678901234567890", "reduceOnly": "false",
        }

    def test_position_uses_explicit_hedge_side_preserves_pnl_raw_and_identity(self):
        position = positions_from_dicts([self.position])[0]
        self.assertEqual(position.account_index, 0)
        self.assertEqual(position.exchange, "bingx")
        self.assertEqual(position.market_id, "BTC-USDT")
        self.assertEqual(position.side, "short")
        self.assertEqual(position.size, Decimal("0.125"))
        self.assertEqual(position.avg_entry_price, Decimal("64000.25"))
        self.assertEqual(position.unrealized_pnl, Decimal("-10.5"))
        self.assertEqual(position.position_value, Decimal("8000.03125"))
        self.assertEqual(position.position_id, "12345678901234567890")
        self.assertIs(position.raw, self.position)
        self.assertIn("bingx", position.key)

    def test_position_one_way_side_uses_signed_amount(self):
        long, short = positions_from_dicts([
            {**self.position, "positionSide": "BOTH", "positionAmt": "2"},
            {**self.position, "positionSide": "BOTH", "positionAmt": "-2"},
        ], account_index=3)
        self.assertEqual((long.side, short.side), ("long", "short"))
        self.assertEqual(short.size, Decimal("2"))
        self.assertEqual(short.account_index, 3)

    def test_positions_reject_empty_non_finite_or_invalid_values(self):
        invalid = [None, {}, {**self.position, "symbol": ""}, {**self.position, "positionSide": "invalid"}]
        for field in ("positionAmt", "avgPrice"):
            invalid.extend({**self.position, field: value} for value in (None, "bad", "NaN", "Infinity", "0"))
        invalid.append({**self.position, "avgPrice": "-1"})
        self.assertEqual(positions_from_dicts(invalid), [])

    def test_positions_preserve_zero_pnl_and_reject_non_finite_pnl(self):
        for value in (0, "0", "0.00"):
            with self.subTest(value=value):
                self.assertEqual(positions_from_dicts([{**self.position, "unrealizedProfit": value}])[0].unrealized_pnl, Decimal("0"))
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                self.assertIsNone(positions_from_dicts([{**self.position, "unrealizedProfit": value}])[0].unrealized_pnl)

    def test_positions_parse_realised_profit_counter_without_zero_fallback(self):
        for value in (0, "0", "0.00", "123.456789", "-123.456789"):
            with self.subTest(value=value):
                position = positions_from_dicts([{**self.position, "realisedProfit": value}])[0]
                self.assertEqual(position.realized_pnl, Decimal(str(value)))

    def test_positions_missing_or_invalid_realised_profit_is_unknown(self):
        self.assertIsNone(positions_from_dicts([self.position])[0].realized_pnl)
        for value in (None, "", "invalid", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                position = positions_from_dicts([{**self.position, "realisedProfit": value}])[0]
                self.assertIsNone(position.realized_pnl)

    def test_limit_preserves_remaining_amount_creation_time_and_large_id(self):
        order = limit_orders_from_dicts([self.limit])[0]
        self.assertEqual(order.exchange, "bingx")
        self.assertEqual(order.market_id, "BTC-USDT")
        self.assertEqual(order.symbol, "BTC-USDT")
        self.assertEqual(order.side, "sell")
        self.assertEqual(order.price, Decimal("65000.25"))
        self.assertEqual(order.remaining_base_amount, Decimal("0.75"))
        self.assertEqual(order.created_at, datetime.fromtimestamp(1_728_000_000, UTC))
        self.assertEqual(order.order_id, "12345678901234567890")
        self.assertTrue(order.reduce_only)

    def test_limit_keeps_normal_limits_and_excludes_other_types_and_inactive_orders(self):
        invalid = [{**self.limit, "type": value} for value in (
            "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRIGGER_LIMIT", "TRAILING_STOP_MARKET",
        )]
        invalid.extend({**self.limit, "status": value} for value in ("FILLED", "CANCELED", "EXPIRED", "unknown"))
        self.assertEqual(limit_orders_from_dicts(invalid), [])
        self.assertEqual(len(limit_orders_from_dicts([{**self.limit, "status": value} for value in ("NEW", "PARTIALLY_FILLED", "PENDING", "")])), 4)

    def test_limit_rejects_filled_negative_or_malformed_amounts(self):
        invalid = [None, {}, {**self.limit, "executedQty": "1.25"}, {**self.limit, "executedQty": "2"}]
        for field in ("price", "origQty"):
            invalid.extend({**self.limit, field: value} for value in (None, "bad", "NaN", "Infinity", "0", "-1"))
        invalid.extend({**self.limit, "executedQty": value} for value in ("bad", "NaN", "-1", {}))
        self.assertEqual(limit_orders_from_dicts(invalid), [])

    def test_limit_supports_missing_fill_metadata_without_using_update_time_as_creation(self):
        raw = {**self.limit, "origQty": None, "quantity": "3", "executedQty": None, "time": None, "side": "BUY", "positionSide": "BOTH", "reduceOnly": "false"}
        order = limit_orders_from_dicts([raw])[0]
        self.assertEqual(order.remaining_base_amount, Decimal("3"))
        self.assertIsNone(order.created_at)
        self.assertFalse(order.reduce_only)

    def test_limit_parses_attached_stop_loss_without_counting_filled_entry_size(self):
        raw = {
            **self.limit,
            "side": "BUY",
            "positionSide": "LONG",
            "stopLoss": {"type": "STOP_MARKET", "stopPrice": 64000.25, "quantity": 0},
        }

        order = limit_orders_from_dicts([raw])[0]

        self.assertEqual(order.remaining_base_amount, Decimal("0.75"))
        self.assertEqual(order.stop_loss, Decimal("64000.25"))
        self.assertIsNone(order.stop_loss_quantity)
        self.assertFalse(order.reduce_only)
        self.assertIs(order.raw, raw)

    def test_limit_parses_json_attachment_and_retains_explicit_stop_quantity(self):
        attachment = {"type": "STOP", "stopPrice": "64000.25", "price": "63900", "quantity": "0.25"}
        order = limit_orders_from_dicts([{**self.limit, "stopLoss": json.dumps(attachment)}])[0]

        self.assertEqual(order.stop_loss, Decimal("64000.25"))
        self.assertEqual(order.stop_loss_quantity, Decimal("0.25"))
        self.assertEqual(order.remaining_base_amount, Decimal("0.75"))

    def test_limit_attachment_without_quantity_follows_full_entry_size(self):
        order = limit_orders_from_dicts([
            {**self.limit, "stopLoss": {"type": "STOP_MARKET", "stopPrice": "66000"}}
        ])[0]

        self.assertEqual(order.stop_loss, Decimal("66000"))
        self.assertIsNone(order.stop_loss_quantity)

    def test_limit_ignores_empty_invalid_and_take_profit_attachments(self):
        attachments = [
            None, "", "not json", "[]", "null", [], {},
            {"type": "", "quantity": 0, "stopPrice": 0, "price": 0, "workingType": ""},
            {"type": "TAKE_PROFIT_MARKET", "stopPrice": "64000"},
            {"stopPrice": "64000"},
        ]
        attachments.extend(
            {"type": "STOP_MARKET", "stopPrice": value}
            for value in (None, "", "bad", "NaN", "Infinity", "-Infinity", "0", "-1", {})
        )
        attachments.extend(
            {"type": "STOP_MARKET", "stopPrice": "64000", "quantity": value}
            for value in (None, "", "bad", "NaN", "Infinity", "-Infinity", "-1", {})
        )
        for attachment in attachments:
            with self.subTest(attachment=attachment):
                order = limit_orders_from_dicts([{**self.limit, "stopLoss": attachment}])[0]
                self.assertIsNone(order.stop_loss)
                self.assertIsNone(order.stop_loss_quantity)

    def test_stop_market_and_stop_limit_map_to_existing_stop_types(self):
        market, limit = stop_orders_from_dicts([self.stop, {**self.stop, "type": "STOP"}])
        self.assertEqual((market.order_type, limit.order_type), ("stop-loss", "stop-loss-limit"))
        self.assertEqual(market.trigger_price, Decimal("66000"))
        self.assertEqual(market.remaining_base_amount, Decimal("0.125"))
        self.assertEqual(market.status, "open")
        self.assertTrue(market.reduce_only)
        self.assertEqual(market.raw["positionID"], "12345678901234567890")

    def test_journal_can_include_take_profit_market_and_limit_triggers(self):
        market, limit = stop_orders_from_dicts([
            {**self.stop, "type": "TAKE_PROFIT_MARKET", "origQty": "0", "closePosition": "true"},
            {**self.stop, "type": "TAKE_PROFIT"},
        ], include_take_profit=True)
        self.assertEqual((market.order_type, limit.order_type), ("take-profit", "take-profit-limit"))
        self.assertIsNone(market.remaining_base_amount)
        self.assertTrue(market.reduce_only)
        self.assertEqual(market.trigger_price, Decimal("66000"))

    def test_journal_tp_parser_still_rejects_inactive_and_invalid_orders(self):
        raw = {**self.stop, "type": "TAKE_PROFIT_MARKET"}
        invalid = [{**raw, "status": "FILLED"}, {**raw, "stopPrice": "NaN"}, {**raw, "origQty": "0"}]
        self.assertEqual(stop_orders_from_dicts(invalid, include_take_profit=True), [])

    def test_stop_classifies_only_closing_hedge_direction_as_reducing(self):
        cases = [("LONG", "SELL", True), ("SHORT", "BUY", True), ("LONG", "BUY", False), ("SHORT", "SELL", False)]
        for position_side, side, expected in cases:
            with self.subTest(position_side=position_side, side=side):
                order = stop_orders_from_dicts([{**self.stop, "positionSide": position_side, "side": side}])[0]
                self.assertEqual(order.reduce_only, expected)

    def test_one_way_stop_requires_reduce_only_or_close_position(self):
        for value, expected in (("false", False), (False, False), ("true", True), (True, True)):
            with self.subTest(value=value):
                order = stop_orders_from_dicts([{**self.stop, "positionSide": "BOTH", "reduceOnly": value}])[0]
                self.assertEqual(order.reduce_only, expected)
        order = stop_orders_from_dicts([{**self.stop, "positionSide": "BOTH", "closePosition": "true"}])[0]
        self.assertTrue(order.reduce_only)
        self.assertIsNone(order.remaining_base_amount)

    def test_whole_position_stop_accepts_zero_quantity_from_live_response(self):
        order = stop_orders_from_dicts([{**self.stop, "origQty": "0", "closePosition": "true", "reduceOnly": True}])[0]
        self.assertTrue(order.reduce_only)
        self.assertIsNone(order.remaining_base_amount)
        self.assertEqual(order.raw["positionID"], self.position["positionId"])

    def test_stops_exclude_tp_open_limits_inactive_and_invalid_triggers(self):
        invalid = [None, {}, {**self.stop, "origQty": "0"}]
        invalid.extend({**self.stop, "type": value} for value in ("LIMIT", "TAKE_PROFIT_MARKET", "TAKE_PROFIT", "TRAILING_TP_SL"))
        invalid.extend({**self.stop, "status": value} for value in ("FILLED", "CANCELED", "EXPIRED"))
        invalid.extend({**self.stop, "stopPrice": value} for value in (None, "bad", "NaN", "Infinity", "0", "-1"))
        self.assertEqual(stop_orders_from_dicts(invalid), [])

    def test_invalid_timestamps_do_not_crash(self):
        for value in (None, "bad", "NaN", "-1", 10 ** 500):
            with self.subTest(value=value):
                self.assertIsNone(limit_orders_from_dicts([{**self.limit, "time": value}])[0].created_at)


class TestBingxCancelOrder(TestCase):
    def setUp(self):
        self.client = BingxRestClient(api_key="test-api-key", api_secret="test-api-secret")

    def test_cancel_sends_signed_delete_for_one_symbol_and_order(self):
        with (
            patch("active_trades.bingx_api.time.time", return_value=1_728_000_000.123),
            patch("active_trades.bingx_api.urlopen", return_value=TestBingxRestClient.response({"code": 0, "data": {}})) as send,
        ):
            self.client.cancel_order(symbol="BTC-USDT", order_id="12345678901234567890")

        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(urlsplit(request.full_url).path, "/openApi/swap/v2/trade/order")
        canonical = "orderId=12345678901234567890&recvWindow=5000&symbol=BTC-USDT&timestamp=1728000000123"
        signature = hmac.new(b"test-api-secret", canonical.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(urlsplit(request.full_url).query, f"{canonical}&signature={signature}")

    def test_read_only_key_gets_a_trade_permission_message(self):
        with patch("active_trades.bingx_api.urlopen", return_value=TestBingxRestClient.response({"code": 100004})):
            with self.assertRaisesRegex(BingxAuthUnavailable, "cannot trade; enable perpetual futures trading"):
                self.client.cancel_order(symbol="BTC-USDT", order_id="1")

    def test_cancel_of_a_missing_order_says_it_may_have_filled(self):
        with patch("active_trades.bingx_api.urlopen", return_value=TestBingxRestClient.response({"code": 109421})):
            with self.assertRaisesRegex(BingxApiError, "may have filled or been cancelled"):
                self.client.cancel_order(symbol="BTC-USDT", order_id="1")

    def test_rejected_cancel_is_not_described_as_a_read(self):
        with patch("active_trades.bingx_api.urlopen", return_value=TestBingxRestClient.response({"code": 101204})):
            with self.assertRaisesRegex(BingxApiError, "the request was rejected"):
                self.client.cancel_order(symbol="BTC-USDT", order_id="1")
