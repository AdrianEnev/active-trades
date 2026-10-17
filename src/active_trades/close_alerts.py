from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any, Literal

from active_trades.email_alerts import EmailConfigError, EmailSender, EmailSettings
from active_trades.lighter_api import _timestamp_to_datetime
from active_trades.models import AccountInfo, Side, now_utc


CloseKind = Literal["TP", "SL"]

_LIGHTER_KINDS: dict[str, CloseKind] = {
    "stop-loss": "SL",
    "stop-loss-limit": "SL",
    "take-profit": "TP",
    "take-profit-limit": "TP",
}
_BINGX_KINDS: dict[str, CloseKind] = {
    "STOP_MARKET": "SL",
    "STOP": "SL",
    "TRAILING_STOP_MARKET": "SL",
    "TAKE_PROFIT_MARKET": "TP",
    "TAKE_PROFIT": "TP",
}
# Fills shortly before enabling are still caught despite exchange/local clock skew.
_CLOCK_GRACE = timedelta(seconds=10)
# Lighter fills can lag their order record; wait a few polls for realized PNL.
_MAX_PNL_DEFERRALS = 3
_MAX_SEND_ATTEMPTS = 3
_SENT_NOTICE_SECONDS = 60.0


@dataclass(frozen=True)
class ClosedTrade:
    """One take-profit or stop-loss order that filled, fully or partially closing a position."""

    alert_id: str
    exchange: str
    account_label: str
    symbol: str
    side: Side
    kind: CloseKind
    size: Decimal
    fill_price: Decimal | None
    trigger_price: Decimal | None
    realized_pnl: Decimal | None
    fees: Decimal | None
    closed_at: datetime


def lighter_close_orders(raw_orders: Iterable[dict[str, Any]], *, since: datetime) -> list[dict[str, Any]]:
    """Inactive SL/TP orders with a fill at or after `since`."""
    return [
        raw for raw in raw_orders
        if isinstance(raw, dict)
        and str(raw.get("type") or "").strip().lower() in _LIGHTER_KINDS
        and (_decimal(raw.get("filled_base_amount")) or 0) > 0
        and (closed_at := _lighter_closed_at(raw)) is not None
        and closed_at >= since
    ]


def lighter_closed_trades(
    close_orders: Iterable[dict[str, Any]],
    raw_trades: Iterable[dict[str, Any]],
    *,
    account: AccountInfo,
) -> list[ClosedTrade]:
    """Join filled SL/TP orders to their fills, which carry Lighter's realized PNL."""
    trades = [trade for trade in raw_trades if isinstance(trade, dict)]
    closes: list[ClosedTrade] = []
    for raw in close_orders:
        order_index = str(raw.get("order_index") or raw.get("order_id") or "")
        is_ask = raw.get("is_ask") is True
        role = "ask" if is_ask else "bid"
        pnl_values = [
            value for trade in trades
            if str(trade.get(f"{role}_id") or "") == order_index
            and str(trade.get(f"{role}_account_id") or "") == str(account.index)
            and (value := _decimal(trade.get(f"{role}_account_pnl"))) is not None
        ]
        size = _decimal(raw.get("filled_base_amount")) or Decimal("0")
        quote = _decimal(raw.get("filled_quote_amount"))
        trigger = _decimal(raw.get("trigger_price"))
        closes.append(ClosedTrade(
            alert_id=f"lighter:{account.index}:{order_index}",
            exchange="lighter",
            account_label=f"Lighter · {account.label} (#{account.index})",
            symbol=str(raw.get("market_symbol") or raw.get("symbol") or raw.get("market_index") or "?"),
            # A sell closes a long; a buy closes a short.
            side="long" if is_ask else "short",
            kind=_LIGHTER_KINDS[str(raw.get("type")).strip().lower()],
            size=size,
            fill_price=quote / size if quote is not None and size > 0 else None,
            trigger_price=trigger if trigger is not None and trigger > 0 else None,
            realized_pnl=sum(pnl_values, Decimal("0")) if pnl_values else None,
            fees=None,
            closed_at=_lighter_closed_at(raw) or now_utc(),
        ))
    return closes


def bingx_closed_trades(raw_orders: Iterable[dict[str, Any]], *, since: datetime) -> list[ClosedTrade]:
    """Filled BingX SL/TP orders; `profit` is the exchange-reported realized PNL of that close."""
    closes: list[ClosedTrade] = []
    for raw in raw_orders:
        if not isinstance(raw, dict):
            continue
        kind = _BINGX_KINDS.get(str(raw.get("type") or "").strip().upper())
        size = _decimal(raw.get("executedQty"))
        closed_at = _bingx_closed_at(raw)
        if kind is None or size is None or size <= 0 or closed_at is None or closed_at < since:
            continue
        if str(raw.get("status") or "").strip().upper() not in {"FILLED", "CANCELLED", "CANCELED", "EXPIRED"}:
            continue  # Still working: alert once it has finished filling.
        position_side = str(raw.get("positionSide") or "").strip().upper()
        if position_side in {"LONG", "SHORT"}:
            side: Side = "long" if position_side == "LONG" else "short"
        else:
            side = "long" if str(raw.get("side") or "").strip().upper() == "SELL" else "short"
        fill_price = _decimal(raw.get("avgPrice"))
        trigger = _decimal(raw.get("stopPrice"))
        order_id = str(raw.get("orderId") or raw.get("orderID") or "")
        closes.append(ClosedTrade(
            alert_id=f"bingx:{order_id}",
            exchange="bingx",
            account_label="BingX",
            symbol=str(raw.get("symbol") or "?"),
            side=side,
            kind=kind,
            size=size,
            fill_price=fill_price if fill_price is not None and fill_price > 0 else None,
            trigger_price=trigger if trigger is not None and trigger > 0 else None,
            realized_pnl=_decimal(raw.get("profit")),
            fees=_decimal(raw.get("commission")),
            closed_at=closed_at,
        ))
    return closes


def alert_message(trade: ClosedTrade) -> tuple[str, str]:
    """Short subject and plain-text body for one TP/SL close."""
    title = "Take profit hit" if trade.kind == "TP" else "Stop loss hit"
    pnl = _money(trade.realized_pnl)
    subject = f"{trade.kind} hit: {trade.symbol} {trade.side.upper()} · PNL {pnl}"
    lines = [
        title,
        "",
        f"Realized PNL:  {pnl}",
    ]
    if trade.fees is not None:
        lines.append(f"Fees:          {_money(trade.fees)}")
    lines.extend((
        "",
        f"Asset:         {trade.symbol}",
        f"Position:      {trade.side.upper()}",
        f"Account:       {trade.account_label}",
        f"Size closed:   {_number(trade.size)}",
        f"Trigger price: {_number(trade.trigger_price)}",
        f"Fill price:    {_number(trade.fill_price)}",
        f"Closed at:     {trade.closed_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
    ))
    if trade.realized_pnl is None:
        lines.extend(("", "The exchange did not report realized PNL for this fill."))
    return subject, "\n".join(lines) + "\n"


class CloseAlertMonitor:
    """Email each TP/SL fill seen while enabled; exchange reads and SMTP run off the UI thread."""

    def __init__(
        self,
        fetch_closed_trades: Callable[[tuple[AccountInfo, ...], datetime], tuple[list[ClosedTrade], list[str]]],
        *,
        interval_seconds: float,
        sender_factory: Callable[[], Any] | None = None,
        now: Callable[[], datetime] = now_utc,
        monotonic: Callable[[], float] = time.monotonic,
        executor: Any = None,
    ) -> None:
        self._fetch = fetch_closed_trades
        self._interval = max(10.0, interval_seconds)
        self._sender_factory = sender_factory or (lambda: EmailSender(EmailSettings.from_env()))
        self._now = now
        self._monotonic = monotonic
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="active-trades-alerts")
        self._lock = RLock()
        self._enabled = False
        self._sender: Any = None
        self._since: datetime | None = None
        self._next_poll = 0.0
        self._pending: Future | None = None
        self._sent: set[str] = set()
        self._deferrals: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._notice: str | None = None
        self._notice_until: float | None = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def notice(self) -> str | None:
        with self._lock:
            if self._notice_until is not None and self._monotonic() >= self._notice_until:
                self._notice = self._notice_until = None
            return self._notice

    def set_enabled(self, enabled: bool) -> bool:
        """Turn alerts on or off; returns the resulting state (off if email is not configured)."""
        with self._lock:
            if enabled == self._enabled:
                return self._enabled
            if not enabled:
                self._enabled = False
                self._set_notice(None)
                return False
            try:
                self._sender = self._sender_factory()
            except EmailConfigError as exc:
                self._set_notice(f"Email alerts unavailable: {exc}", seconds=_SENT_NOTICE_SECONDS)
                return False
            self._enabled = True
            # Only fills from now on; earlier closes are history, not alerts.
            self._since = self._now() - _CLOCK_GRACE
            self._next_poll = self._monotonic() + self._interval
            self._set_notice(None)
            return True

    def toggle(self) -> bool:
        return self.set_enabled(not self.enabled)

    def tick(self, accounts: Iterable[AccountInfo]) -> None:
        """Start a background check when one is due; never blocks on network I/O."""
        with self._lock:
            if not self._enabled or (self._pending is not None and not self._pending.done()):
                return
            if self._monotonic() < self._next_poll:
                return
            self._next_poll = self._monotonic() + self._interval
            unique = tuple({(account.exchange, account.index): account for account in accounts}.values())
            self._pending = self._executor.submit(self._poll, unique)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _poll(self, accounts: tuple[AccountInfo, ...]) -> None:
        with self._lock:
            since, sender = self._since, self._sender
        if since is None or sender is None:
            return
        try:
            closes, errors = self._fetch(accounts, since)
        except Exception as exc:  # A failed check must not stop later checks.
            self._set_notice(f"Email alert check failed: {type(exc).__name__}")
            return
        failed = False
        for trade in sorted(closes, key=lambda item: item.closed_at):
            with self._lock:
                if not self._enabled or trade.alert_id in self._sent:
                    continue
                if trade.realized_pnl is None:
                    waited = self._deferrals.get(trade.alert_id, 0)
                    if waited < _MAX_PNL_DEFERRALS:
                        self._deferrals[trade.alert_id] = waited + 1
                        continue
            try:
                sender.send(*alert_message(trade))
            except Exception as exc:
                failed = True
                with self._lock:
                    attempts = self._failures.get(trade.alert_id, 0) + 1
                    self._failures[trade.alert_id] = attempts
                    if attempts >= _MAX_SEND_ATTEMPTS:
                        self._sent.add(trade.alert_id)  # Give up rather than retrying forever.
                    self._set_notice(f"Email alert failed for {trade.symbol} {trade.kind}: {type(exc).__name__}")
                continue
            with self._lock:
                self._sent.add(trade.alert_id)
                self._set_notice(
                    f"Email sent: {trade.symbol} {trade.kind} hit ({trade.account_label})",
                    seconds=_SENT_NOTICE_SECONDS,
                )
        if errors and not failed:
            self._set_notice(f"Email alert check incomplete: {errors[0]}", seconds=_SENT_NOTICE_SECONDS)

    def _set_notice(self, message: str | None, *, seconds: float | None = None) -> None:
        with self._lock:
            self._notice = message
            self._notice_until = self._monotonic() + seconds if message and seconds else None


def _lighter_closed_at(raw: dict[str, Any]) -> datetime | None:
    for name in ("updated_at", "transaction_time", "timestamp"):
        value = _timestamp_to_datetime(raw.get(name))
        if value is not None:
            return value
    return None


def _bingx_closed_at(raw: dict[str, Any]) -> datetime | None:
    # A triggered stop's `time` is its trigger time; updateTime covers later fills.
    values = [_timestamp_to_datetime(raw.get(name)) for name in ("updateTime", "time")]
    known = [value for value in values if value is not None]
    return max(known) if known else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.2f}"


def _number(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"
