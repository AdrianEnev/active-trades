import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from active_trades.models import ActiveTrade, Position
from active_trades.profit_tracking import PositionProfitTracker


START = datetime(2026, 10, 15, tzinfo=UTC)


def trade(*, size="10", realized="0", risk="100", exchange="lighter", account=1, side="long", market=1):
    return ActiveTrade(
        row_number=1,
        position=Position(
            account_index=account,
            market_id=market,
            symbol="BTC",
            side=side,
            size=Decimal(size),
            avg_entry_price=Decimal("100"),
            realized_pnl=Decimal(realized) if realized is not None else None,
            exchange=exchange,
        ),
        opened_at=START,
        opened_at_source="state",
        stop_loss=Decimal("90"),
        risk_usd=Decimal(risk) if risk is not None else None,
    )


def observe(tracker, *trades, second=0, exchange="lighter", account=1):
    return tracker.observe_account(
        trades, exchange=exchange, account_index=account,
        observed_at=START + timedelta(seconds=second),
    )


def record(tracker, active_trade=None):
    return tracker.dump()["positions"][(active_trade or trade()).position.key]


def test_cumulative_partial_profits_use_original_risk_and_ignore_lifetime_counter():
    tracker = PositionProfitTracker()
    assert not observe(tracker, trade(realized="1000"))[0].profit_target_reached
    assert not observe(tracker, trade(size="8", realized="1030", risk="80"), second=1)[0].profit_target_reached
    assert observe(tracker, trade(size="6", realized="1050", risk="60"), second=2)[0].profit_target_reached
    assert record(tracker)["original_risk_usd"] == "100"
    assert record(tracker)["cumulative_partial_profit"] == "50"


def test_exactly_49_percent_does_not_qualify():
    tracker = PositionProfitTracker()
    observe(tracker, trade())
    assert not observe(tracker, trade(size="5", realized="49"), second=1)[0].profit_target_reached
    assert observe(tracker, trade(size="4", realized="49.01"), second=2)[0].profit_target_reached


def test_stop_loss_and_risk_changes_do_not_change_original_denominator():
    tracker = PositionProfitTracker()
    observe(tracker, trade())
    observe(tracker, replace(trade(risk="2"), stop_loss=Decimal("99.8")), second=1)
    assert not observe(tracker, trade(size="8", realized="30", risk="1.6"), second=2)[0].profit_target_reached
    assert record(tracker)["original_risk_usd"] == "100"
    assert observe(tracker, trade(size="6", realized="50", risk="1.2"), second=3)[0].profit_target_reached


def test_counter_changes_without_size_reduction_are_rebased():
    tracker = PositionProfitTracker()
    observe(tracker, trade(realized="0"))
    observe(tracker, trade(realized="1000"), second=1)
    observe(tracker, trade(size="12", realized="2000"), second=2)
    assert not observe(tracker, trade(size="11", realized="2001"), second=3)[0].profit_target_reached
    assert record(tracker)["cumulative_partial_profit"] == "1"


def test_partial_losses_reduce_cumulative_profit_even_with_negative_counters():
    tracker = PositionProfitTracker()
    observe(tracker, trade(realized="-500"))
    observe(tracker, trade(size="8", realized="-470"), second=1)
    observe(tracker, trade(size="6", realized="-490"), second=2)
    assert record(tracker)["cumulative_partial_profit"] == "10"
    assert observe(tracker, trade(size="4", realized="-450"), second=3)[0].profit_target_reached


def test_zero_realized_counter_is_known_and_zero_original_risk_stays_zero():
    tracker = PositionProfitTracker()
    observe(tracker, trade(realized="0", risk="0"))
    assert not observe(tracker, trade(size="9", realized="0", risk="100"), second=1)[0].profit_target_reached
    assert record(tracker)["partial_close_seen"] is True
    assert observe(tracker, trade(size="8", realized="0.01", risk="100"), second=2)[0].profit_target_reached
    assert record(tracker)["original_risk_usd"] == "0"


def test_missing_realized_values_break_counter_chain_and_never_use_upnl():
    tracker = PositionProfitTracker()
    unknown = trade(realized=None)
    observe(tracker, unknown)
    huge_upnl = replace(trade(size="9", realized="1000"), position=replace(
        trade(size="9", realized="1000").position, unrealized_pnl=Decimal("999999"),
    ))
    assert not observe(tracker, huge_upnl, second=1)[0].profit_target_reached
    observe(tracker, trade(size="8", realized=None), second=2)
    assert not observe(tracker, trade(size="7", realized="2000"), second=3)[0].profit_target_reached
    assert record(tracker)["cumulative_partial_profit"] == "0"
    assert not observe(tracker, trade(size="6", realized="2050"), second=4)[0].profit_target_reached
    assert record(tracker)["profit_data_complete"] is False


def test_original_risk_waits_for_first_known_valid_value():
    tracker = PositionProfitTracker()
    observe(tracker, trade(risk=None))
    observe(tracker, trade(risk="NaN"), second=1)
    observe(tracker, trade(risk="-10"), second=2)
    observe(tracker, trade(risk="100"), second=3)
    assert not observe(tracker, trade(size="8", realized="30", risk="1"), second=4)[0].profit_target_reached
    assert record(tracker)["original_risk_usd"] == "100"


def test_missing_original_risk_never_uses_risk_from_already_reduced_position():
    tracker = PositionProfitTracker()
    observe(tracker, trade(risk=None))
    assert not observe(tracker, trade(size="8", realized="50", risk="80"), second=1)[0].profit_target_reached
    assert record(tracker)["original_risk_usd"] is None
    observe(tracker, trade(size="8", realized="50", risk="1"), second=2)
    assert not observe(tracker, trade(size="6", realized="100", risk="60"), second=3)[0].profit_target_reached
    assert record(tracker)["original_risk_usd"] is None


def test_unknown_partial_prevents_later_known_subtotal_from_qualifying_after_restart():
    tracker = PositionProfitTracker()
    observe(tracker, trade())
    observe(tracker, trade(size="8", realized=None), second=1)
    assert record(tracker)["partial_close_seen"] is True
    restored = PositionProfitTracker()
    restored.restore(tracker.dump())
    observe(restored, trade(size="8", realized="-1000"), second=2)
    assert not observe(restored, trade(size="6", realized="0"), second=3)[0].profit_target_reached
    assert record(restored)["cumulative_partial_profit"] == "1000"
    assert record(restored)["profit_data_complete"] is False


def test_qualifying_color_stays_latched_after_losses_and_position_adds():
    tracker = PositionProfitTracker()
    observe(tracker, trade())
    assert observe(tracker, trade(size="8", realized="50"), second=1)[0].profit_target_reached
    assert observe(tracker, trade(size="6", realized="-100"), second=2)[0].profit_target_reached
    assert observe(tracker, trade(size="12", realized="-200", risk="1000"), second=3)[0].profit_target_reached


def test_close_reset_is_scoped_to_exchange_and_account_and_reverse_starts_new_baseline():
    tracker = PositionProfitTracker()
    for exchange, account in (("lighter", 1), ("lighter", 2), ("bingx", 1)):
        observe(tracker, trade(exchange=exchange, account=account), exchange=exchange, account=account)
        observe(tracker, trade(size="8", realized="50", exchange=exchange, account=account),
                exchange=exchange, account=account, second=1)
    observe(tracker, second=2)
    assert len(tracker.dump()["positions"]) == 2
    assert not observe(tracker, trade(realized="5000"), second=3)[0].profit_target_reached
    short = trade(side="short", size="5", realized="6000")
    assert not observe(tracker, short, second=4)[0].profit_target_reached
    assert trade().position.key not in tracker.dump()["positions"]
    assert record(tracker, short)["cumulative_partial_profit"] == "0"


def test_restart_round_trip_preserves_original_risk_profit_and_sticky_flag():
    tracker = PositionProfitTracker()
    observe(tracker, trade(realized="1000"))
    observe(tracker, trade(size="8", realized="1030", risk="80"), second=1)
    restored = PositionProfitTracker()
    restored.restore(json.loads(json.dumps(tracker.dump())))
    assert restored.dump() == tracker.dump()
    assert observe(restored, trade(size="6", realized="1050", risk="60"), second=2)[0].profit_target_reached
    again = PositionProfitTracker()
    again.restore(restored.dump())
    assert observe(again, trade(size="5", realized="900", risk="50"), second=3)[0].profit_target_reached


@pytest.mark.parametrize("field,value", [
    ("last_size", "0"), ("last_size", "-1"), ("last_size", True),
    ("last_realized_pnl", "NaN"), ("last_realized_pnl", False),
    ("original_risk_usd", "-1"), ("original_risk_usd", "Infinity"),
    ("cumulative_partial_profit", "-Infinity"), ("cumulative_partial_profit", True),
    ("observed_at", "2026-10-15T00:00:00"), ("observed_at", "invalid"),
    ("observed_at", "0001-01-01T00:00:00+01:00"),
    ("account_index", True), ("exchange", ""),
    ("profit_target_reached", "true"), ("partial_close_seen", 1),
    ("profit_data_complete", "true"),
    ("cumulative_partial_profit", "100"),
])
def test_restore_skips_corrupt_entries(field, value):
    tracker = PositionProfitTracker()
    observe(tracker, trade())
    payload = tracker.dump()
    payload["positions"][trade().position.key][field] = value
    restored = PositionProfitTracker()
    restored.restore(payload)
    assert restored.dump()["positions"] == {}


@pytest.mark.parametrize("payload", [None, [], {"positions": []}, {"positions": {"bad": None}}, {"accounts": [None, {}]}])
def test_restore_tolerates_malformed_payloads(payload):
    tracker = PositionProfitTracker()
    tracker.restore(payload)
    assert tracker.dump() == {"positions": {}, "accounts": []}


def test_stale_observations_never_reapply_profits_delete_or_resurrect_positions():
    tracker = PositionProfitTracker()
    observe(tracker, trade())
    observe(tracker, trade(size="8", realized="50"), second=2)
    payload = tracker.dump()
    assert observe(tracker, trade(size="5", realized="1000"), second=1)[0].profit_target_reached
    observe(tracker, second=1)
    observe(tracker, trade(size="5", realized="1000"), second=2)
    assert tracker.dump() == payload
    observe(tracker, second=3)
    assert not observe(tracker, trade(size="8", realized="1000"), second=2)[0].profit_target_reached
    assert tracker.dump()["positions"] == {}
    restored = PositionProfitTracker()
    restored.restore(tracker.dump())
    observe(restored, trade(realized="5000"), second=2)
    assert restored.dump()["positions"] == {}
    assert not observe(restored, trade(realized="5000"), second=4)[0].profit_target_reached


def test_concurrent_observations_and_dumps_do_not_double_count():
    tracker = PositionProfitTracker()
    observe(tracker, trade(size="100", realized="1000"))

    def update(second):
        observe(tracker, trade(size=str(100 - second), realized=str(1000 + 3 * second)), second=second)
        return json.loads(json.dumps(tracker.dump()))

    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(update, range(1, 21)))
    assert all(snapshot["positions"] for snapshot in snapshots)
    assert record(tracker)["last_size"] == "80"
    assert record(tracker)["cumulative_partial_profit"] == "60"
    assert record(tracker)["profit_target_reached"] is True
