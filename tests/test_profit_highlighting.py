import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from active_trades.tracker import ActiveTradesTracker
from test_tracker import FakeAuthProvider, FakeClient, bingx_position, bingx_stop, fake_bingx, make_settings


class MutableLighterClient(FakeClient):
    """Position snapshots with $100 initial risk and a cumulative exchange PNL counter."""

    def __init__(self, accounts=(1,)):
        self.positions = {
            index: {"size": "0.1", "realized": "500", "side": 1}
            for index in accounts
        }

    def set_position(self, *, account=1, size, realized, side=1):
        self.positions[account] = {"size": size, "realized": realized, "side": side}

    def fetch_account_by_index(self, account_index, *, active_only=True):
        account = super().fetch_account_by_index(account_index, active_only=active_only)
        current = self.positions[account_index]
        account["positions"] = [{
            **account["positions"][0],
            "position": current["size"],
            "sign": current["side"],
            "realized_pnl": current["realized"],
        }] if Decimal(current["size"]) else []
        return account


def make_tracker(path: Path, client: MutableLighterClient, *, accounts=(1,), bingx=None):
    return ActiveTradesTracker(
        make_settings(path, account_index=accounts[0], explicit_account_indexes=accounts),
        client=client,
        auth_provider=FakeAuthProvider(),
        bingx_client=bingx,
    )


def position_trade(snapshot, *, exchange="lighter", account=1):
    section = next(
        section for section in snapshot.accounts
        if section.account.exchange == exchange and section.account.index == account
    )
    return section.trades[0]


def live_update(snapshot, *, size, realized, seconds=1):
    old = position_trade(snapshot).position
    positions = () if Decimal(size) == 0 else (
        replace(old, size=Decimal(size), realized_pnl=Decimal(realized)),
    )
    return {1: SimpleNamespace(
        positions=positions,
        received_at=snapshot.pnl_collected_at + timedelta(seconds=seconds),
    )}


def test_rest_partial_profits_accumulate_against_original_risk_and_strict_threshold(tmp_path):
    client = MutableLighterClient()
    tracker = make_tracker(tmp_path / "state.json", client)
    first = position_trade(tracker.collect_snapshot())
    assert first.risk_usd == Decimal("100")
    assert first.position.realized_pnl == Decimal("500")
    assert not first.profit_target_reached

    # The exchange reports a running total; $500 predates this monitoring session.
    for size, realized, expected in (
        ("0.08", "530", False),
        ("0.06", "549", False),
        ("0.04", "550", True),
    ):
        client.set_position(size=size, realized=realized)
        trade = position_trade(tracker.collect_snapshot())
        assert trade.profit_target_reached is expected


def test_restart_preserves_original_risk_accumulated_profit_and_qualified_status(tmp_path):
    path = tmp_path / "state.json"
    client = MutableLighterClient()
    tracker = make_tracker(path, client)
    tracker.collect_snapshot()
    client.set_position(size="0.08", realized="530")
    assert not position_trade(tracker.collect_snapshot()).profit_target_reached
    assert json.loads(path.read_text())["profit_tracking"]

    tracker = make_tracker(path, client)
    assert not position_trade(tracker.collect_snapshot()).profit_target_reached
    client.set_position(size="0.06", realized="550")
    assert position_trade(tracker.collect_snapshot()).profit_target_reached

    tracker = make_tracker(path, client)
    assert position_trade(tracker.collect_snapshot()).profit_target_reached


def test_live_partial_profits_do_not_touch_state_and_next_rest_refresh_persists_mark(tmp_path):
    path = tmp_path / "state.json"
    client = MutableLighterClient()
    tracker = make_tracker(path, client)
    snapshot = tracker.collect_snapshot()
    stored_before_live = path.read_text()
    with patch.object(tracker, "state_store", wraps=tracker.state_store) as store:
        snapshot = tracker.apply_live_positions(snapshot, live_update(snapshot, size="0.08", realized="530"))
        assert not position_trade(snapshot).profit_target_reached
        snapshot = tracker.apply_live_positions(snapshot, live_update(snapshot, size="0.06", realized="550"))
        assert position_trade(snapshot).profit_target_reached
        assert store.mock_calls == []
    assert path.read_text() == stored_before_live

    client.set_position(size="0.06", realized="550")
    assert position_trade(tracker.collect_snapshot()).profit_target_reached
    assert position_trade(make_tracker(path, client).collect_snapshot()).profit_target_reached


def test_mark_valuation_time_does_not_suppress_newer_position_profit_observation(tmp_path):
    client = MutableLighterClient()
    tracker = make_tracker(tmp_path / "state.json", client)
    snapshot = tracker.collect_snapshot()
    original = position_trade(snapshot).position
    positions_at = snapshot.collected_at
    snapshot = tracker.apply_live_positions(snapshot, {1: SimpleNamespace(
        positions=(replace(original, unrealized_pnl=Decimal("75")),),
        positions_received_at=positions_at,
        received_at=positions_at + timedelta(seconds=10),
    )})
    assert not position_trade(snapshot).profit_target_reached

    snapshot = tracker.apply_live_positions(snapshot, {1: SimpleNamespace(
        positions=(replace(original, size=Decimal("0.08"), realized_pnl=Decimal("550")),),
        positions_received_at=positions_at + timedelta(seconds=5),
        received_at=positions_at + timedelta(seconds=11),
    )})
    assert position_trade(snapshot).profit_target_reached


def test_graceful_state_flush_persists_live_highlight_without_rest_refresh(tmp_path):
    path = tmp_path / "state.json"
    client = MutableLighterClient()
    tracker = make_tracker(path, client)
    snapshot = tracker.collect_snapshot()
    snapshot = tracker.apply_live_positions(snapshot, live_update(snapshot, size="0.08", realized="550"))
    assert position_trade(snapshot).profit_target_reached

    tracker.save_state()
    client.set_position(size="0.08", realized="550")
    restored = make_tracker(path, client).collect_snapshot()
    assert position_trade(restored).profit_target_reached


@pytest.mark.parametrize("via_live", [False, True])
def test_close_and_reopen_same_market_resets_profit_highlight(tmp_path, via_live):
    client = MutableLighterClient()
    tracker = make_tracker(tmp_path / "state.json", client)
    snapshot = tracker.collect_snapshot()
    client.set_position(size="0.08", realized="550")
    snapshot = tracker.collect_snapshot()
    assert position_trade(snapshot).profit_target_reached

    if via_live:
        reopened = replace(
            position_trade(snapshot).position,
            size=Decimal("0.1"), realized_pnl=Decimal("0"),
        )
        snapshot = tracker.apply_live_positions(snapshot, live_update(snapshot, size="0", realized="550"))
        assert snapshot.accounts[0].trades == ()
        # Keep the close snapshot: the first reopened stream row has no cached stop.
        snapshot = tracker.apply_live_positions(snapshot, {1: SimpleNamespace(
            positions=(reopened,), received_at=snapshot.pnl_collected_at + timedelta(seconds=1),
        )})
        assert not position_trade(snapshot).profit_target_reached
    else:
        client.set_position(size="0", realized="550")
        assert tracker.collect_snapshot().accounts[0].trades == ()

    client.set_position(size="0.1", realized="0")
    assert not position_trade(tracker.collect_snapshot()).profit_target_reached
    client.set_position(size="0.08", realized="30")
    assert not position_trade(tracker.collect_snapshot()).profit_target_reached


def test_account_realized_pnl_and_profit_highlights_are_isolated(tmp_path):
    client = MutableLighterClient(accounts=(1, 2))
    tracker = make_tracker(tmp_path / "state.json", client, accounts=(1, 2))
    tracker.collect_snapshot()
    client.set_position(account=1, size="0.08", realized="550")
    snapshot = tracker.collect_snapshot()
    assert position_trade(snapshot, account=1).profit_target_reached
    assert not position_trade(snapshot, account=2).profit_target_reached
    client.set_position(account=2, size="0.08", realized="530")
    snapshot = tracker.collect_snapshot()
    assert position_trade(snapshot, account=1).profit_target_reached
    assert not position_trade(snapshot, account=2).profit_target_reached


def test_bingx_realised_profit_marks_partial_close_without_affecting_lighter_same_index(tmp_path):
    client = MutableLighterClient(accounts=(0,))
    initial = {**bingx_position(), "positionAmt": "0.1", "realisedProfit": "500"}
    bingx = fake_bingx(
        positions=[initial],
        orders=[{**bingx_stop(price="99000"), "origQty": "0.1"}],
    )
    tracker = make_tracker(tmp_path / "state.json", client, accounts=(0,), bingx=bingx)
    snapshot = tracker.collect_snapshot()
    assert position_trade(snapshot, exchange="bingx", account=0).risk_usd == Decimal("100")
    assert not position_trade(snapshot, exchange="bingx", account=0).profit_target_reached

    for size, realized, expected in (("0.08", "530", False), ("0.06", "550", True)):
        bingx.fetch_positions.return_value = [{**initial, "positionAmt": size, "realisedProfit": realized}]
        snapshot = tracker.collect_snapshot()
        assert position_trade(snapshot, exchange="bingx", account=0).profit_target_reached is expected
        assert not position_trade(snapshot, exchange="lighter", account=0).profit_target_reached
