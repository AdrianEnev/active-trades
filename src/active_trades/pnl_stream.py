from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from active_trades.lighter_api import _ssl_context_for_url, position_from_dict
from active_trades.models import Position


@dataclass(frozen=True)
class StreamPositions:
    positions: tuple[Position, ...]
    received_at: datetime
    positions_received_at: datetime


class PnlStream:
    """Keep public position deltas and mark prices off the rendering thread."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 15,
        connect_fn: Callable[..., Any] | None = None,
    ) -> None:
        self._url = _stream_url(base_url)
        self._ssl = _ssl_context_for_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._connect_fn = connect_fn
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connection: Any = None
        self._accounts: set[int] = set()
        self._positions: dict[int, dict[int, Position]] = {}
        self._position_times: dict[int, dict[int, datetime]] = {}
        self._account_times: dict[int, datetime] = {}
        self._snapshot_times: dict[int, datetime] = {}
        self._marks: dict[int, tuple[Decimal, datetime]] = {}
        self._notice: str | None = "UPNL stream connecting."

    @property
    def notice(self) -> str | None:
        with self._lock:
            return self._notice

    def start(self, account_indexes: Iterable[int]) -> None:
        self.set_accounts(account_indexes)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="active-trades-pnl", daemon=True)
            self._thread.start()

    def set_accounts(self, account_indexes: Iterable[int]) -> None:
        indexes = {int(index) for index in account_indexes}
        with self._lock:
            self._accounts = indexes
            for index in set(self._positions) - indexes:
                self._positions.pop(index, None)
                self._position_times.pop(index, None)
                self._account_times.pop(index, None)
                self._snapshot_times.pop(index, None)

    def seed_positions(
        self,
        account_index: int,
        positions: Iterable[Position],
        received_at: datetime,
    ) -> None:
        """Reconcile REST state without discarding deltas received during its request."""
        incoming = {position.market_id: position for position in positions}
        with self._lock:
            if account_index not in self._accounts:
                return
            if self._snapshot_times.get(account_index, received_at) > received_at:
                return
            current = self._positions.setdefault(account_index, {})
            times = self._position_times.setdefault(account_index, {})
            for market_id in set(current) | set(incoming) | set(times):
                if times.get(market_id, received_at) > received_at:
                    continue
                if market_id in incoming:
                    current[market_id] = incoming[market_id]
                else:
                    current.pop(market_id, None)
                times[market_id] = received_at
            self._account_times[account_index] = max(
                received_at, self._account_times.get(account_index, received_at)
            )

    def latest_positions(self) -> Mapping[int, StreamPositions]:
        with self._lock:
            result: dict[int, StreamPositions] = {}
            for account_index, positions in self._positions.items():
                account_time = self._account_times[account_index]
                valuation_time = account_time
                valued_positions: list[Position] = []
                for market_id in sorted(positions):
                    position = positions[market_id]
                    mark = self._marks.get(market_id)
                    position_time = self._position_times[account_index][market_id]
                    if mark is not None and mark[1] > position_time:
                        price, mark_time = mark
                        if (
                            position.unrealized_pnl is not None
                            and position.position_value is not None
                            and position.position_value > 0
                        ):
                            # API entry prices are rounded; retain the canonical entry precision.
                            old_mark = abs(position.position_value) / position.size
                            pnl = position.unrealized_pnl + position.signed_size * (price - old_mark)
                        else:
                            pnl = position.signed_size * (price - position.avg_entry_price)
                        position = replace(
                            position, unrealized_pnl=pnl, position_value=position.size * price
                        )
                        valuation_time = max(valuation_time, mark_time)
                    valued_positions.append(position)
                result[account_index] = StreamPositions(
                    positions=tuple(valued_positions),
                    received_at=valuation_time,
                    positions_received_at=account_time,
                )
            return result

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            connection = self._connection
            thread = self._thread
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                connect_fn = self._connect_fn
                if connect_fn is None:
                    from websockets.sync.client import connect

                    connect_fn = connect
                options: dict[str, Any] = {
                    "open_timeout": self._timeout_seconds,
                    "close_timeout": 1,
                    "ping_interval": 20,
                    "ping_timeout": 20,
                }
                if self._ssl is not None:
                    options["ssl"] = self._ssl
                with connect_fn(self._url, **options) as connection:
                    with self._lock:
                        self._connection = connection
                        self._notice = "UPNL stream connecting."
                        # Reconnection requires fresh market/account subscriptions.
                        self._marks.clear()
                    subscriptions: set[int] = set()
                    connection.send(json.dumps({"type": "subscribe", "channel": "market_stats/all"}))
                    while not self._stop.is_set():
                        with self._lock:
                            accounts = self._accounts.copy()
                        for account_index in sorted(subscriptions - accounts):
                            connection.send(json.dumps({
                                "type": "unsubscribe",
                                "channel": f"account_all_positions/{account_index}",
                            }))
                        for account_index in sorted(accounts - subscriptions):
                            connection.send(json.dumps({
                                "type": "subscribe",
                                "channel": f"account_all_positions/{account_index}",
                            }))
                        subscriptions = accounts
                        try:
                            raw_message = connection.recv(timeout=0.25)
                        except TimeoutError:
                            continue
                        if self._handle_message(raw_message, connection):
                            delay = 1.0
            except Exception:
                # Never show transport payloads, URLs, headers, or exception text.
                with self._lock:
                    self._notice = "UPNL stream disconnected; retrying."
            finally:
                with self._lock:
                    self._connection = None
            if not self._stop.is_set():
                self._stop.wait(delay)
                delay = min(delay * 2, 30)

    def _handle_message(self, raw_message: Any, connection: Any = None) -> bool:
        try:
            message = json.loads(raw_message) if isinstance(raw_message, (str, bytes)) else raw_message
        except (ValueError, TypeError):
            return False
        if not isinstance(message, dict):
            return False
        message_type = message.get("type")
        if message_type == "ping":
            if connection is not None:
                connection.send(json.dumps({"type": "pong"}))
            return False
        if message_type in {"shutdown", "error"}:
            raise RuntimeError("UPNL stream requires reconnection.")
        if message_type not in {
            "subscribed/account_all_positions", "update/account_all_positions",
            "subscribed/market_stats", "update/market_stats",
        }:
            return False
        received_at = datetime.now(UTC)
        if message_type.endswith("/account_all_positions"):
            channel = str(message.get("channel", ""))
            try:
                account_index = int(channel.replace("/", ":").split(":")[-1])
            except ValueError:
                return False
            raw_positions = message.get("positions")
            if not isinstance(raw_positions, dict):
                return False
            with self._lock:
                if account_index not in self._accounts:
                    return False
                if message_type.startswith("subscribed/"):
                    self._positions[account_index] = {}
                    self._position_times[account_index] = {}
                    self._snapshot_times[account_index] = received_at
                current = self._positions.setdefault(account_index, {})
                times = self._position_times.setdefault(account_index, {})
                for key, raw_position in raw_positions.items():
                    if not isinstance(raw_position, dict):
                        continue
                    try:
                        market_id = int(raw_position.get("market_id", raw_position.get("market_index", key)))
                    except (ValueError, TypeError):
                        continue
                    try:
                        position = position_from_dict(
                            {**raw_position, "market_id": market_id}, account_index=account_index
                        )
                    except (ArithmeticError, TypeError, ValueError):
                        continue
                    if position is not None:
                        if not position.size.is_finite() or not position.avg_entry_price.is_finite():
                            continue
                        position = replace(
                            position,
                            unrealized_pnl=(
                                position.unrealized_pnl
                                if position.unrealized_pnl is not None and position.unrealized_pnl.is_finite()
                                else None
                            ),
                            position_value=(
                                position.position_value
                                if position.position_value is not None and position.position_value.is_finite()
                                else None
                            ),
                        )
                        current[market_id] = position
                    elif _decimal(raw_position.get("position", raw_position.get("size"))) == 0:
                        current.pop(market_id, None)
                    else:
                        continue
                    times[market_id] = received_at
                self._account_times[account_index] = received_at
                self._notice = None
            return True
        raw_stats = message.get("market_stats")
        if not isinstance(raw_stats, dict):
            return False
        # The all channel is keyed by market ID; individual channels contain one object.
        stats = [raw_stats] if "market_id" in raw_stats else raw_stats.values()
        with self._lock:
            for raw_market in stats:
                if not isinstance(raw_market, dict):
                    continue
                try:
                    market_id = int(raw_market["market_id"])
                except (KeyError, ValueError, TypeError):
                    continue
                mark_price = _decimal(raw_market.get("mark_price"))
                if mark_price is not None and mark_price > 0:
                    self._marks[market_id] = (mark_price, received_at)
        return True


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _stream_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = dict(parse_qsl(parsed.query))
    query["readonly"] = "true"
    return urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/") + "/stream", urlencode(query), ""))
