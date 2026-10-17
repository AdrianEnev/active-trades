from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call

import pytest

from active_trades import cli
from active_trades.models import AccountInfo, AccountSnapshot, ActiveTrade, Position, TrackerSnapshot
from active_trades.terminal import DashboardPager


@pytest.fixture
def snapshot():
    position = Position(1, 0, "BTC", "long", Decimal("2"), Decimal("100"), unrealized_pnl=Decimal("25"))
    trade = ActiveTrade(1, position, None, "state", Decimal("90"), Decimal("20"), "at-risk")
    return TrackerSnapshot(
        datetime(2026, 10, 16, tzinfo=UTC),
        (AccountSnapshot(AccountInfo(1), (trade,)),),
    )


@pytest.fixture
def settings():
    return SimpleNamespace(
        base_url="https://unused.example", timeout_seconds=1,
        refresh_seconds=15, pnl_refresh_seconds=30,
        state_path=Path("state/position_state.json"),
    )


@pytest.fixture
def live_harness(monkeypatch, snapshot):
    pagers = []
    state = SimpleNamespace(keys=[], active=True)

    class TestPager(DashboardPager):
        def __enter__(self):
            self.active = state.active
            pagers.append(self)
            return self

        def __exit__(self, *_args):
            self.active = False

        def poll(self):
            before = self.page, self.hide_amounts, self.quit_requested
            self._consume(state.keys.pop(0) if state.keys else "")
            return before != (self.page, self.hide_amounts, self.quit_requested)

    tracker = Mock()
    tracker.collect_snapshot.return_value = snapshot
    tracker.apply_live_positions.side_effect = lambda current, _updates: current
    stream = Mock(notice=None)
    stream.latest_positions.return_value = {}
    executor = Mock()
    rich_render = Mock(return_value="rich dashboard")
    plain_render = Mock(return_value="plain dashboard")
    monkeypatch.setattr(cli, "DashboardPager", TestPager)
    monkeypatch.setattr(cli, "PnlStream", Mock(return_value=stream))
    monkeypatch.setattr(cli, "ThreadPoolExecutor", Mock(return_value=executor))
    monkeypatch.setattr(cli.signal, "signal", Mock())
    monkeypatch.setattr(cli, "Console", Mock())
    monkeypatch.setattr(cli, "Live", MagicMock())
    monkeypatch.setattr(cli, "render_snapshot", rich_render)
    monkeypatch.setattr(cli, "render_plain", plain_render)
    return SimpleNamespace(
        state=state, pagers=pagers, tracker=tracker, stream=stream,
        executor=executor, rich_render=rich_render, plain_render=plain_render,
    )


def test_hide_amounts_flag_is_opt_in():
    assert cli.parse_args([]).hide_amounts is False
    assert cli.parse_args(["--hide-amounts"]).hide_amounts is True


@pytest.mark.parametrize("once", [False, True])
def test_main_forwards_initial_privacy_mode(monkeypatch, snapshot, settings, once):
    tracker = Mock()
    tracker.collect_snapshot.return_value = snapshot
    output = Mock(return_value=0)
    monkeypatch.setattr(cli, "load_shared_env", Mock())
    monkeypatch.setattr(cli, "settings_from_env", Mock(return_value=settings))
    monkeypatch.setattr(cli, "ActiveTradesTracker", Mock(return_value=tracker))
    monkeypatch.setattr(cli, "_print_once" if once else "_run_live", output)
    arguments = ["--hide-amounts", "--plain"] + (["--once"] if once else [])
    assert cli.main(arguments) == 0
    output.assert_called_once_with(
        snapshot if once else tracker, settings=settings, plain=True, hide_amounts=True,
        **({} if once else {"email_alerts": False}),
    )


@pytest.mark.parametrize("plain", [False, True])
def test_print_once_passes_privacy_to_renderer_without_changing_values(monkeypatch, snapshot, settings, plain):
    original = deepcopy(snapshot)
    renderer = Mock(return_value="masked dashboard")
    monkeypatch.setattr(cli, "Console", Mock())
    monkeypatch.setattr(cli, "render_plain" if plain else "render_snapshot", renderer)
    cli._print_once(snapshot, settings=settings, plain=plain, hide_amounts=True)
    renderer.assert_called_once_with(
        snapshot, refresh_seconds=15, pnl_refresh_seconds=30, hide_amounts=True,
    )
    assert renderer.call_args.args[0] is snapshot
    assert snapshot == original
    assert snapshot.accounts[0].trades[0].risk_usd == Decimal("20")


@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.parametrize("start_hidden", [False, True])
def test_live_privacy_toggle_reaches_every_redraw(
    monkeypatch, live_harness, snapshot, settings, plain, start_hidden,
):
    original = deepcopy(snapshot)
    harness = live_harness

    def loop(_tracker, current, _settings, _stream, _executor, display, should_stop, *, poll, alerts=None, cancels=None):
        assert not should_stop()
        for _ in range(2):
            harness.state.keys.append("m")
            assert poll()
            display(current)

    monkeypatch.setattr(cli, "_live_loop", loop)
    assert cli._run_live(harness.tracker, settings=settings, plain=plain, hide_amounts=start_hidden) == 0
    renderer = harness.plain_render if plain else harness.rich_render
    assert [call.kwargs["hide_amounts"] for call in renderer.call_args_list] == [
        start_hidden, not start_hidden, start_hidden,
    ]
    assert all(call.args[0] is snapshot for call in renderer.call_args_list)
    assert snapshot == original
    harness.stream.close.assert_called_once()
    # The refresh worker drops queued reads; a confirmed cancel is allowed to finish.
    assert harness.executor.shutdown.call_args_list == [call(wait=True, cancel_futures=True), call(wait=True)]
    harness.tracker.save_state.assert_called_once()


def test_plain_live_quit_key_exits_without_waiting_for_refresh(
    monkeypatch, live_harness, settings, capsys,
):
    harness = live_harness
    harness.state.keys.append("q")
    monkeypatch.setattr(cli, "_sleep", lambda _seconds, should_stop: should_stop())
    assert cli._run_live(harness.tracker, settings=settings, plain=True) == 0
    harness.plain_render.assert_called_once()
    harness.tracker.apply_live_positions.assert_not_called()
    assert harness.pagers[0].quit_requested
    assert "m hide amounts · q quit" in capsys.readouterr().out


def test_plain_noninteractive_output_omits_keyboard_hint(monkeypatch, live_harness, settings, capsys):
    live_harness.state.active = False
    monkeypatch.setattr(cli, "_live_loop", Mock())
    cli._run_live(live_harness.tracker, settings=settings, plain=True, hide_amounts=True)
    assert "q quit" not in capsys.readouterr().out
    assert live_harness.plain_render.call_args.kwargs["hide_amounts"] is True


def test_privacy_action_wakes_live_loop_during_long_pnl_interval(monkeypatch, snapshot):
    elapsed = [0.0]
    action_sent = [False]
    rendered = []
    tracker = Mock()
    tracker.apply_live_positions.return_value = snapshot
    stream = Mock(notice=None)
    stream.latest_positions.return_value = {}
    executor = Mock()

    def poll():
        if elapsed[0] >= 0.25 and not action_sent[0]:
            action_sent[0] = True
            return True
        return False

    def advance(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(cli.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(cli.time, "sleep", advance)
    cli._live_loop(
        tracker, snapshot, SimpleNamespace(refresh_seconds=3600, pnl_refresh_seconds=120),
        stream, executor, rendered.append, lambda: bool(rendered), poll=poll,
    )
    assert len(rendered) == 1
    assert action_sent[0]
    assert elapsed[0] <= 0.5
    executor.submit.assert_not_called()
