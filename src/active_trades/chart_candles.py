"""Read public, exchange-matched OHLC history for a closed journal position.

Lighter's candle requests use seconds; candle timestamps and BingX requests use
milliseconds. We keep timestamps on the real time axis and never fill missing
bars with invented prices. Context outside the trade is opportunistic, but the
entire period from its opening fill through its closing fill must be present.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from active_trades.exit_chart import Candle
from active_trades.journal_executions import JournalError
from active_trades.lighter_api import _ssl_context_for_url


MINUTE = 60_000
DAY = 24 * 60 * MINUTE
# These are the fixed-length intervals supported by the official APIs. Month
# candles are deliberately excluded: their variable length breaks time bounds.
_INTERVALS = {
    "1m": MINUTE, "3m": 3 * MINUTE, "5m": 5 * MINUTE,
    "15m": 15 * MINUTE, "30m": 30 * MINUTE, "1h": 60 * MINUTE,
    "2h": 120 * MINUTE, "4h": 240 * MINUTE, "6h": 360 * MINUTE,
    "8h": 480 * MINUTE, "12h": 720 * MINUTE, "1d": DAY,
    "3d": 3 * DAY, "1w": 7 * DAY,
}
_LIGHTER_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d")
_BINGX_INTERVALS = tuple(_INTERVALS)
_PAGE_BARS = 400  # Below Lighter's 500 and BingX's 1440 per-request limits.
_MAX_PAGES = 32


@dataclass(frozen=True)
class CandleWindow:
    interval: str
    interval_ms: int
    start_ms: int
    end_ms: int  # exclusive upper bound on candle opening timestamps


def _floor_bar(value: int, interval: str) -> int:
    duration = _INTERVALS[interval]
    # Exchange weekly candles open on Monday, whereas the Unix epoch is Thursday.
    offset = 4 * DAY if interval == "1w" else 0
    return (value - offset) // duration * duration + offset


def candle_window(trade, *, now_ms: int, interval: str = "auto") -> CandleWindow:
    """Choose about 100–240 bars, including context and available exit padding."""
    opened, closed = trade.opened_ms, trade.closed_ms
    if (isinstance(opened, bool) or not isinstance(opened, int) or opened <= 0
            or isinstance(closed, bool) or not isinstance(closed, int)
            or closed < opened or closed > now_ms):
        raise JournalError("Exit chart needs valid opening and closing fill timestamps.")
    supported = _LIGHTER_INTERVALS if trade.exchange == "lighter" else _BINGX_INTERVALS
    if trade.exchange not in {"lighter", "bingx"}:
        raise JournalError("Exit charts do not support this exchange.")
    if interval == "auto":
        interval = next((name for name in supported
                         if math.ceil((closed - opened) / _INTERVALS[name]) <= 160), supported[-1])
    if interval not in supported:
        raise JournalError("The selected exit-chart interval is unavailable on this exchange.")
    duration = _INTERVALS[interval]
    first, last = _floor_bar(opened, interval), _floor_bar(closed, interval)
    end = min(last + 13 * duration, _floor_bar(now_ms, interval) + duration)
    trade_and_exit_bars = (end - first) // duration
    before_bars = max(60, 100 - trade_and_exit_bars)
    start = max(0, first - before_bars * duration)
    if math.ceil((end - start) / duration) > _PAGE_BARS * _MAX_PAGES:
        raise JournalError("Exit-chart history is too large; choose a wider candle interval.")
    return CandleWindow(interval, duration, start, end)


class CandleReader:
    """Public history reader; ``get_json`` and millisecond ``clock`` are injectable.

    A transport accepts ``(base_url, path, query, *, timeout_seconds)`` and returns
    decoded JSON. It must not sign requests or attach account credentials.
    """

    def __init__(self, tracker, *, get_json: Callable[..., dict] | None = None,
                 clock: Callable[[], int] | None = None) -> None:
        self.tracker = tracker
        self.get_json = get_json or public_get_json
        self.clock = clock or (lambda: int(time.time() * 1000))

    def read(self, trade, *, interval: str = "auto") -> list[Candle]:
        now_ms = self.clock()
        window = candle_window(trade, now_ms=now_ms, interval=interval)
        if trade.exchange == "lighter":
            client = self.tracker.client
            try:
                market = int(trade.market)
            except (ValueError, TypeError):
                raise JournalError("Lighter exit chart is missing its market identifier.") from None
            if not 0 <= market <= 32767:
                raise JournalError("Lighter exit chart has an invalid market identifier.")
        else:
            client = self.tracker.bingx_client
            market = trade.market
            if not isinstance(market, str) or not market or len(market) > 40:
                raise JournalError("BingX exit chart is missing its market symbol.")
        if client is None:
            raise JournalError("The exchange client for this exit chart is unavailable.")
        base_url = client.base_url
        timeout = max(1, min(10, float(getattr(client, "timeout_seconds", 10))))
        all_bars: dict[int, Candle] = {}
        page_start = window.start_ms
        while page_start < window.end_ms:
            page_end = min(window.end_ms, page_start + _PAGE_BARS * window.interval_ms)
            # Do not ask for future market data. The overlay itself may extend
            # beyond the close without pretending any future candles exist.
            actual_end = min(page_end, now_ms + 1)
            count = math.ceil((actual_end - page_start) / window.interval_ms)
            if trade.exchange == "lighter":
                path = "/api/v1/candles"
                query = {
                    "market_id": market, "resolution": window.interval,
                    "start_timestamp": page_start // 1000,
                    "end_timestamp": math.ceil(actual_end / 1000),
                    "count_back": count, "set_timestamp_to_end": "false",
                }
            else:
                path = "/openApi/swap/v3/quote/klines"
                query = {
                    "symbol": market, "interval": window.interval,
                    "startTime": page_start, "endTime": actual_end - 1,
                    "limit": count,
                }
            try:
                payload = self.get_json(base_url, path, query, timeout_seconds=timeout)
            except JournalError:
                raise
            except Exception:
                # Injected transports and upstream clients may put signed URLs
                # or configuration values in errors. Never propagate their text.
                raise JournalError("Could not read public exit-chart candles; sync will retry.") from None
            bars = _parse_payload(payload, trade.exchange)
            for bar in bars:
                if not page_start <= bar.time_ms < page_end or bar.time_ms > now_ms:
                    continue
                if _floor_bar(bar.time_ms, window.interval) != bar.time_ms:
                    raise JournalError("Exit-chart history has an unexpected candle timestamp.")
                previous = all_bars.get(bar.time_ms)
                if previous is not None and previous != bar:
                    raise JournalError("Exit-chart history contains conflicting candles; sync will retry.")
                all_bars[bar.time_ms] = bar
            page_start = page_end
        candles = sorted(all_bars.values(), key=lambda candle: candle.time_ms)
        _check_coverage(candles, trade, window.interval_ms)
        return candles


def _price(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
        if result.is_finite() and result > 0:
            return result
    except (InvalidOperation, ValueError):
        pass
    raise JournalError("Exit-chart history contains a missing or invalid price.")


def _time_ms(value: Any) -> int:
    try:
        parsed = Decimal(str(value))
        if parsed.is_finite() and parsed == parsed.to_integral_value() and parsed > 0:
            return int(parsed)
    except (InvalidOperation, ValueError):
        pass
    raise JournalError("Exit-chart history contains an invalid timestamp.")


def _parse_payload(payload: Any, exchange: str) -> list[Candle]:
    expected_code = 200 if exchange == "lighter" else 0
    if not isinstance(payload, dict) or str(payload.get("code")) != str(expected_code):
        raise JournalError(f"{exchange.title()} rejected the public candle request; sync will retry.")
    records = payload.get("c" if exchange == "lighter" else "data")
    if not isinstance(records, list):
        raise JournalError("The exchange returned an unexpected exit-chart candle response.")
    result: list[Candle] = []
    for record in records:
        if exchange == "lighter" and isinstance(record, dict):
            values = [record.get(name) for name in ("t", "o", "h", "l", "c")]
        elif exchange == "bingx" and isinstance(record, dict):
            values = [record.get(name) for name in ("time", "open", "high", "low", "close")]
        elif exchange == "bingx" and isinstance(record, (list, tuple)) and len(record) >= 5:
            values = record[:5]
        else:
            raise JournalError("The exchange returned a malformed exit-chart candle.")
        opened, high, low, close = [_price(value) for value in values[1:]]
        if low > min(opened, close) or high < max(opened, close) or low > high:
            raise JournalError("Exit-chart history contains inconsistent candle prices.")
        result.append(Candle(_time_ms(values[0]), opened, high, low, close))
    return result


def _check_coverage(candles: list[Candle], trade, interval_ms: int) -> None:
    if not candles:
        raise JournalError("No public candles are available for this exit chart; sync will retry.")
    if (candles[0].time_ms > trade.opened_ms
            or candles[-1].time_ms + interval_ms <= trade.closed_ms):
        raise JournalError("Exit-chart history does not cover the entire trade; sync will retry.")
    if any(after.time_ms - before.time_ms != interval_ms
           for before, after in zip(candles, candles[1:])):
        raise JournalError("Exit-chart history has missing candles; sync will retry.")


def public_get_json(base_url: str, path: str, query: dict, *, timeout_seconds: float) -> dict:
    """GET public prices with a bounded timeout, one transient retry and safe errors."""
    url = f"{base_url.rstrip('/')}{path}?{urlencode(query)}"
    for attempt in range(2):
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "active-trades/0.1"})
            with urlopen(request, timeout=timeout_seconds, context=_ssl_context_for_url(url)) as response:
                payload = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
        except HTTPError as exc:
            if attempt == 0 and (exc.code == 429 or exc.code >= 500):
                time.sleep(1)
                continue
            raise JournalError(f"Public exit-chart candle request failed (HTTP {exc.code}); sync will retry.") from None
        except (URLError, OSError, TimeoutError):
            if attempt == 0:
                time.sleep(1)
                continue
            raise JournalError("Public exit-chart candle request failed; sync will retry.") from None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise JournalError("Public exit-chart candle response was invalid; sync will retry.") from None
        if not isinstance(payload, dict):
            raise JournalError("Public exit-chart candle response was not an object.")
        return payload
    raise JournalError("Public exit-chart candle request failed; sync will retry.")
