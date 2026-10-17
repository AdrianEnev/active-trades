from __future__ import annotations

import asyncio
import inspect
import json
import os
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from active_trades.models import AccountInfo, LimitOrder, Position, StopOrder, Trade


class LighterApiError(RuntimeError):
    """A Lighter API request failed with a message safe to show in the terminal."""


class AuthUnavailable(LighterApiError):
    """Credentials are missing or could not produce a usable auth token."""


@dataclass(frozen=True)
class CachedAuthToken:
    token: str
    expires_at: float


class AuthProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_private_key: str | None,
        api_key_index: int | None,
        read_only_auth_token: str | None = None,
        default_account_index: int | None = None,
        ttl_seconds: int = 3600,
    ) -> None:
        self.base_url = base_url
        self.api_private_key = api_private_key
        self.api_key_index = api_key_index
        self.read_only_auth_token = read_only_auth_token
        self.default_account_index = default_account_index
        self.ttl_seconds = ttl_seconds
        self._cache: dict[int, CachedAuthToken] = {}

    def token_for(self, account_index: int) -> str:
        account_read_only_token = os.getenv(f"LIGHTER_ACCOUNT_{account_index}_READ_ONLY_AUTH_TOKEN")
        if account_read_only_token:
            return account_read_only_token
        if self.read_only_auth_token:
            return self.read_only_auth_token

        account_api_private_key = os.getenv(f"LIGHTER_ACCOUNT_{account_index}_API_PRIVATE_KEY")
        raw_api_key_index = os.getenv(f"LIGHTER_ACCOUNT_{account_index}_API_KEY_INDEX")
        has_account_key = bool(account_api_private_key and raw_api_key_index)
        if (
            self.default_account_index is not None
            and account_index != self.default_account_index
            and not has_account_key
        ):
            raise AuthUnavailable(
                "Add per-account Lighter credentials or a read-only token to show limit orders and SL/risk here."
            )
        api_private_key = account_api_private_key or self.api_private_key
        api_key_index = _parse_int(raw_api_key_index) if raw_api_key_index else self.api_key_index
        if not api_private_key or api_key_index is None:
            raise AuthUnavailable(
                "Missing Lighter API credentials; limit orders and risk need authenticated order reads."
            )

        cached = self._cache.get(account_index)
        now = time.time()
        if cached is not None and cached.expires_at - now > 60:
            return cached.token

        token = self._create_sdk_auth_token(
            account_index,
            api_private_key=api_private_key,
            api_key_index=api_key_index,
        )
        self._cache[account_index] = CachedAuthToken(
            token=token,
            expires_at=now + max(120, self.ttl_seconds),
        )
        return token

    def _create_sdk_auth_token(
        self,
        account_index: int,
        *,
        api_private_key: str,
        api_key_index: int,
    ) -> str:
        try:
            import lighter
        except ImportError as exc:
            raise AuthUnavailable(
                "lighter-sdk is required for authenticated order reads. Install this project with "
                "`pip install -e .`."
            ) from exc

        async def create_token() -> str:
            signer = lighter.SignerClient(
                url=self.base_url,
                account_index=account_index,
                api_private_keys={api_key_index: api_private_key},
            )
            try:
                auth_token, error = signer.create_auth_token_with_expiry(
                    deadline=self.ttl_seconds,
                    api_key_index=api_key_index,
                )
                if error:
                    raise AuthUnavailable(f"Lighter auth token creation failed: {error}")
                if not auth_token:
                    raise AuthUnavailable("Lighter auth token creation returned an empty token.")
                return str(auth_token)
            finally:
                close = getattr(signer, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result

        return asyncio.run(create_token())


def signing_key_for(
    account_index: int,
    *,
    default_account_index: int | None,
    api_private_key: str | None,
    api_key_index: int | None,
) -> tuple[str, int]:
    """API key pair able to sign transactions for one account; read-only tokens cannot."""
    account_private_key = os.getenv(f"LIGHTER_ACCOUNT_{account_index}_API_PRIVATE_KEY")
    raw_api_key_index = os.getenv(f"LIGHTER_ACCOUNT_{account_index}_API_KEY_INDEX")
    if account_private_key and raw_api_key_index:
        parsed_index = _parse_int(raw_api_key_index)
        if parsed_index is not None:
            return account_private_key, parsed_index
    # API keys are scoped to one account index, so the base pair only signs for the base account.
    if account_index == default_account_index and api_private_key and api_key_index is not None:
        return api_private_key, api_key_index
    raise AuthUnavailable(
        f"Cancelling needs LIGHTER_ACCOUNT_{account_index}_API_PRIVATE_KEY and "
        f"LIGHTER_ACCOUNT_{account_index}_API_KEY_INDEX; a read-only token cannot cancel."
    )


def cancel_orders(
    *,
    base_url: str,
    account_index: int,
    api_private_key: str,
    api_key_index: int,
    market_index: int,
    order_indexes: list[int],
) -> list[str | None]:
    """Submit one signed cancel per order, in order; returns each submission's error or None.

    Acceptance means Lighter queued the transaction, not that the order has left the book.
    """
    try:
        import lighter
    except ImportError as exc:
        raise AuthUnavailable(
            "lighter-sdk is required to cancel orders. Install this project with `pip install -e .`."
        ) from exc

    async def submit() -> list[str | None]:
        signer = lighter.SignerClient(
            url=base_url,
            account_index=account_index,
            api_private_keys={api_key_index: api_private_key},
        )
        errors: list[str | None] = []
        try:
            for order_index in order_indexes:
                try:
                    _tx, response, error = await signer.cancel_order(
                        market_index=market_index, order_index=order_index
                    )
                except Exception as exc:  # The SDK raises its own transport and API exceptions.
                    response, error = None, _last_line(exc)
                if error is None and response is None:
                    error = "no response"
                elif error is None and getattr(response, "code", 200) != 200:
                    error = getattr(response, "message", None) or f"response code {response.code}"
                errors.append(str(error)[:200] if error is not None else None)
        finally:
            close = getattr(signer, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        return errors

    try:
        return asyncio.run(submit())
    except Exception as exc:
        raise LighterApiError(f"Lighter cancel could not be signed or sent: {_last_line(exc)}") from None


def _last_line(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    return lines[-1] if lines else type(exc).__name__


class LighterRestClient:
    def __init__(self, *, base_url: str, timeout_seconds: int = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def fetch_account_by_index(self, account_index: int, *, active_only: bool = True) -> dict[str, Any]:
        accounts = self.fetch_accounts(
            by="index",
            value=str(account_index),
            active_only=active_only,
        )
        if not accounts:
            raise LighterApiError(f"No account returned for index {account_index}.")
        return accounts[0]

    def fetch_accounts(
        self,
        *,
        by: str,
        value: str,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        accounts: list[dict[str, Any]] = []
        while True:
            query: dict[str, Any] = {
                "by": by,
                "value": value,
                "active_only": active_only,
            }
            if cursor:
                query["cursor"] = cursor
            payload = self.get_json("/api/v1/account", query=query)
            _raise_for_lighter_code(payload, "account lookup")
            raw_accounts = payload.get("accounts")
            if isinstance(raw_accounts, list):
                accounts.extend(object_to_dict(account) for account in raw_accounts)
            elif "index" in payload:
                accounts.append(object_to_dict(payload))
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return accounts

    def fetch_sub_accounts_by_l1_address(self, l1_address: str) -> list[dict[str, Any]]:
        cursor: str | None = None
        accounts: list[dict[str, Any]] = []
        while True:
            query = {"l1_address": l1_address}
            if cursor:
                query["cursor"] = cursor
            payload = self.get_json("/api/v1/accountsByL1Address", query=query)
            _raise_for_lighter_code(payload, "sub-account lookup")
            raw_accounts = payload.get("sub_accounts") or []
            accounts.extend(object_to_dict(account) for account in raw_accounts)
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return accounts

    def fetch_active_orders(self, account_index: int, authorization: str) -> list[dict[str, Any]]:
        payload = self.get_json(
            "/api/v1/accountActiveOrders",
            query={"account_index": account_index, "market_type": "perp"},
            headers={"authorization": authorization},
        )
        _raise_for_lighter_code(payload, "active-order lookup")
        orders = payload.get("orders") or []
        if not isinstance(orders, list):
            raise LighterApiError("active-order lookup returned an unexpected payload.")
        return [object_to_dict(order) for order in orders]

    def fetch_market_symbols(self) -> dict[int, str]:
        payload = self.get_json("/api/v1/orderBooks", query={"filter": "perp"})
        _raise_for_lighter_code(payload, "market lookup")
        order_books = payload.get("order_books")
        if not isinstance(order_books, list):
            raise LighterApiError("market lookup returned an unexpected payload.")
        symbols: dict[int, str] = {}
        for value in order_books:
            raw = object_to_dict(value)
            market_id = _int_field(raw, "market_id", "market_index")
            symbol = str(raw.get("symbol") or "").strip()
            if market_id is not None and symbol:
                symbols[market_id] = symbol
        return symbols

    def fetch_inactive_orders(self, account_index: int, authorization: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent filled or cancelled orders, including triggered SL/TP orders."""
        payload = self.get_json(
            "/api/v1/accountInactiveOrders",
            query={"account_index": account_index, "market_type": "perp", "limit": max(1, min(100, limit))},
            headers={"authorization": authorization},
        )
        _raise_for_lighter_code(payload, "inactive-order lookup")
        orders = payload.get("orders") or []
        if not isinstance(orders, list):
            raise LighterApiError("inactive-order lookup returned an unexpected payload.")
        return [object_to_dict(order) for order in orders]

    def fetch_trades(
        self,
        *,
        account_index: int,
        market_id: int | None,
        authorization: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Recent fills for one market, or for every market when market_id is None."""
        payload = self.get_json(
            "/api/v1/trades",
            query={
                "account_index": account_index,
                "market_id": market_id,
                "market_type": "perp",
                "sort_by": "timestamp",
                "sort_dir": "desc",
                "type": "trade",
                "limit": max(1, min(100, limit)),
            },
            headers={"authorization": authorization},
        )
        _raise_for_lighter_code(payload, "trade lookup")
        trades = payload.get("trades") or []
        if not isinstance(trades, list):
            raise LighterApiError("trade lookup returned an unexpected payload.")
        return [object_to_dict(trade) for trade in trades]

    def get_json(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        query_string = f"?{urlencode(_query_params(query or {}))}" if query else ""
        url = f"{self.base_url}{path}{query_string}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "active-trades/0.1",
                **(headers or {}),
            },
        )
        try:
            with urlopen(
                request,
                timeout=self.timeout_seconds,
                context=_ssl_context_for_url(url),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = _safe_error_body(exc)
            detail = f": {body}" if body else ""
            raise LighterApiError(f"HTTP {exc.code} from Lighter{detail}") from exc
        except URLError as exc:
            raise LighterApiError(f"Lighter request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LighterApiError("Lighter request timed out.") from exc
        except json.JSONDecodeError as exc:
            raise LighterApiError("Lighter returned invalid JSON.") from exc

        if not isinstance(payload, dict):
            raise LighterApiError("Lighter returned a non-object JSON response.")
        return payload


def account_info_from_dict(raw: dict[str, Any], *, fallback_index: int | None = None) -> AccountInfo:
    index = _int_field(raw, "index", "account_index")
    if index is None:
        index = fallback_index
    if index is None:
        raise LighterApiError("Account payload did not include an account index.")
    return AccountInfo(
        index=index,
        name=str(raw.get("name") or ""),
        description=str(raw.get("description") or ""),
        display_name=str(raw.get("display_name") or ""),
        l1_address=str(raw.get("l1_address") or ""),
        status=_int_field(raw, "status"),
        collateral=_decimal_or_none(raw.get("collateral")),
        available_balance=_decimal_or_none(raw.get("available_balance")),
        raw=raw,
    )


def positions_from_account(raw: dict[str, Any], account_index: int) -> list[Position]:
    positions: list[Position] = []
    for position_raw in raw.get("positions") or []:
        if not isinstance(position_raw, dict):
            continue
        position = position_from_dict(position_raw, account_index=account_index)
        if position is not None:
            positions.append(position)
    return positions


def position_from_dict(raw: dict[str, Any], *, account_index: int) -> Position | None:
    market_id = _int_field(raw, "market_id", "market_index")
    if market_id is None:
        return None
    raw_size = _decimal_or_none(raw.get("position") if "position" in raw else raw.get("size"))
    avg_entry = _decimal_or_none(raw.get("avg_entry_price") if "avg_entry_price" in raw else raw.get("entry_price"))
    if raw_size is None or raw_size == 0 or avg_entry is None or avg_entry <= 0:
        return None

    sign = _int_field(raw, "sign")
    if sign is None:
        sign = 1 if raw_size > 0 else -1
    if sign == 0:
        return None

    return Position(
        account_index=account_index,
        market_id=market_id,
        symbol=str(raw.get("symbol") or market_id),
        side="long" if sign > 0 else "short",
        size=abs(raw_size),
        avg_entry_price=avg_entry,
        position_value=_decimal_or_none(raw.get("position_value")),
        unrealized_pnl=_decimal_or_none(
            raw.get("unrealized_pnl") if raw.get("unrealized_pnl") is not None else raw.get("pnl")
        ),
        raw=raw,
        realized_pnl=_decimal_or_none(raw.get("realized_pnl")),
    )


def stop_orders_from_dicts(raw_orders: list[dict[str, Any]], *, account_index: int) -> list[StopOrder]:
    orders: list[StopOrder] = []
    for raw in raw_orders:
        order = stop_order_from_dict(raw, account_index=account_index)
        if order is not None:
            orders.append(order)
    return orders


def limit_orders_from_dicts(
    raw_orders: list[dict[str, Any]],
    *,
    account_index: int,
    symbols: dict[int, str] | None = None,
) -> list[LimitOrder]:
    """Parse remaining normal limits, excluding conditional SL/TP and market orders."""
    orders: list[LimitOrder] = []
    for raw in raw_orders:
        if not isinstance(raw, dict) or str(raw.get("type") or "").strip().lower() != "limit":
            continue
        status = str(raw.get("status") or "").strip().lower()
        if status and status not in {"open", "pending", "in-progress"}:
            continue
        market_id = _int_field(raw, "market_index", "market_id")
        price = _decimal_or_none(raw.get("price"))
        remaining = _decimal_or_none(raw.get("remaining_base_amount"))
        if market_id is None or price is None or price <= 0 or remaining is None or remaining <= 0:
            continue
        # Lighter deprecates `side`; SDK objects may default it to buy even for asks.
        is_ask = raw.get("is_ask")
        side = (
            "sell" if is_ask else "buy"
        ) if isinstance(is_ask, bool) else str(raw.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            continue
        symbol = str(raw.get("symbol") or "").strip() or (symbols or {}).get(market_id) or str(market_id)
        order_id = raw.get("order_id")
        if order_id is None or order_id == "":
            order_id = raw.get("order_index")
        orders.append(
            LimitOrder(
                account_index=account_index,
                market_id=market_id,
                symbol=symbol,
                side=side,
                price=price,
                remaining_base_amount=remaining,
                created_at=_timestamp_to_datetime(raw.get("created_at")) or _timestamp_to_datetime(raw.get("timestamp")),
                order_id=str(order_id) if order_id is not None else "",
                reduce_only=raw.get("reduce_only") is True,
                raw=raw,
            )
        )
    return orders


def stop_order_from_dict(raw: dict[str, Any], *, account_index: int) -> StopOrder | None:
    market_id = _int_field(raw, "market_index", "market_id")
    trigger_price = _decimal_or_none(raw.get("trigger_price")) or _decimal_or_none(raw.get("price"))
    if market_id is None or trigger_price is None:
        return None

    # SDK objects may populate the deprecated side field with its buy default.
    is_ask = raw.get("is_ask")
    side = (
        "sell" if is_ask else "buy"
    ) if isinstance(is_ask, bool) else str(raw.get("side") or "").strip().lower()

    return StopOrder(
        account_index=account_index,
        market_id=market_id,
        side=side,
        trigger_price=trigger_price,
        remaining_base_amount=_decimal_or_none(raw.get("remaining_base_amount")),
        created_at=_timestamp_to_datetime(raw.get("created_at") or raw.get("timestamp")),
        order_id=str(raw.get("order_id") or raw.get("order_index") or ""),
        order_type=str(raw.get("type") or "").lower(),
        reduce_only=bool(raw.get("reduce_only")),
        status=str(raw.get("status") or "").lower(),
        raw=raw,
    )


def trade_from_dict(raw: dict[str, Any], *, account_index: int, market_id: int) -> Trade | None:
    raw_market_id = _int_field(raw, "market_id", "market_index")
    if raw_market_id is None or raw_market_id != market_id:
        return None
    size = _decimal_or_none(raw.get("size"))
    price = _decimal_or_none(raw.get("price"))
    if size is None or size <= 0 or price is None:
        return None

    ask_account = _int_field(raw, "ask_account_id", "ask_account_index")
    bid_account = _int_field(raw, "bid_account_id", "bid_account_index")
    if ask_account == account_index:
        signed_delta = -size
    elif bid_account == account_index:
        signed_delta = size
    else:
        return None

    return Trade(
        account_index=account_index,
        market_id=market_id,
        size=size,
        price=price,
        timestamp=_timestamp_to_datetime(raw.get("timestamp")),
        signed_delta=signed_delta,
        raw=raw,
    )


def trades_from_dicts(
    raw_trades: list[dict[str, Any]],
    *,
    account_index: int,
    market_id: int,
) -> list[Trade]:
    trades: list[Trade] = []
    for raw in raw_trades:
        trade = trade_from_dict(raw, account_index=account_index, market_id=market_id)
        if trade is not None:
            trades.append(trade)
    return trades


def object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): _jsonish(item) for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return object_to_dict(data)
    try:
        data = vars(value)
    except TypeError:
        return {}
    return {str(key): _jsonish(item) for key, item in data.items() if not key.startswith("_")}


def _jsonish(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonish(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonish(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonish(to_dict())
    try:
        return {str(key): _jsonish(item) for key, item in vars(value).items() if not key.startswith("_")}
    except TypeError:
        return str(value)


def _raise_for_lighter_code(payload: dict[str, Any], action: str) -> None:
    code = payload.get("code")
    if code is None:
        return
    try:
        if int(code) == 200:
            return
    except (TypeError, ValueError):
        pass
    message = payload.get("message") or f"response code {code}"
    raise LighterApiError(f"Lighter {action} failed: {message}")


def _query_params(values: dict[str, Any]) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            params[key] = "true" if value else "false"
        else:
            params[key] = str(value)
    return params


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _int_field(data: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = data.get(name)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _timestamp_to_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp <= 0:
        return None
    try:
        if timestamp > 10_000_000_000_000_000:
            seconds = timestamp / 1_000_000_000
        elif timestamp > 10_000_000_000_000:
            seconds = timestamp / 1_000_000
        elif timestamp > 10_000_000_000:
            seconds = timestamp / 1000
        else:
            seconds = timestamp
        return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None


def _safe_error_body(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8")[:500]
    except Exception:
        return ""


def _ssl_context_for_url(url: str) -> ssl.SSLContext | None:
    if not url.startswith("https://"):
        return None
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())
