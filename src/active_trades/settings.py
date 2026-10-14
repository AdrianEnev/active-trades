from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_BASE_URL = "https://mainnet.zklighter.elliot.ai"
DEFAULT_BINGX_BASE_URL = "https://open-api.bingx.com"
DEFAULT_EXPLORER_URL = "https://explorer.elliot.ai/api"
DEFAULT_REFRESH_SECONDS = 15.0
DEFAULT_PNL_REFRESH_SECONDS = 0.5
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_TRADE_LOOKBACK_LIMIT = 100
DEFAULT_AUTH_TOKEN_TTL_SECONDS = 60 * 60


@dataclass(frozen=True)
class Settings:
    base_url: str
    explorer_url: str
    account_index: int
    api_key_index: int | None
    api_private_key: str | None
    read_only_auth_token: str | None
    refresh_seconds: float
    timeout_seconds: int
    state_path: Path
    explicit_account_indexes: tuple[int, ...]
    trade_lookback_limit: int
    auth_token_ttl_seconds: int
    fetch_orders: bool = True
    infer_open_dates: bool = True
    pnl_refresh_seconds: float = DEFAULT_PNL_REFRESH_SECONDS
    bingx_api_key: str | None = field(default=None, repr=False)
    bingx_api_secret: str | None = field(default=None, repr=False)
    bingx_base_url: str = DEFAULT_BINGX_BASE_URL


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def settings_from_env(
    *,
    refresh_seconds: float | None = None,
    pnl_refresh_seconds: float | None = None,
    account_index: int | None = None,
    account_indexes: tuple[int, ...] | None = None,
    state_path: str | Path | None = None,
    timeout_seconds: int | None = None,
    fetch_orders: bool = True,
    infer_open_dates: bool = True,
) -> Settings:
    if refresh_seconds is not None and (not math.isfinite(refresh_seconds) or refresh_seconds <= 0):
        raise ValueError("--refresh must be greater than 0.")
    if pnl_refresh_seconds is not None and (
        not math.isfinite(pnl_refresh_seconds) or pnl_refresh_seconds < 0.5
    ):
        raise ValueError("--pnl-refresh must be a finite number of at least 0.5 seconds.")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("--timeout must be greater than 0.")

    resolved_account_index = account_index
    if resolved_account_index is None:
        resolved_account_index = _optional_int(os.getenv("LIGHTER_ACCOUNT_INDEX"), "LIGHTER_ACCOUNT_INDEX")
    if resolved_account_index is None:
        raise ValueError("LIGHTER_ACCOUNT_INDEX is required in the environment or via --account-index.")

    env_account_indexes = _parse_account_indexes(
        os.getenv("LIGHTER_ACCOUNT_INDEXES") or os.getenv("LIGHTER_SUB_ACCOUNT_INDEXES")
    )
    explicit_account_indexes = account_indexes if account_indexes is not None else env_account_indexes

    if state_path is None:
        state_path = os.getenv("ACTIVE_TRADES_STATE_PATH")
    resolved_state_path = (
        Path(state_path).expanduser()
        if state_path is not None
        else project_root() / "state" / "position_state.json"
    )
    resolved_pnl_refresh = (
        pnl_refresh_seconds
        if pnl_refresh_seconds is not None
        else _optional_float(os.getenv("ACTIVE_TRADES_PNL_REFRESH_SECONDS"), "ACTIVE_TRADES_PNL_REFRESH_SECONDS")
        or DEFAULT_PNL_REFRESH_SECONDS
    )
    if resolved_pnl_refresh < 0.5:
        raise ValueError("ACTIVE_TRADES_PNL_REFRESH_SECONDS must be at least 0.5 seconds.")

    return Settings(
        base_url=(os.getenv("LIGHTER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        explorer_url=(os.getenv("LIGHTER_EXPLORER_URL") or DEFAULT_EXPLORER_URL).rstrip("/"),
        account_index=resolved_account_index,
        api_key_index=_optional_int(os.getenv("LIGHTER_API_KEY_INDEX"), "LIGHTER_API_KEY_INDEX"),
        api_private_key=os.getenv("LIGHTER_API_PRIVATE_KEY") or None,
        read_only_auth_token=os.getenv("LIGHTER_READ_ONLY_AUTH_TOKEN") or None,
        refresh_seconds=(
            refresh_seconds
            if refresh_seconds is not None
            else _optional_float(os.getenv("ACTIVE_TRADES_REFRESH_SECONDS"), "ACTIVE_TRADES_REFRESH_SECONDS")
            or DEFAULT_REFRESH_SECONDS
        ),
        timeout_seconds=(
            timeout_seconds
            if timeout_seconds is not None
            else _optional_int(os.getenv("ACTIVE_TRADES_TIMEOUT_SECONDS"), "ACTIVE_TRADES_TIMEOUT_SECONDS")
            or DEFAULT_TIMEOUT_SECONDS
        ),
        state_path=resolved_state_path,
        explicit_account_indexes=explicit_account_indexes,
        trade_lookback_limit=_optional_int(
            os.getenv("ACTIVE_TRADES_TRADE_LOOKBACK_LIMIT"),
            "ACTIVE_TRADES_TRADE_LOOKBACK_LIMIT",
        )
        or DEFAULT_TRADE_LOOKBACK_LIMIT,
        auth_token_ttl_seconds=_optional_int(
            os.getenv("ACTIVE_TRADES_AUTH_TOKEN_TTL_SECONDS"),
            "ACTIVE_TRADES_AUTH_TOKEN_TTL_SECONDS",
        )
        or DEFAULT_AUTH_TOKEN_TTL_SECONDS,
        fetch_orders=fetch_orders,
        infer_open_dates=infer_open_dates,
        pnl_refresh_seconds=resolved_pnl_refresh,
        bingx_api_key=(os.getenv("BINGX_API_KEY") or "").strip() or None,
        bingx_api_secret=(os.getenv("BINGX_API_SECRET") or "").strip() or None,
        bingx_base_url=(os.getenv("BINGX_BASE_URL") or DEFAULT_BINGX_BASE_URL).rstrip("/"),
    )


def _optional_int(value: str | None, name: str) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def _optional_float(value: str | None, name: str) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be greater than 0.")
    return parsed


def _parse_account_indexes(value: str | None) -> tuple[int, ...]:
    if value is None or value.strip() == "":
        return ()
    indexes: list[int] = []
    for raw_part in value.replace(";", ",").split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            indexes.append(int(part))
        except ValueError as exc:
            raise ValueError("LIGHTER_ACCOUNT_INDEXES must contain comma-separated integers.") from exc
    return tuple(dict.fromkeys(indexes))
