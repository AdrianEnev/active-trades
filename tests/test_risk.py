from datetime import UTC, datetime
from decimal import Decimal
from unittest import TestCase

from active_trades.models import Position, StopOrder, Trade
from active_trades.risk import find_protective_stop, infer_opened_at_from_trades, risk_usd


class TestRisk(TestCase):
    def test_long_risk_uses_highest_protective_sell_stop_below_entry(self):
        position = Position(
            account_index=1,
            market_id=10,
            symbol="BTC",
            side="long",
            size=Decimal("0.5"),
            avg_entry_price=Decimal("100"),
        )
        orders = [
            StopOrder(1, 10, "sell", Decimal("90"), reduce_only=True, status="open", order_type="stop-loss"),
            StopOrder(1, 10, "sell", Decimal("95"), reduce_only=True, status="open", order_type="stop-loss"),
            StopOrder(1, 10, "buy", Decimal("80"), reduce_only=True, status="open", order_type="stop-loss"),
        ]

        stop, state, note = find_protective_stop(position, orders)

        self.assertEqual(stop.trigger_price, Decimal("95"))
        self.assertEqual(state, "at-risk")
        self.assertEqual(note, "")
        self.assertEqual(risk_usd(position, stop), Decimal("2.5"))

    def test_short_risk_uses_lowest_protective_buy_stop_above_entry(self):
        position = Position(
            account_index=1,
            market_id=10,
            symbol="ETH",
            side="short",
            size=Decimal("2"),
            avg_entry_price=Decimal("100"),
        )
        orders = [
            StopOrder(1, 10, "buy", Decimal("110"), reduce_only=True, status="open", order_type="stop-loss"),
            StopOrder(1, 10, "buy", Decimal("105"), reduce_only=True, status="pending", order_type="stop-loss"),
        ]

        stop, state, note = find_protective_stop(position, orders)

        self.assertEqual(stop.trigger_price, Decimal("105"))
        self.assertEqual(state, "at-risk")
        self.assertEqual(note, "")
        self.assertEqual(risk_usd(position, stop), Decimal("10"))

    def test_stop_beyond_breakeven_has_zero_risk(self):
        position = Position(
            account_index=1,
            market_id=10,
            symbol="BTC",
            side="short",
            size=Decimal("0.25"),
            avg_entry_price=Decimal("100"),
        )
        orders = [
            StopOrder(1, 10, "buy", Decimal("90"), reduce_only=True, status="pending", order_type="stop-loss-limit"),
        ]

        stop, state, note = find_protective_stop(position, orders)

        self.assertEqual(stop.trigger_price, Decimal("90"))
        self.assertEqual(state, "locked")
        self.assertEqual(note, "SL beyond breakeven")
        self.assertEqual(risk_usd(position, stop), Decimal("0"))

    def test_infer_opened_at_from_trades_walks_back_to_side_flip(self):
        opened_at = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
        position = Position(
            account_index=7,
            market_id=1,
            symbol="BTC",
            side="long",
            size=Decimal("3"),
            avg_entry_price=Decimal("100"),
        )
        trades = [
            Trade(7, 1, Decimal("1"), Decimal("105"), datetime(2026, 9, 11, 12, 0, tzinfo=UTC), Decimal("1")),
            Trade(7, 1, Decimal("1"), Decimal("103"), datetime(2026, 9, 11, 11, 0, tzinfo=UTC), Decimal("-1")),
            Trade(7, 1, Decimal("3"), Decimal("100"), opened_at, Decimal("3")),
        ]

        self.assertEqual(infer_opened_at_from_trades(position, trades), opened_at)
