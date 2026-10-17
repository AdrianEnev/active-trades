from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from active_trades import chart_candles as module
from active_trades.chart_candles import CandleReader, candle_window, public_get_json
from active_trades.journal_executions import JournalError


MINUTE = 60_000
NOW = 1_790_000_000_000 // MINUTE * MINUTE + 30_000


def trade(exchange="lighter", *, duration=90 * MINUTE, closed=None):
    close = NOW - 30 * MINUTE if closed is None else closed
    return SimpleNamespace(exchange=exchange, market="1" if exchange == "lighter" else "BTC-USDT",
                           opened_ms=close - duration, closed_ms=close)


def tracker():
    return SimpleNamespace(client=SimpleNamespace(base_url="https://lighter.invalid", timeout_seconds=15),
                           bingx_client=SimpleNamespace(base_url="https://bingx.invalid", timeout_seconds=15))


def history_transport(calls, *, omit=None, mode="objects", mutate=None):
    def request(base_url, path, query, *, timeout_seconds):
        calls.append((base_url, path, query, timeout_seconds))
        lighter = "lighter" in base_url
        interval = module._INTERVALS[query.get("resolution", query.get("interval"))]
        start = query["start_timestamp"] * 1000 if lighter else query["startTime"]
        end = query["end_timestamp"] * 1000 if lighter else query["endTime"] + 1
        values = []
        for timestamp in range(start, end, interval):
            if timestamp == omit:
                continue
            ohlc = ["81600.123456789123456789", "81630.2", "81520.7", "81580.4"]
            if lighter:
                row = dict(zip(("t", "o", "h", "l", "c"), [timestamp, *ohlc]))
            elif mode == "arrays":
                row = [timestamp, *ohlc, "0"]
            else:
                row = dict(zip(("time", "open", "high", "low", "close"), [timestamp, *ohlc]))
            values.append(row)
        if mutate:
            mutate(values)
        # BingX currently returns newest first, despite older array-format docs.
        return {"code": 200 if lighter else 0, "c" if lighter else "data": values[::-1]}
    return request


@pytest.mark.parametrize("exchange,mode", [("lighter", "objects"), ("bingx", "objects"), ("bingx", "arrays")])
def test_original_exchange_history_is_sorted_precise_and_public(exchange, mode):
    calls = []
    position = trade(exchange)
    bars = CandleReader(tracker(), get_json=history_transport(calls, mode=mode), clock=lambda: NOW).read(position)
    assert 100 <= len(bars) <= 240
    assert bars[0].time_ms < position.opened_ms < bars[-1].time_ms
    assert bars[-1].time_ms > position.closed_ms
    assert all(b.time_ms > a.time_ms for a, b in zip(bars, bars[1:]))
    assert bars[0].open == Decimal("81600.123456789123456789")
    assert calls[0][1] == ("/api/v1/candles" if exchange == "lighter" else "/openApi/swap/v3/quote/klines")
    assert calls[0][3] == 10
    assert not any("auth" in str(value).lower() or "signature" in str(value).lower() for value in calls)


def test_newly_closed_trade_uses_current_candle_without_requesting_future_data():
    calls = []
    position = trade("bingx", closed=NOW)
    bars = CandleReader(tracker(), get_json=history_transport(calls), clock=lambda: NOW).read(position)
    assert calls[0][2]["endTime"] == NOW
    assert bars[-1].time_ms <= NOW < bars[-1].time_ms + MINUTE


def test_auto_resolution_preserves_long_trade_and_explicit_interval_paginates():
    position = trade(duration=200 * module.DAY)
    window = candle_window(position, now_ms=NOW)
    assert window.interval == "1d"
    calls = []
    bars = CandleReader(tracker(), get_json=history_transport(calls), clock=lambda: NOW).read(position, interval="12h")
    assert len(calls) == 2
    assert bars[0].time_ms <= position.opened_ms
    assert bars[-1].time_ms + 12 * 60 * MINUTE > position.closed_ms
    assert calls[0][2]["end_timestamp"] == calls[1][2]["start_timestamp"]


def test_missing_candle_inside_trade_is_rejected_instead_of_fabricated():
    position = trade()
    missing = position.opened_ms // MINUTE * MINUTE + 10 * MINUTE
    reader = CandleReader(tracker(), get_json=history_transport([], omit=missing), clock=lambda: NOW)
    with pytest.raises(JournalError, match="missing candles"):
        reader.read(position)


def test_truncated_history_cannot_omit_open_or_close():
    position = trade()
    def remove_opening(values):
        values[:] = [v for v in values if v["t"] > position.opened_ms]
    reader = CandleReader(tracker(), get_json=history_transport([], mutate=remove_opening), clock=lambda: NOW)
    with pytest.raises(JournalError, match="entire trade"):
        reader.read(position)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-3", "0", None])
def test_invalid_ohlc_is_rejected(value):
    def corrupt(values):
        values[0]["h"] = value
    reader = CandleReader(tracker(), get_json=history_transport([], mutate=corrupt), clock=lambda: NOW)
    with pytest.raises(JournalError, match="invalid price"):
        reader.read(trade())


def test_conflicting_duplicate_candle_is_rejected():
    def duplicate(values):
        values.append({**values[0], "c": "81590.4"})
    reader = CandleReader(tracker(), get_json=history_transport([], mutate=duplicate), clock=lambda: NOW)
    with pytest.raises(JournalError, match="conflicting"):
        reader.read(trade())


def test_transport_error_never_echoes_sensitive_details():
    def request(*args, **kwargs):
        raise RuntimeError("https://sensitive.invalid?signature=secret")
    with pytest.raises(JournalError) as error:
        CandleReader(tracker(), get_json=request, clock=lambda: NOW).read(trade())
    assert "secret" not in str(error.value)


def test_future_close_and_excessive_explicit_history_fail_before_requests():
    with pytest.raises(JournalError, match="timestamps"):
        candle_window(trade(closed=NOW + 1), now_ms=NOW)
    with pytest.raises(JournalError, match="too large"):
        candle_window(trade(duration=100 * module.DAY), now_ms=NOW, interval="1m")


def test_public_http_transport_retains_json_decimal_and_sends_no_auth(monkeypatch):
    requests = []
    def open_url(request, **kwargs):
        requests.append(request)
        return BytesIO(b'{"code":200,"value":0.123456789123456789}')
    monkeypatch.setattr(module, "urlopen", open_url)
    result = public_get_json("http://public.invalid", "/candles", {"market_id": 1}, timeout_seconds=2)
    assert result["value"] == Decimal("0.123456789123456789")
    assert set(requests[0].header_items()) == {("Accept", "application/json"), ("User-agent", "active-trades/0.1")}


@pytest.mark.parametrize("kind", ["connection", "rate_limit"])
def test_public_http_retries_only_once_and_sanitizes_failures(monkeypatch, kind):
    attempts = []
    def open_url(request, **kwargs):
        attempts.append(request)
        if kind == "rate_limit":
            raise HTTPError("secret", 429, "secret", {}, BytesIO(b"secret"))
        raise URLError("secret")
    monkeypatch.setattr(module, "urlopen", open_url)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(JournalError) as error:
        public_get_json("http://public.invalid", "/candles", {}, timeout_seconds=2)
    assert len(attempts) == 2
    assert "secret" not in str(error.value)
