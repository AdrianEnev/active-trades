from concurrent.futures import Future
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from active_trades.cli import _lighter_account_indexes, _live_loop, _seed_stream, parse_args
from active_trades.models import AccountInfo, AccountSnapshot, TrackerSnapshot


def _snapshot():
    return TrackerSnapshot(
        collected_at=datetime(2026, 10, 13, tzinfo=UTC),
        accounts=(AccountSnapshot(account=AccountInfo(index=1), trades=()),),
    )


def test_pnl_refresh_argument():
    assert parse_args(["--pnl-refresh", "1"]).pnl_refresh == 1


def test_stream_keeps_rendering_while_metadata_refresh_is_pending():
    snapshot = _snapshot()
    tracker = Mock()
    tracker.apply_live_positions.return_value = snapshot
    stream = Mock(notice=None)
    stream.latest_positions.return_value = {}
    executor = Mock()
    executor.submit.return_value = Future()  # Slow request never completes in this test.
    rendered = []
    elapsed = [0.0]
    settings = SimpleNamespace(refresh_seconds=1, pnl_refresh_seconds=0.5)

    def sleep(seconds, should_stop):
        elapsed[0] += seconds

    with patch("active_trades.cli._sleep", side_effect=sleep), patch(
        "active_trades.cli.time.monotonic", side_effect=lambda: elapsed[0]
    ):
        _live_loop(tracker, snapshot, settings, stream, executor, rendered.append, lambda: len(rendered) == 4)

    assert len(rendered) == 4
    assert tracker.apply_live_positions.call_count == 4
    executor.submit.assert_called_once_with(tracker.collect_snapshot)


def test_disconnect_notice_does_not_accumulate_every_tick():
    snapshot = _snapshot()
    tracker = Mock()
    tracker.apply_live_positions.side_effect = lambda current, updates: current
    stream = Mock(notice="UPNL stream reconnecting.")
    stream.latest_positions.return_value = {}
    rendered = []
    settings = SimpleNamespace(refresh_seconds=15, pnl_refresh_seconds=0.5)
    with patch("active_trades.cli._sleep"):
        _live_loop(tracker, snapshot, settings, stream, Mock(), rendered.append, lambda: len(rendered) == 3)
    assert all(item.errors == ("UPNL stream reconnecting.",) for item in rendered)


def test_failed_account_read_does_not_seed_empty_stream_positions():
    stream = Mock()
    snapshot = TrackerSnapshot(
        collected_at=datetime(2026, 10, 13, tzinfo=UTC),
        accounts=(AccountSnapshot(
            account=AccountInfo(index=1), trades=(),
            errors=("Could not fetch account positions: unavailable",),
        ),),
    )
    _seed_stream(stream, snapshot)
    stream.seed_positions.assert_not_called()


def test_bingx_is_excluded_from_lighter_stream_accounts_and_seeds():
    stream = Mock()
    snapshot = TrackerSnapshot(
        collected_at=datetime(2026, 10, 13, tzinfo=UTC),
        accounts=(
            AccountSnapshot(account=AccountInfo(index=1), trades=()),
            AccountSnapshot(account=AccountInfo(index=0, exchange="bingx"), trades=()),
        ),
    )

    assert _lighter_account_indexes(snapshot) == (1,)
    _seed_stream(stream, snapshot)

    stream.seed_positions.assert_called_once_with(1, (), snapshot.collected_at)


def test_completed_mixed_exchange_refresh_only_updates_lighter_stream_subscriptions():
    refreshed = TrackerSnapshot(
        collected_at=datetime(2026, 10, 13, 1, tzinfo=UTC),
        accounts=(
            AccountSnapshot(account=AccountInfo(index=1), trades=()),
            AccountSnapshot(account=AccountInfo(index=0, exchange="bingx"), trades=()),
        ),
    )
    completed = Future()
    completed.set_result(refreshed)
    tracker = Mock()
    tracker.apply_live_positions.side_effect = lambda current, updates: current
    stream = Mock(notice=None)
    stream.latest_positions.return_value = {}
    executor = Mock()
    executor.submit.return_value = completed
    elapsed = [0.0]
    rendered = []
    settings = SimpleNamespace(refresh_seconds=1, pnl_refresh_seconds=0.5)

    def sleep(seconds, should_stop):
        elapsed[0] += seconds

    with patch("active_trades.cli._sleep", side_effect=sleep), patch(
        "active_trades.cli.time.monotonic", side_effect=lambda: elapsed[0]
    ):
        _live_loop(
            tracker, _snapshot(), settings, stream, executor, rendered.append,
            lambda: len(rendered) == 3,
        )

    assert rendered[-1] == refreshed
    stream.set_accounts.assert_called_once_with((1,))
    stream.seed_positions.assert_called_once_with(1, (), refreshed.collected_at)
