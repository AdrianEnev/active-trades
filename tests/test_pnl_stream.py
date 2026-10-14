from __future__ import annotations

import json
import queue
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import TestCase

from active_trades.pnl_stream import PnlStream


def position(market_id=0, *, size="2", sign=1, pnl="10", value="210"):
    return {
        "market_id": market_id,
        "symbol": "BTC",
        "position": size,
        "sign": sign,
        "avg_entry_price": "100",
        "position_value": value,
        "unrealized_pnl": pnl,
    }


def account_message(positions, *, subscribed=False, account_index=1):
    return {
        "type": ("subscribed" if subscribed else "update") + "/account_all_positions",
        "channel": f"account_all_positions:{account_index}",
        "positions": positions,
    }


class FakeConnection:
    def __init__(self):
        self.sent = []
        self.messages = queue.Queue()
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self, *, timeout):
        if self.closed.is_set():
            raise RuntimeError("closed")
        try:
            return self.messages.get(timeout=min(timeout, 0.01))
        except queue.Empty as exc:
            raise TimeoutError from exc

    def close(self):
        self.closed.set()


class TestPnlStream(TestCase):
    def make_stream(self):
        stream = PnlStream(base_url="http://test.invalid")
        stream.set_accounts([1])
        return stream

    def test_snapshot_deltas_zero_removal_and_reconnection_snapshot(self):
        stream = self.make_stream()
        stream._handle_message(account_message({"0": position(), "1": position(1)}, subscribed=True))
        stream._handle_message(account_message({"0": position(pnl="12")}))
        self.assertEqual(len(stream.latest_positions()[1].positions), 2)
        self.assertEqual(stream.latest_positions()[1].positions[0].unrealized_pnl, Decimal("12"))
        stream._handle_message(account_message({"0": position(size="0")}))
        self.assertEqual([p.market_id for p in stream.latest_positions()[1].positions], [1])
        stream._handle_message(account_message({}, subscribed=True))
        self.assertEqual(stream.latest_positions()[1].positions, ())

    def test_marks_preserve_canonical_entry_precision_and_short_direction(self):
        stream = self.make_stream()
        stream._handle_message(account_message({
            "0": position(pnl="10.123456"),
            "1": position(1, sign=-1, pnl="-10.654321"),
        }, subscribed=True))
        before = stream.latest_positions()[1]
        stream._handle_message({
            "type": "update/market_stats", "channel": "market_stats:all",
            "market_stats": {"0": {"market_id": 0, "mark_price": "106"},
                             "1": {"market_id": 1, "mark_price": "106"}},
        })
        after = stream.latest_positions()[1]
        self.assertEqual(after.positions[0].unrealized_pnl, Decimal("12.123456"))
        self.assertEqual(after.positions[1].unrealized_pnl, Decimal("-12.654321"))
        self.assertGreater(after.received_at, before.received_at)
        self.assertEqual(after.positions_received_at, before.positions_received_at)
        self.assertEqual(after.positions[0].position_value, Decimal("212"))

    def test_newer_canonical_position_wins_over_older_mark(self):
        stream = self.make_stream()
        stream._handle_message({
            "type": "update/market_stats", "market_stats": {"market_id": 0, "mark_price": "110"},
        })
        stream._handle_message(account_message({"0": position(pnl="3.21")}, subscribed=True))
        self.assertEqual(stream.latest_positions()[1].positions[0].unrealized_pnl, Decimal("3.21"))

    def test_missing_canonical_pnl_falls_back_to_entry_and_invalid_marks_ignored(self):
        stream = self.make_stream()
        raw_position = position(pnl=None, value=None)
        stream._handle_message(account_message({"0": raw_position}, subscribed=True))
        stream._handle_message({"type": "update/market_stats", "market_stats": {"market_id": 0, "mark_price": "110"}})
        self.assertEqual(stream.latest_positions()[1].positions[0].unrealized_pnl, Decimal("20"))
        for bad_mark in ["NaN", "Infinity", "-1", "bad"]:
            stream._handle_message({"type": "update/market_stats", "market_stats": {"market_id": 0, "mark_price": bad_mark}})
        self.assertEqual(stream.latest_positions()[1].positions[0].unrealized_pnl, Decimal("20"))

    def test_rest_seed_keeps_newer_deltas_and_zero_removals(self):
        stream = self.make_stream()
        stream._handle_message(account_message({"0": position(), "1": position(1)}, subscribed=True))
        initial = stream.latest_positions()[1]
        stream._handle_message(account_message({"0": position(pnl="22"), "1": position(1, size="0")}))
        stream.seed_positions(1, initial.positions, initial.positions_received_at)
        latest = stream.latest_positions()[1]
        self.assertEqual(len(latest.positions), 1)
        self.assertEqual(latest.positions[0].unrealized_pnl, Decimal("22"))
        next_time = latest.received_at + timedelta(seconds=1)
        stream.seed_positions(1, [], next_time)
        self.assertEqual(stream.latest_positions()[1].positions, ())

    def test_removed_accounts_and_unrequested_data_are_ignored(self):
        stream = self.make_stream()
        stream._handle_message(account_message({"0": position()}, subscribed=True))
        stream.set_accounts([2])
        self.assertFalse(stream._handle_message(account_message({"0": position()}, account_index=1)))
        self.assertEqual(stream.latest_positions(), {})
        stream.seed_positions(1, [], datetime.now(UTC))
        self.assertEqual(stream.latest_positions(), {})

    def test_authoritative_empty_subscription_wins_over_older_rest_seed(self):
        stream = self.make_stream()
        stream._handle_message(account_message({"0": position()}, subscribed=True))
        initial = stream.latest_positions()[1]
        stream._handle_message(account_message({}, subscribed=True))
        stream.seed_positions(1, initial.positions, initial.received_at)
        self.assertEqual(stream.latest_positions()[1].positions, ())

    def test_malformed_messages_keep_existing_positions(self):
        stream = self.make_stream()
        stream._handle_message(account_message({"0": position()}, subscribed=True))
        for malformed in ["not JSON", "[]", {}, {"type": "connected"},
                          account_message({"0": position(size="bad")}),
                          account_message({"0": position(size="Infinity")}),
                          account_message({"0": {**position(), "avg_entry_price": "NaN"}}),
                          account_message({"bad": {"market_id": "bad"}})]:
            stream._handle_message(malformed)
        self.assertEqual(len(stream.latest_positions()[1].positions), 1)

    def test_thread_subscribes_new_accounts_replies_to_ping_and_closes(self):
        connection = FakeConnection()
        options = {}

        def connect_fn(url, **kwargs):
            options.update(kwargs)
            options["url"] = url
            return connection

        stream = PnlStream(base_url="http://test.invalid", connect_fn=connect_fn)
        try:
            stream.start([1])
            self.wait_until(lambda: len(connection.sent) >= 2)
            self.assertIn({"type": "subscribe", "channel": "market_stats/all"}, connection.sent)
            self.assertIn({"type": "subscribe", "channel": "account_all_positions/1"}, connection.sent)
            stream.set_accounts([1, 2])
            connection.messages.put(json.dumps({"type": "ping"}))
            connection.messages.put(json.dumps(account_message({"0": position()}, subscribed=True)))
            self.wait_until(lambda: stream.notice is None and any(message.get("channel") == "account_all_positions/2" for message in connection.sent))
            self.assertIn({"type": "pong"}, connection.sent)
            self.assertEqual(options["url"], "ws://test.invalid/stream?readonly=true")
            self.assertEqual(options["ping_interval"], 20)
        finally:
            stream.close()
        self.assertTrue(connection.closed.is_set())
        self.assertFalse(stream._thread.is_alive())

    def test_reconnect_notice_never_exposes_transport_error(self):
        attempted = threading.Event()

        def connect_fn(*_args, **_kwargs):
            attempted.set()
            raise RuntimeError("private token and server payload")

        stream = PnlStream(base_url="http://test.invalid", connect_fn=connect_fn)
        try:
            stream.start([1])
            self.assertTrue(attempted.wait(timeout=1))
            self.wait_until(lambda: stream.notice == "UPNL stream disconnected; retrying.")
            self.assertNotIn("token", stream.notice)
        finally:
            stream.close()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 1
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("Background stream did not reach expected state.")
            time.sleep(0.005)
