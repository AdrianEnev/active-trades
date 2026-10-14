from datetime import UTC, datetime
from dataclasses import replace
from decimal import Decimal
from unittest import TestCase

from active_trades.models import AccountInfo, AccountSnapshot, ActiveTrade, LimitOrder, Position, StopOrder, TrackerSnapshot, Trade
from active_trades.risk import find_protective_stop, infer_opened_at_from_trades, limit_risk_total, position_risk_total, risk_usd


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


class TestRiskTotals(TestCase):
    def entry(self, **changes):
        return replace(LimitOrder(1, 10, "BTC", "buy", Decimal("100"), Decimal("1")), **changes)

    def stop(self, **changes):
        return replace(StopOrder(
            1, 10, "sell", Decimal("90"), remaining_base_amount=Decimal("1"),
            reduce_only=True, status="open", order_type="stop-loss",
        ), **changes)

    def trade(self, **changes):
        position = Position(1, 10, "BTC", "long", Decimal("1"), Decimal("100"))
        return replace(ActiveTrade(1, position, None, "unknown", Decimal("90"), Decimal("10"), "at-risk"), **changes)

    def snapshot(self, *, trades=(), entries=(), stops=(), **account_changes):
        account = AccountSnapshot(
            AccountInfo(1), tuple(trades), limit_orders=tuple(entries), stop_orders=tuple(stops),
        )
        return TrackerSnapshot(datetime(2026, 10, 14, tzinfo=UTC), (replace(account, **account_changes),))

    def test_position_risk_sums_accounts_without_offsetting_breakeven_stops(self):
        snapshot = self.snapshot(trades=(self.trade(), self.trade(risk_usd=Decimal("0"), risk_state="locked")))
        bingx_trade = self.trade(risk_usd=Decimal("12.34"))
        bingx_trade = replace(bingx_trade, position=replace(bingx_trade.position, exchange="bingx"))
        snapshot = replace(snapshot, accounts=(*snapshot.accounts, AccountSnapshot(
            AccountInfo(0, exchange="bingx"), (bingx_trade,),
        )))
        self.assertEqual(position_risk_total(snapshot), (Decimal("22.34"), False))

    def test_missing_or_ambiguous_position_risk_is_never_reported_as_zero(self):
        for unknown in (self.trade(risk_usd=None, risk_state="missing"), self.trade(risk_state="unknown")):
            with self.subTest(state=unknown.risk_state):
                self.assertEqual(position_risk_total(self.snapshot(trades=(unknown,))), (None, True))
                self.assertEqual(position_risk_total(self.snapshot(trades=(self.trade(), unknown))), (Decimal("10"), True))

    def test_sized_stop_must_cover_the_active_position(self):
        partial = self.stop(remaining_base_amount=Decimal("0.5"))
        snapshot = self.snapshot(trades=(self.trade(),), stops=(partial,))
        self.assertEqual(position_risk_total(snapshot), (None, True))
        covered = replace(snapshot.accounts[0], stop_orders=(self.stop(),))
        self.assertEqual(position_risk_total(replace(snapshot, accounts=(covered,))), (Decimal("10"), False))

    def test_current_positions_cannot_use_a_child_stop_waiting_for_its_entry(self):
        child = self.stop(raw={"trigger_status": "parent-order", "parent_order_id": 11})
        trade = self.trade()
        self.assertEqual(find_protective_stop(trade.position, [child])[:2], (None, "missing"))
        self.assertEqual(position_risk_total(self.snapshot(trades=(trade,), stops=(child,))), (None, True))
        self.assertEqual(limit_risk_total(self.snapshot(entries=(self.entry(order_id="11"),), stops=(child,))), (Decimal("10"), False))

    def test_shared_sized_stop_cannot_be_counted_for_each_separate_position(self):
        trades = tuple(
            replace(self.trade(), position=replace(self.trade().position, exchange="bingx", position_id=identity))
            for identity in ("first", "second")
        )
        stop = self.stop(exchange="bingx", raw={"positionSide": "LONG"})
        snapshot = self.snapshot(trades=trades, stops=(stop,))
        self.assertEqual(position_risk_total(snapshot), (None, True))
        covered = replace(snapshot.accounts[0], stop_orders=(replace(stop, remaining_base_amount=Decimal("2")),))
        self.assertEqual(position_risk_total(replace(snapshot, accounts=(covered,))), (Decimal("20"), False))

    def test_attached_stops_use_remaining_quantity_and_both_directions(self):
        long = self.entry(remaining_base_amount=Decimal("0.5"), stop_loss=Decimal("90"), raw={"initial_base_amount": "2"})
        short = self.entry(side="sell", price=Decimal("120"), remaining_base_amount=Decimal("2"), stop_loss=Decimal("130"))
        self.assertEqual(limit_risk_total(self.snapshot(entries=(long, short))), (Decimal("25"), False))

    def test_attached_stop_quantity_must_cover_the_whole_partially_filled_entry(self):
        entry = self.entry(
            exchange="bingx", remaining_base_amount=Decimal("0.5"), stop_loss=Decimal("90"),
            stop_loss_quantity=Decimal("1"), raw={"origQty": "2", "executedQty": "1.5"},
        )
        self.assertEqual(limit_risk_total(self.snapshot(entries=(entry,))), (None, True))
        self.assertEqual(limit_risk_total(self.snapshot(entries=(replace(entry, stop_loss_quantity=Decimal("2")),))), (Decimal("5"), False))

    def test_reducing_limits_do_not_add_entry_risk(self):
        entry = self.entry(reduce_only=True)
        self.assertEqual(limit_risk_total(self.snapshot(entries=(entry,))), (Decimal("0"), False))

    def test_linked_lighter_zero_size_child_covers_its_parent_entry(self):
        for entry, stop in (
            (self.entry(order_id="11"), self.stop(remaining_base_amount=Decimal("0"), raw={"parent_order_index": 11})),
            (self.entry(raw={"to_trigger_order_id_0": 22}), self.stop(order_id="22", remaining_base_amount=Decimal("0"))),
        ):
            with self.subTest(entry=entry):
                self.assertEqual(limit_risk_total(self.snapshot(entries=(entry,), stops=(stop,))), (Decimal("10"), False))

    def test_unrelated_or_inactive_stops_cannot_protect_pending_entries(self):
        entry = self.entry(order_id="11")
        for stop in (
            self.stop(account_index=2), self.stop(exchange="bingx"), self.stop(market_id=99),
            self.stop(side="buy"), self.stop(status="canceled"), self.stop(order_type="take-profit"),
            self.stop(reduce_only=False), self.stop(raw={"parent_order_id": 12}),
        ):
            with self.subTest(stop=stop):
                self.assertEqual(limit_risk_total(self.snapshot(entries=(entry,), stops=(stop,))), (None, True))

    def test_shared_stop_capacity_is_reserved_for_positions_and_all_pending_entries(self):
        entries = (
            self.entry(remaining_base_amount=Decimal("0.5")),
            self.entry(price=Decimal("110"), remaining_base_amount=Decimal("0.5")),
        )
        snapshot = self.snapshot(trades=(self.trade(),), entries=entries, stops=(self.stop(),))
        self.assertEqual(limit_risk_total(snapshot), (None, True))
        covered = replace(snapshot.accounts[0], stop_orders=(self.stop(remaining_base_amount=Decimal("2")),))
        self.assertEqual(limit_risk_total(replace(snapshot, accounts=(covered,))), (Decimal("15"), False))

    def test_shared_stop_capacity_tracks_live_position_growth_and_closure(self):
        entry = self.entry()
        snapshot = self.snapshot(entries=(entry,), stops=(self.stop(),))
        self.assertEqual(limit_risk_total(snapshot), (Decimal("10"), False))
        grown = replace(snapshot.accounts[0], trades=(self.trade(),))
        self.assertEqual(limit_risk_total(replace(snapshot, accounts=(grown,))), (None, True))
        self.assertEqual(limit_risk_total(snapshot), (Decimal("10"), False))

    def test_unknown_size_and_multiple_trigger_prices_are_not_assumed_to_cover_every_fill(self):
        for stops in (
            (self.stop(remaining_base_amount=None),), (self.stop(remaining_base_amount=Decimal("0")),),
            (self.stop(), self.stop(trigger_price=Decimal("95"))),
        ):
            with self.subTest(stops=stops):
                self.assertEqual(limit_risk_total(self.snapshot(entries=(self.entry(),), stops=stops)), (None, True))

    def test_stops_at_the_same_trigger_can_cover_the_combined_quantity(self):
        stops = (self.stop(order_id="1"), self.stop(order_id="2"))
        entries = (self.entry(remaining_base_amount=Decimal("2")),)
        self.assertEqual(limit_risk_total(self.snapshot(entries=entries, stops=stops)), (Decimal("20"), False))

    def test_standalone_stop_that_triggers_before_entry_is_not_protection(self):
        self.assertEqual(limit_risk_total(self.snapshot(entries=(self.entry(price=Decimal("85")),), stops=(self.stop(),))), (None, True))

    def test_bingx_close_position_stop_covers_all_future_same_leg_fills(self):
        entry = self.entry(exchange="bingx")
        close_all = self.stop(exchange="bingx", remaining_base_amount=None, raw={"closePosition": True, "positionSide": "LONG"})
        self.assertEqual(limit_risk_total(self.snapshot(entries=(entry,), stops=(close_all,))), (Decimal("10"), False))
        wrong_leg = replace(close_all, raw={"closePosition": True, "positionSide": "SHORT"})
        self.assertEqual(limit_risk_total(self.snapshot(entries=(entry,), stops=(wrong_leg,))), (None, True))

    def test_bingx_stop_for_another_isolated_position_is_not_reused(self):
        entry = self.entry(exchange="bingx", raw={"positionId": "expected"})
        stop = self.stop(exchange="bingx", raw={"positionSide": "LONG", "positionID": "other"})
        self.assertEqual(limit_risk_total(self.snapshot(entries=(entry,), stops=(stop,))), (None, True))

    def test_unbound_bingx_entry_cannot_borrow_a_stop_from_an_existing_position_id(self):
        entry = self.entry(exchange="bingx")
        stop = self.stop(exchange="bingx", remaining_base_amount=Decimal("10"), raw={"positionSide": "LONG", "positionID": "existing"})
        trade = replace(self.trade(), position=replace(self.trade().position, exchange="bingx", position_id="existing"))
        self.assertEqual(limit_risk_total(self.snapshot(trades=(trade,), entries=(entry,), stops=(stop,))), (None, True))

    def test_confirmed_empty_and_unavailable_lookups_are_distinct(self):
        empty = self.snapshot()
        self.assertEqual(position_risk_total(empty), (Decimal("0"), False))
        self.assertEqual(limit_risk_total(empty), (Decimal("0"), False))
        for state in ("unavailable", "auth-needed", "disabled"):
            with self.subTest(state=state):
                self.assertEqual(limit_risk_total(self.snapshot(orders_state=state)), (None, True))
                self.assertEqual(position_risk_total(self.snapshot(positions_state=state)), (None, True))
        incomplete = replace(empty, accounts_complete=False)
        self.assertEqual(position_risk_total(incomplete), (None, True))
        self.assertEqual(limit_risk_total(incomplete), (None, True))

    def test_attached_limit_risk_does_not_need_current_position_reads(self):
        snapshot = self.snapshot(entries=(self.entry(stop_loss=Decimal("90")),), positions_state="unavailable")
        self.assertEqual(limit_risk_total(snapshot), (Decimal("10"), False))
