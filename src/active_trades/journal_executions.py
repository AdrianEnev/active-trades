"""Read actual executions, including manual closes, without placing orders."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
from typing import Any

from active_trades.bingx_api import BingxApiError
from active_trades.lighter_api import LighterApiError, _raise_for_lighter_code


class JournalError(RuntimeError):
    """A journal failure whose message is safe to display."""


def number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
        if result.is_finite():
            return result
    except (InvalidOperation, ValueError):
        pass
    raise JournalError("Execution history contains a missing or invalid number; sync will retry.")


def timestamp_ms(value: Any) -> int:
    if isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return int(parsed.timestamp() * 1000)
        except ValueError:
            pass
    value = number(value)
    if value <= 0:
        raise JournalError("Execution history contains an invalid timestamp.")
    if value < 10_000_000_000:
        value *= 1000
    return int(value)


@dataclass(frozen=True)
class Execution:
    id: str
    exchange: str
    account: int
    market: str
    symbol: str
    time_ms: int
    delta: str
    price: str
    pnl: str | None
    fee: str = "0"
    lane: str = "BOTH"
    position_id: str = ""
    before: str | None = None

    @property
    def scope(self) -> str:
        return f"{self.exchange}:{self.account}"

    @property
    def position_key(self) -> tuple[str, str, str]:
        return self.market, self.lane, self.position_id

    def payload(self) -> dict:
        return asdict(self)


def lighter_execution(raw: dict, account: int, symbols: dict[int, str]) -> Execution:
    roles = [role for role in ("ask", "bid") if str(raw.get(f"{role}_account_id")) == str(account)]
    if len(roles) != 1:
        raise JournalError("Lighter execution has an ambiguous account side; journal sync paused for this account.")
    role = roles[0]
    fill_id = str(raw.get("trade_id_str") or raw.get("trade_id") or "")
    if not fill_id or raw.get("market_id") is None:
        raise JournalError("Lighter execution is missing its ID or market.")
    size, price = number(raw.get("size")), number(raw.get("price"))
    if size <= 0 or price <= 0:
        raise JournalError("Lighter execution has an invalid quantity or price.")
    maker = raw.get("is_maker_ask") is (role == "ask")
    before = raw.get(f"{'maker' if maker else 'taker'}_position_size_before")
    market = int(raw["market_id"])
    pnl = raw.get(f"{role}_account_pnl")
    return Execution(
        f"lighter:{account}:{fill_id}", "lighter", account, str(market),
        str(raw.get("market_symbol") or symbols.get(market, str(market))), timestamp_ms(raw.get("timestamp")),
        str(-size if role == "ask" else size), str(price),
        str(number(pnl)) if pnl is not None else None,
        before=str(number(before)) if before is not None else None,
    )


def bingx_execution(raw: dict, order: dict) -> Execution:
    fill_id = str(raw.get("tradeId") or raw.get("id") or raw.get("_journal_id") or "")
    symbol = str(raw.get("symbol") or "")
    side = str(raw.get("side") or order.get("side") or "").upper()
    lane = str(raw.get("positionSide") or order.get("positionSide") or "").upper()
    if not fill_id or not symbol or side not in {"BUY", "SELL"} or lane not in {"LONG", "SHORT", "BOTH"}:
        raise JournalError("BingX execution is missing its identity or position side; sync will retry.")
    size, price = number(raw.get("qty", raw.get("quantity", raw.get("volume")))), number(raw.get("price"))
    if size <= 0 or price <= 0:
        raise JournalError("BingX execution has an invalid quantity or price.")
    pnl = raw.get("realizedPnl", raw.get("profit"))
    fee = raw.get("fee", raw.get("commission"))
    if fee is None:
        raise JournalError("BingX execution omitted its fee; sync will retry.")
    asset = raw.get("commissionAsset", raw.get("feeAsset", raw.get("currency")))
    if asset and asset not in {"USDT", "USDC", "USD"}:
        raise JournalError("BingX execution fee is not in USD/USDT/USDC; automatic conversion is unavailable.")
    return Execution(
        f"bingx:0:{symbol}:{fill_id}", "bingx", 0, symbol, symbol,
        timestamp_ms(raw.get("time", raw.get("timestamp", raw.get("filledTm", raw.get("filledTime"))))),
        str(size if side == "BUY" else -size), str(price),
        str(number(pnl)) if pnl is not None else None, str(number(fee)), lane,
        str(raw.get("positionId") or order.get("positionId") or ""),
    )


class ExecutionReader:
    def __init__(self, tracker) -> None:
        self.tracker = tracker
        self.symbols: dict[int, str] = {}
        self.orders: dict[str, dict] = {}
        self._last_bingx_read = 0.0

    def _bingx_get(self, client, path, query):
        delay = 0.21 - (time.monotonic() - self._last_bingx_read)
        if delay > 0:
            time.sleep(delay)
        self._last_bingx_read = time.monotonic()
        return client._get_json(path, query)

    def read(self, exchange: str, account: int, since_ms: int, end_ms: int) -> list[Execution]:
        try:
            if exchange == "lighter":
                return self._lighter(account, since_ms, end_ms)
            return self._bingx(since_ms, end_ms)
        except (LighterApiError, BingxApiError):
            # API errors may contain upstream bodies. Do not expose auth material.
            raise JournalError(f"{exchange.title()} execution-history read failed; check credentials, access and connection.") from None

    def _lighter(self, account: int, since_ms: int, end_ms: int) -> list[Execution]:
        client = self.tracker.client
        token = self.tracker.auth_provider.token_for(account)
        if not self.symbols:
            self.symbols = client.fetch_market_symbols()
        result: list[Execution] = []
        cursor = None
        cursors: set[str] = set()
        for _ in range(1000):
            payload = client.get_json("/api/v1/trades", query={
                "account_index": account, "market_type": "perp", "sort_by": "timestamp",
                "sort_dir": "desc", "type": "all", "limit": 100, "cursor": cursor,
            }, headers={"authorization": token})
            _raise_for_lighter_code(payload, "journal execution lookup")
            rows = payload.get("trades")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise JournalError("Lighter execution history returned an unexpected response.")
            parsed = [lighter_execution(row, account, self.symbols) for row in rows]
            result.extend(fill for fill in parsed if since_ms <= fill.time_ms <= end_ms)
            if parsed and min(fill.time_ms for fill in parsed) < since_ms:
                return result
            cursor = payload.get("next_cursor")
            if not cursor:
                if len(rows) == 100:
                    raise JournalError("Lighter returned a full execution page without a cursor; refusing incomplete history.")
                return result
            if cursor in cursors:
                raise JournalError("Lighter execution pagination repeated a cursor; sync will retry.")
            cursors.add(cursor)
        raise JournalError("Lighter execution history exceeds the pagination limit; shorten the lookback.")

    def _bingx(self, since_ms: int, end_ms: int) -> list[Execution]:
        client = self.tracker.bingx_client
        if client is None:
            raise JournalError("BingX is not configured.")
        result: list[Execution] = []
        # Bounded windows cover downtime without exceeding exchange time limits.
        start = since_ms
        while start <= end_ms:
            end = min(start + 86_400_000 - 1, end_ms)
            rows = self._bingx_window(client, start, end)
            occurrences: dict[str, int] = {}
            for raw in rows:
                if not raw.get("tradeId") and not raw.get("id"):
                    # Production fill_orders has no tradeId. Preserve identical
                    # executions using an occurrence number within the complete
                    # timestamp window, not an unstable page/array index.
                    identity = [raw.get(key) for key in (
                        "orderId", "filledTm", "filledTime", "symbol", "side",
                        "positionSide", "volume", "price",
                    )]
                    if not raw.get("orderId") or not (raw.get("filledTm") or raw.get("filledTime")):
                        raise JournalError("BingX execution omitted its stable identity fields.")
                    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                    occurrences[digest] = occurrences.get(digest, 0) + 1
                    raw = {**raw, "_journal_id": f"{digest}:{occurrences[digest]}"}
                order_id = str(raw.get("orderId") or "")
                order: dict = {}
                if not raw.get("positionSide"):
                    if not order_id:
                        raise JournalError("BingX execution omitted its order ID.")
                    if order_id not in self.orders:
                        payload = self._bingx_get(client, "/openApi/swap/v2/trade/order", {
                            "symbol": str(raw.get("symbol") or ""), "orderId": order_id,
                        })
                        data = payload.get("data")
                        if isinstance(data, dict) and isinstance(data.get("order"), dict):
                            data = data["order"]
                        if not isinstance(data, dict):
                            raise JournalError("BingX order details returned an unexpected response.")
                        self.orders[order_id] = data
                    order = self.orders[order_id]
                fill = bingx_execution(raw, order)
                if since_ms <= fill.time_ms <= end_ms:
                    result.append(fill)
            start = end + 1
        return result

    def _bingx_window(self, client, start: int, end: int) -> list[dict]:
        payload = self._bingx_get(client, "/openApi/swap/v2/trade/allFillOrders", {
            "tradingUnit": "COIN", "startTs": str(start), "endTs": str(end),
        })
        rows = payload.get("data")
        if isinstance(rows, dict):
            rows = rows.get("fill_orders", rows.get("fills", rows.get("orders")))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise JournalError("BingX execution history returned an unexpected response.")
        # The endpoint has no documented cursor. Split a potentially capped page.
        if len(rows) >= 100:
            if start == end:
                raise JournalError("BingX execution history may be truncated within one millisecond; sync paused.")
            midpoint = (start + end) // 2
            return self._bingx_window(client, start, midpoint) + self._bingx_window(client, midpoint + 1, end)
        return rows
