from __future__ import annotations

import hashlib
import hmac
import json
import ssl
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from active_trades.models import LimitOrder, Position, StopOrder


class BingxApiError(RuntimeError):
    """A BingX read failed with a message safe to display in the terminal."""


class BingxAuthUnavailable(BingxApiError):
    """BingX credentials are absent, invalid, or cannot access the account."""


_AUTH_ERROR_MESSAGES = {
    100001: "signature verification failed; check BINGX_API_SECRET.",
    100004: "the API key cannot read this account; check its permissions.",
    100412: "the request signature was rejected.",
    100413: "the API key was rejected; check BINGX_API_KEY.",
    100419: "this IP address is outside the API key's whitelist.",
}
_ERROR_MESSAGES = {
    100410: "request rate limit exceeded; reduce the refresh frequency.",
    100421: "request timestamp rejected; synchronize the system clock.",
    100500: "the service is busy; retry later.",
    109400: "request parameters were rejected.",
    109500: "the service returned an internal error; retry later.",
}
_ACTIVE_ORDER_STATUSES = {"", "NEW", "PENDING", "PARTIALLY_FILLED"}
_FALLBACK_BASE_URLS = {
    "https://open-api.bingx.com": "https://open-api.bingx.pro",
    "https://open-api-vst.bingx.com": "https://open-api-vst.bingx.pro",
}


class BingxRestClient:
    """Read USDT/USDC perpetual positions and orders using BingX OpenAPI."""

    def __init__(
        self,
        *,
        api_key: str | None,
        api_secret: str | None,
        base_url: str = "https://open-api.bingx.com",
        timeout_seconds: int = 15,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout_seconds = timeout_seconds

    def fetch_positions(self) -> list[dict[str, Any]]:
        payload = self._get_json("/openApi/swap/v2/user/positions")
        return _objects_from_payload(payload, "positions")

    def fetch_active_orders(self) -> list[dict[str, Any]]:
        payload = self._get_json("/openApi/swap/v2/trade/openOrders")
        return _objects_from_payload(payload, "orders")

    def _get_json(self, path: str) -> dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise BingxAuthUnavailable(
                "Add both BINGX_API_KEY and BINGX_API_SECRET to read BingX positions and limit orders."
            )

        base_urls = [self.base_url]
        fallback = _FALLBACK_BASE_URLS.get(self.base_url)
        if fallback:
            base_urls.append(fallback)
        for attempt, base_url in enumerate(base_urls):
            # These endpoints need no business parameters: omitting symbol
            # returns all markets. Sign the sorted, unencoded string, using a
            # fresh timestamp for each request (including the domain fallback).
            params = {"recvWindow": "5000", "timestamp": str(int(time.time() * 1000))}
            canonical = "&".join(f"{key}={params[key]}" for key in sorted(params))
            signature = hmac.new(
                self.api_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            query = urlencode({**params, "signature": signature})
            url = f"{base_url}{path}?{query}"
            try:
                request = Request(
                    url,
                    method="GET",
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "active-trades/0.1",
                        "X-BX-APIKEY": self.api_key,
                        "X-SOURCE-KEY": "BX-AI-SKILL",
                    },
                )
                with urlopen(request, timeout=self.timeout_seconds, context=_ssl_context(url)) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                # An upstream body or transport reason may echo the signed
                # URL. Never include it or chain its exception in a user error.
                # HTTP/business failures must not retry against the fallback.
                if exc.code in {401, 403}:
                    raise BingxAuthUnavailable(f"HTTP {exc.code} from BingX; check API access and IP restrictions.") from None
                raise BingxApiError(f"HTTP {exc.code} from BingX.") from None
            except TimeoutError:
                if attempt + 1 < len(base_urls):
                    continue
                raise BingxApiError("BingX request timed out.") from None
            except (URLError, OSError):
                if attempt + 1 < len(base_urls):
                    continue
                raise BingxApiError("BingX request failed; check the connection and API base URL.") from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise BingxApiError("BingX returned invalid JSON.") from None
            except ValueError:
                raise BingxApiError("BingX request could not be constructed; check the API configuration.") from None

            if not isinstance(payload, dict):
                raise BingxApiError("BingX returned a non-object JSON response.")
            _raise_for_bingx_code(payload)
            return payload
        raise BingxApiError("BingX request failed.")


def positions_from_dicts(
    raw_positions: list[dict[str, Any]], *, account_index: int = 0
) -> list[Position]:
    positions: list[Position] = []
    for raw in raw_positions:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "").strip()
        size = _decimal(raw.get("positionAmt"))
        entry = _first_decimal(raw, "avgPrice", "entryPrice")
        if not symbol or size is None or size == 0 or entry is None or entry <= 0:
            continue
        position_side = str(raw.get("positionSide") or "").strip().upper()
        if position_side in {"LONG", "SHORT"}:
            side = "long" if position_side == "LONG" else "short"
        elif position_side in {"", "BOTH"}:
            side = "long" if size > 0 else "short"
        else:
            continue
        positions.append(
            Position(
                account_index=account_index,
                market_id=symbol,
                symbol=symbol,
                side=side,
                size=abs(size),
                avg_entry_price=entry,
                position_value=_decimal(raw.get("positionValue")),
                unrealized_pnl=_decimal(raw.get("unrealizedProfit")),
                raw=raw,
                exchange="bingx",
                position_id=_identifier(raw, "positionId", "positionID"),
            )
        )
    return positions


def limit_orders_from_dicts(
    raw_orders: list[dict[str, Any]], *, account_index: int = 0
) -> list[LimitOrder]:
    """Parse unfilled normal limits, excluding conditional stop and TP orders."""
    orders: list[LimitOrder] = []
    for raw in raw_orders:
        if not isinstance(raw, dict) or _text(raw, "type") != "LIMIT" or not _is_active(raw):
            continue
        symbol = str(raw.get("symbol") or "").strip()
        side = _text(raw, "side").lower()
        price = _decimal(raw.get("price"))
        remaining = _remaining_amount(raw)
        if (
            not symbol
            or side not in {"buy", "sell"}
            or price is None
            or price <= 0
            or remaining is None
            or remaining <= 0
        ):
            continue
        stop_loss, stop_loss_quantity = _attached_stop_loss(raw.get("stopLoss"))
        orders.append(
            LimitOrder(
                account_index=account_index,
                market_id=symbol,
                symbol=symbol,
                side=side,
                price=price,
                remaining_base_amount=remaining,
                created_at=_order_created_at(raw),
                order_id=_identifier(raw, "orderID", "orderId"),
                reduce_only=_is_reducing_order(raw),
                raw=raw,
                exchange="bingx",
                stop_loss=stop_loss,
                stop_loss_quantity=stop_loss_quantity,
            )
        )
    return orders


def stop_orders_from_dicts(
    raw_orders: list[dict[str, Any]], *, account_index: int = 0
) -> list[StopOrder]:
    orders: list[StopOrder] = []
    for raw in raw_orders:
        if not isinstance(raw, dict) or _text(raw, "type") not in {"STOP_MARKET", "STOP"} or not _is_active(raw):
            continue
        symbol = str(raw.get("symbol") or "").strip()
        side = _text(raw, "side").lower()
        trigger = _decimal(raw.get("stopPrice"))
        remaining = _remaining_amount(raw)
        close_position = _bool(raw.get("closePosition"))
        if (
            not symbol
            or side not in {"buy", "sell"}
            or trigger is None
            or trigger <= 0
            or (remaining is not None and remaining <= 0 and not close_position)
        ):
            continue
        orders.append(
            StopOrder(
                account_index=account_index,
                market_id=symbol,
                side=side,
                trigger_price=trigger,
                remaining_base_amount=None if close_position else remaining,
                created_at=_order_created_at(raw),
                order_id=_identifier(raw, "orderID", "orderId"),
                order_type="stop-loss" if _text(raw, "type") == "STOP_MARKET" else "stop-loss-limit",
                reduce_only=_is_reducing_order(raw),
                status="in-progress" if _text(raw, "status") == "PARTIALLY_FILLED" else "open",
                raw=raw,
                exchange="bingx",
            )
        )
    return orders


def _raise_for_bingx_code(payload: dict[str, Any]) -> None:
    try:
        code = int(payload["code"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise BingxApiError("BingX response omitted a valid status code.") from None
    if code == 0:
        return
    if code in _AUTH_ERROR_MESSAGES:
        raise BingxAuthUnavailable(f"BingX error {code}: {_AUTH_ERROR_MESSAGES[code]}")
    detail = _ERROR_MESSAGES.get(code, "the account read was rejected.")
    raise BingxApiError(f"BingX error {code}: {detail}")


def _objects_from_payload(payload: dict[str, Any], name: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get(name)
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise BingxApiError(f"BingX {name} lookup returned an unexpected payload.")
    return data


def _text(raw: dict[str, Any], name: str) -> str:
    return str(raw.get(name) or "").strip().upper()


def _identifier(raw: dict[str, Any], *names: str) -> str:
    for name in names:
        value = raw.get(name)
        if value is not None and value != "":
            return str(value)
    return ""


def _is_active(raw: dict[str, Any]) -> bool:
    return _text(raw, "status") in _ACTIVE_ORDER_STATUSES


def _bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1"}


def _is_reducing_order(raw: dict[str, Any]) -> bool:
    position_side = _text(raw, "positionSide")
    side = _text(raw, "side")
    if position_side in {"LONG", "SHORT"}:
        # reduceOnly is unavailable in Hedge Mode. The hedge direction and
        # order direction together explicitly identify a closing order.
        return (position_side, side) in {("LONG", "SELL"), ("SHORT", "BUY")}
    return _bool(raw.get("reduceOnly")) or _bool(raw.get("closePosition"))


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _first_decimal(raw: dict[str, Any], *names: str) -> Decimal | None:
    for name in names:
        value = _decimal(raw.get(name))
        if value is not None:
            return value
    return None


def _remaining_amount(raw: dict[str, Any]) -> Decimal | None:
    original = _first_decimal(raw, "origQty", "quantity")
    if original is None:
        return None
    executed_raw = raw.get("executedQty")
    executed = _decimal(executed_raw) if executed_raw is not None and executed_raw != "" else Decimal("0")
    if original < 0 or executed is None or executed < 0:
        return None
    return original - executed


def _attached_stop_loss(value: Any) -> tuple[Decimal | None, Decimal | None]:
    # openOrders returns the entry's attached SL as an object. Also accept the
    # JSON serialization used when supplying stopLoss to the order endpoint.
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(value, dict) or _text(value, "type") not in {"STOP_MARKET", "STOP"}:
        return None, None
    price = _decimal(value.get("stopPrice"))
    if price is None or price <= 0:
        return None, None
    if "quantity" not in value:
        return price, None
    quantity = _decimal(value["quantity"])
    if quantity is None or quantity < 0:
        return None, None
    # The documented attachment default is zero: follow the entry's size.
    return price, quantity if quantity > 0 else None


def _order_created_at(raw: dict[str, Any]) -> datetime | None:
    # updateTime is a last-amendment timestamp, not the order's creation time.
    for name in ("time", "createTime", "createdAt"):
        value = _timestamp(raw.get(name))
        if value is not None:
            return value
    return None


def _timestamp(value: Any) -> datetime | None:
    amount = _decimal(value)
    if amount is None or amount <= 0:
        return None
    # BingX documents millisecond timestamps; accept seconds for older payloads.
    if amount > 10_000_000_000:
        amount /= 1000
    try:
        return datetime.fromtimestamp(float(amount), UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None


def _ssl_context(url: str) -> ssl.SSLContext | None:
    if not url.startswith("https://"):
        return None
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())
