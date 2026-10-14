from __future__ import annotations

import argparse
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from active_trades.env import load_shared_env
from active_trades.settings import Settings, settings_from_env
from active_trades.tracker import ActiveTradesTracker
from active_trades.pnl_stream import PnlStream
from active_trades.ui import render_plain, render_snapshot
from active_trades.terminal import DashboardPager

try:
    from rich.console import Console
    from rich.live import Live
except ImportError:  # pragma: no cover - plain mode handles this.
    Console = None  # type: ignore[assignment]
    Live = None  # type: ignore[assignment]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track Lighter and BingX positions and limit orders in one dashboard.",
    )
    parser.add_argument(
        "--refresh",
        type=float,
        help="Refresh interval in seconds. Defaults to ACTIVE_TRADES_REFRESH_SECONDS or 15.",
    )
    parser.add_argument(
        "--pnl-refresh",
        type=float,
        help="UPNL display interval in seconds (minimum 0.5). Defaults to ACTIVE_TRADES_PNL_REFRESH_SECONDS or 0.5.",
    )
    parser.add_argument(
        "--account-index",
        type=int,
        help="Base Lighter account index. Defaults to LIGHTER_ACCOUNT_INDEX.",
    )
    parser.add_argument(
        "--accounts",
        help="Comma-separated account indexes to track. Skips L1 sub-account discovery.",
    )
    parser.add_argument(
        "--state",
        help="Path to the local first-seen/open-date state file.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="HTTP timeout in seconds. Defaults to ACTIVE_TRADES_TIMEOUT_SECONDS or 15.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch once, print the dashboard, and exit.",
    )
    parser.add_argument(
        "--list-accounts",
        action="store_true",
        help="List discovered account indexes and exit.",
    )
    parser.add_argument(
        "--no-orders",
        action="store_true",
        help="Skip authenticated active-order reads. Risk and limit orders will show as disabled.",
    )
    parser.add_argument(
        "--no-open-date-inference",
        action="store_true",
        help="Skip trade-history lookback and use stop-order/first-seen dates.",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Use plain terminal output instead of Rich live rendering.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_shared_env(anchor_path=Path(__file__))
    args = parse_args(argv)
    try:
        settings = settings_from_env(
            refresh_seconds=args.refresh,
            pnl_refresh_seconds=args.pnl_refresh,
            account_index=args.account_index,
            account_indexes=_parse_account_indexes_arg(args.accounts),
            state_path=args.state,
            timeout_seconds=args.timeout,
            fetch_orders=not args.no_orders,
            infer_open_dates=not args.no_open_date_inference,
        )
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    tracker = ActiveTradesTracker(settings)
    if args.list_accounts:
        return _list_accounts(tracker)
    if args.once:
        snapshot = tracker.collect_snapshot()
        _print_once(snapshot, settings=settings, plain=args.plain)
        return 0 if not snapshot.errors else 1
    return _run_live(tracker, settings=settings, plain=args.plain)


def _run_live(tracker: ActiveTradesTracker, *, settings: Settings, plain: bool) -> int:
    stop = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    stream = PnlStream(base_url=settings.base_url, timeout_seconds=settings.timeout_seconds)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="active-trades-refresh")
    try:
        snapshot = tracker.collect_snapshot()
        stream.start(_lighter_account_indexes(snapshot))
        _seed_stream(stream, snapshot)
        if not plain and Console and Live:
            console = Console()
            with DashboardPager() as pager, Live(
                render_snapshot(
                    snapshot, refresh_seconds=settings.refresh_seconds,
                    pnl_refresh_seconds=settings.pnl_refresh_seconds, live=True,
                    page=pager.page if pager.active else None,
                ),
                console=console,
                auto_refresh=False,
                vertical_overflow="ellipsis" if pager.active else "visible",
                transient=False,
            ) as live:
                def display(current) -> None:
                    live.update(render_snapshot(
                        current, refresh_seconds=settings.refresh_seconds,
                        pnl_refresh_seconds=settings.pnl_refresh_seconds, live=True,
                        page=pager.page if pager.active else None,
                    ), refresh=True)
                _live_loop(
                    tracker, snapshot, settings, stream, executor, display,
                    lambda: stop or pager.quit_requested, poll=pager.poll,
                )
            return 0

        def display_plain(current) -> None:
            print("\033[2J\033[H", end="")
            print(render_plain(
                current, refresh_seconds=settings.refresh_seconds,
                pnl_refresh_seconds=settings.pnl_refresh_seconds, live=True,
            ), flush=True)
        display_plain(snapshot)
        _live_loop(tracker, snapshot, settings, stream, executor, display_plain, lambda: stop)
        return 0
    finally:
        stream.close()
        executor.shutdown(wait=True, cancel_futures=True)
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def _live_loop(tracker, snapshot, settings, stream, executor, display, should_stop, *, poll=lambda: None) -> None:
    """Keep rendering stream updates while slow REST work runs in one worker."""
    next_refresh = time.monotonic() + settings.refresh_seconds
    refresh = None
    refresh_notice = None
    while not should_stop():
        _sleep(settings.pnl_refresh_seconds, should_stop)
        poll()
        if should_stop():
            return
        if refresh is not None and refresh.done():
            try:
                refreshed = refresh.result()
                if refreshed.accounts:
                    snapshot = refreshed
                    stream.set_accounts(_lighter_account_indexes(snapshot))
                    _seed_stream(stream, snapshot)
                    refresh_notice = None
                else:
                    refresh_notice = "Account refresh failed; retaining the last snapshot."
            except Exception:
                refresh_notice = "Account refresh failed; retaining the last snapshot."
            refresh = None
            next_refresh = time.monotonic() + settings.refresh_seconds
        if refresh is None and time.monotonic() >= next_refresh:
            refresh = executor.submit(tracker.collect_snapshot)
        snapshot = tracker.apply_live_positions(snapshot, stream.latest_positions())
        notices = tuple(message for message in (stream.notice, refresh_notice) if message)
        display(replace(snapshot, errors=(*snapshot.errors, *notices)))


def _seed_stream(stream, snapshot) -> None:
    for section in snapshot.accounts:
        if section.account.exchange != "lighter" or section.positions_state != "available":
            continue
        if any(error.startswith("Could not fetch account positions") for error in section.errors):
            continue
        stream.seed_positions(
            section.account.index,
            tuple(trade.position for trade in section.trades),
            snapshot.collected_at,
        )


def _lighter_account_indexes(snapshot) -> tuple[int, ...]:
    return tuple(
        section.account.index for section in snapshot.accounts
        if section.account.exchange == "lighter"
    )


def _list_accounts(tracker: ActiveTradesTracker) -> int:
    accounts, errors = tracker.discover_accounts()
    for account in accounts:
        print(f"{account.index}\t{account.label}\t{account.l1_address}")
    for error in errors:
        print(f"Notice: {error}", file=sys.stderr)
    return 0 if accounts else 1


def _print_once(snapshot, *, settings: Settings, plain: bool) -> None:
    if not plain and Console:
        Console().print(render_snapshot(
            snapshot, refresh_seconds=settings.refresh_seconds,
            pnl_refresh_seconds=settings.pnl_refresh_seconds,
        ))
        return
    print(render_plain(
        snapshot, refresh_seconds=settings.refresh_seconds,
        pnl_refresh_seconds=settings.pnl_refresh_seconds,
    ))


def _sleep(seconds: float, should_stop) -> None:
    deadline = time.monotonic() + seconds
    while not should_stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _parse_account_indexes_arg(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    indexes: list[int] = []
    for raw_part in value.replace(";", ",").split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            indexes.append(int(part))
        except ValueError as exc:
            raise ValueError("--accounts must contain comma-separated integers.") from exc
    return tuple(dict.fromkeys(indexes))
