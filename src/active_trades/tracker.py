from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from active_trades.bingx_api import (
    BingxApiError,
    BingxAuthUnavailable,
    BingxRestClient,
    limit_orders_from_dicts as bingx_limit_orders_from_dicts,
    positions_from_dicts as bingx_positions_from_dicts,
    stop_orders_from_dicts as bingx_stop_orders_from_dicts,
)
from active_trades.lighter_api import (
    AuthProvider,
    AuthUnavailable,
    LighterApiError,
    LighterRestClient,
    account_info_from_dict,
    limit_orders_from_dicts,
    positions_from_account,
    stop_orders_from_dicts,
    trades_from_dicts,
    _timestamp_to_datetime,
)
from active_trades.models import (
    AccountInfo,
    AccountSnapshot,
    ActiveTrade,
    LimitOrder,
    Position,
    StopOrder,
    TrackerSnapshot,
    now_utc,
)
from active_trades.risk import find_protective_stop, infer_opened_at_from_trades, risk_usd
from active_trades.settings import Settings
from active_trades.state import PositionStateStore
from active_trades.pnl_stream import StreamPositions


class ActiveTradesTracker:
    def __init__(
        self,
        settings: Settings,
        *,
        client: LighterRestClient | None = None,
        auth_provider: AuthProvider | None = None,
        state_store: PositionStateStore | None = None,
        bingx_client: BingxRestClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or LighterRestClient(
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
        )
        self.auth_provider = auth_provider or AuthProvider(
            base_url=settings.base_url,
            api_private_key=settings.api_private_key,
            api_key_index=settings.api_key_index,
            read_only_auth_token=settings.read_only_auth_token,
            default_account_index=settings.account_index,
            ttl_seconds=settings.auth_token_ttl_seconds,
        )
        self.state_store = state_store or PositionStateStore(settings.state_path)
        self.bingx_client = bingx_client
        if self.bingx_client is None and (settings.bingx_api_key or settings.bingx_api_secret):
            self.bingx_client = BingxRestClient(
                base_url=settings.bingx_base_url,
                api_key=settings.bingx_api_key,
                api_secret=settings.bingx_api_secret,
                timeout_seconds=settings.timeout_seconds,
            )
        self._market_symbols: dict[int, str] = {}

    def discover_accounts(self) -> tuple[list[AccountInfo], list[str]]:
        errors: list[str] = []
        if self.settings.explicit_account_indexes:
            return (
                _friendly_account_labels(
                    [AccountInfo(index=index) for index in self.settings.explicit_account_indexes],
                    self.settings.account_index,
                ),
                errors,
            )

        base_account = self._fetch_account_info(self.settings.account_index)
        if not base_account.l1_address:
            return (
                [base_account],
                [f"Account {base_account.index} did not include an L1 address; using base account only."],
            )

        try:
            raw_accounts = self.client.fetch_sub_accounts_by_l1_address(base_account.l1_address)
        except LighterApiError as exc:
            return (
                [base_account],
                [f"Could not discover sub-accounts from L1 address; using base account only: {exc}"],
            )

        accounts_by_index = {base_account.index: base_account}
        for raw in raw_accounts:
            try:
                account = account_info_from_dict(raw)
            except LighterApiError as exc:
                errors.append(str(exc))
                continue
            accounts_by_index[account.index] = account

        return (
            _friendly_account_labels(
                [accounts_by_index[index] for index in sorted(accounts_by_index)],
                self.settings.account_index,
            ),
            errors,
        )

    def collect_snapshot(self) -> TrackerSnapshot:
        collected_at = now_utc()
        account_sections: list[AccountSnapshot] = []
        active_position_keys: set[str] = set()
        global_errors: list[str] = []
        positions_complete = True

        try:
            discovered_accounts, discover_errors = self.discover_accounts()
        except LighterApiError as exc:
            positions_complete = False
            global_errors.append(f"Lighter account discovery failed: {exc}")
            discovered_accounts, discover_errors = [], []

        global_errors.extend(discover_errors)
        for discovered_account in discovered_accounts:
            account_errors: list[str] = []
            try:
                raw_account = self.client.fetch_account_by_index(discovered_account.index, active_only=True)
                account = account_info_from_dict(raw_account, fallback_index=discovered_account.index)
                if discovered_account.display_name and not account.display_name:
                    account = replace(account, display_name=discovered_account.display_name)
                positions = positions_from_account(raw_account, account.index)
            except LighterApiError as exc:
                positions_complete = False
                account_sections.append(
                    AccountSnapshot(
                        account=discovered_account,
                        trades=(),
                        errors=(f"Could not fetch account positions: {exc}",),
                        orders_state="unavailable",
                        positions_state="unavailable",
                    )
                )
                continue

            stop_orders, limit_orders, auth_token, orders_state = self._fetch_orders(
                account.index, positions, account_errors
            )
            trades: list[ActiveTrade] = []
            for row_number, position in enumerate(positions, start=1):
                active_position_keys.add(position.key)
                if orders_state != "available":
                    stop_order = None
                    risk_state = orders_state
                    stop_note = "Auth needed" if orders_state == "auth-needed" else ""
                else:
                    stop_order, risk_state, stop_note = find_protective_stop(position, stop_orders)
                opened_at, opened_source, open_date_note = self._resolve_opened_at(
                    position=position,
                    stop_order=stop_order,
                    auth_token=auth_token,
                    collected_at=collected_at,
                )
                risk_value = risk_usd(position, stop_order)
                note_parts = [part for part in (stop_note, open_date_note) if part]
                trades.append(
                    ActiveTrade(
                        row_number=row_number,
                        position=position,
                        opened_at=opened_at,
                        opened_at_source=opened_source,
                        stop_loss=stop_order.trigger_price if stop_order is not None else None,
                        risk_usd=risk_value,
                        risk_state=risk_state,
                        risk_note=" ".join(note_parts),
                    )
                )

            account_sections.append(
                AccountSnapshot(
                    account=account,
                    trades=tuple(trades),
                    errors=tuple(account_errors),
                    limit_orders=tuple(limit_orders),
                    orders_state=orders_state,
                    pnl_collected_at=collected_at,
                    stop_orders=tuple(stop_orders),
                )
            )

        if self.bingx_client is not None:
            bingx_section = self._collect_bingx(collected_at)
            account_sections.append(bingx_section)
            active_position_keys.update(trade.position.key for trade in bingx_section.trades)
            positions_complete = positions_complete and bingx_section.positions_state == "available"

        if positions_complete:
            self.state_store.prune(active_position_keys)
        self.state_store.save()
        return TrackerSnapshot(
            collected_at=collected_at,
            accounts=tuple(account_sections),
            errors=tuple(global_errors),
            pnl_collected_at=collected_at,
            accounts_complete=not global_errors,
        )

    def _collect_bingx(self, collected_at: datetime) -> AccountSnapshot:
        assert self.bingx_client is not None
        account = AccountInfo(index=0, name="Main", exchange="bingx")
        errors: list[str] = []
        positions: list[Position] = []
        positions_state = "available"
        try:
            positions = bingx_positions_from_dicts(self.bingx_client.fetch_positions())
        except BingxAuthUnavailable as exc:
            positions_state = "auth-needed"
            errors.append(f"Could not fetch account positions: {exc}")
        except BingxApiError as exc:
            positions_state = "unavailable"
            errors.append(f"Could not fetch account positions: {exc}")

        stops: list[StopOrder] = []
        limits: list[LimitOrder] = []
        orders_state = "disabled"
        if self.settings.fetch_orders:
            try:
                raw_orders = self.bingx_client.fetch_active_orders()
                stops = bingx_stop_orders_from_dicts(raw_orders)
                limits = bingx_limit_orders_from_dicts(raw_orders)
                orders_state = "available"
            except BingxAuthUnavailable as exc:
                orders_state = "auth-needed"
                errors.append(f"Could not fetch active orders: {exc}")
            except BingxApiError as exc:
                orders_state = "unavailable"
                errors.append(f"Could not fetch active orders: {exc}")

        trades: list[ActiveTrade] = []
        for number, position in enumerate(positions, start=1):
            if orders_state == "available":
                stop_order, risk_state, stop_note = find_protective_stop(position, stops)
            else:
                stop_order, risk_state, stop_note = None, orders_state, ""
            raw = position.raw or {}
            opened_at = next((
                parsed for name in ("createTime", "createdTime", "createdAt")
                if (parsed := _timestamp_to_datetime(raw.get(name))) is not None
            ), None)
            if opened_at is not None:
                self.state_store.remember(position, opened_at)
                opened_source, open_note = "exchange", ""
            else:
                opened_at, opened_source, open_note = self._resolve_opened_at(
                    position=position,
                    stop_order=stop_order,
                    auth_token=None,
                    collected_at=collected_at,
                )
            trades.append(ActiveTrade(
                row_number=number,
                position=position,
                opened_at=opened_at,
                opened_at_source=opened_source,
                stop_loss=stop_order.trigger_price if stop_order is not None else None,
                risk_usd=risk_usd(position, stop_order),
                risk_state=risk_state,
                risk_note=" ".join(part for part in (stop_note, open_note) if part),
            ))
        return AccountSnapshot(
            account=account,
            trades=tuple(trades),
            errors=tuple(errors),
            limit_orders=tuple(limits),
            orders_state=orders_state,
            pnl_collected_at=collected_at if positions_state == "available" else None,
            positions_state=positions_state,
            stop_orders=tuple(stops),
        )

    def _fetch_account_info(self, account_index: int) -> AccountInfo:
        raw_account = self.client.fetch_account_by_index(account_index, active_only=False)
        return account_info_from_dict(raw_account, fallback_index=account_index)

    def _fetch_orders(
        self,
        account_index: int,
        positions: list[Position],
        errors: list[str],
    ) -> tuple[list[StopOrder], list[LimitOrder], str | None, str]:
        if not self.settings.fetch_orders:
            return [], [], None, "disabled"
        try:
            auth_token = self.auth_provider.token_for(account_index)
            raw_orders = self.client.fetch_active_orders(account_index, auth_token)
        except AuthUnavailable as exc:
            errors.append(str(exc))
            return [], [], None, "auth-needed"
        except LighterApiError as exc:
            errors.append(f"Could not fetch active orders: {exc}")
            return [], [], None, "unavailable"
        self._market_symbols.update({position.market_id: position.symbol for position in positions})
        limits = limit_orders_from_dicts(
            raw_orders, account_index=account_index, symbols=self._market_symbols
        )
        if any(order.market_id not in self._market_symbols for order in limits):
            try:
                self._market_symbols.update(self.client.fetch_market_symbols())
            except LighterApiError as exc:
                errors.append(f"Could not fetch market names; showing market IDs: {exc}")
            limits = limit_orders_from_dicts(
                raw_orders, account_index=account_index, symbols=self._market_symbols
            )
        return stop_orders_from_dicts(raw_orders, account_index=account_index), limits, auth_token, "available"

    def apply_live_positions(
        self,
        snapshot: TrackerSnapshot,
        updates: Mapping[int, StreamPositions],
    ) -> TrackerSnapshot:
        """Merge stream state without REST requests, history reads or state-file writes."""
        accounts: list[AccountSnapshot] = []
        latest_at = snapshot.pnl_collected_at or snapshot.collected_at
        for section in snapshot.accounts:
            if section.account.exchange != "lighter":
                accounts.append(section)
                continue
            update = updates.get(section.account.index)
            if update is None or update.received_at <= (
                section.pnl_collected_at or snapshot.collected_at
            ):
                accounts.append(section)
                continue
            previous = {trade.position.key: trade for trade in section.trades}
            trades: list[ActiveTrade] = []
            for row_number, position in enumerate(update.positions, start=1):
                old = previous.get(position.key)
                if old is not None:
                    risk_value = old.risk_usd
                    risk_state = old.risk_state
                    risk_note = old.risk_note
                    if old.stop_loss is not None:
                        per_unit = (
                            position.avg_entry_price - old.stop_loss
                            if position.side == "long"
                            else old.stop_loss - position.avg_entry_price
                        )
                        risk_value = max(per_unit, 0) * position.size
                        if risk_state in {"at-risk", "locked"}:
                            risk_state = "at-risk" if per_unit > 0 else "locked"
                            risk_note = risk_note.replace("SL beyond breakeven", "").strip()
                            if risk_state == "locked":
                                risk_note = f"SL beyond breakeven {risk_note}".strip()
                    trades.append(replace(
                        old, row_number=row_number, position=position,
                        risk_usd=risk_value, risk_state=risk_state, risk_note=risk_note,
                    ))
                else:
                    trades.append(ActiveTrade(
                        row_number=row_number,
                        position=position,
                        opened_at=update.received_at,
                        opened_at_source="first-seen",
                        stop_loss=None,
                        risk_usd=None,
                        risk_state=(section.orders_state if section.orders_state != "available" else "unavailable"),
                    ))
            accounts.append(replace(
                section, trades=tuple(trades), pnl_collected_at=update.received_at,
                positions_state="available",
            ))
            latest_at = max(latest_at, update.received_at)
        return replace(snapshot, accounts=tuple(accounts), pnl_collected_at=latest_at)

    def _resolve_opened_at(
        self,
        *,
        position: Position,
        stop_order: StopOrder | None,
        auth_token: str | None,
        collected_at: datetime,
    ) -> tuple[datetime | None, str, str]:
        stored_opened_at = self.state_store.opened_at_for(position)
        if stored_opened_at is not None:
            return stored_opened_at, "state", ""

        inferred_opened_at = None
        if self.settings.infer_open_dates and auth_token is not None:
            try:
                raw_trades = self.client.fetch_trades(
                    account_index=position.account_index,
                    market_id=position.market_id,
                    authorization=auth_token,
                    limit=self.settings.trade_lookback_limit,
                )
                inferred_opened_at = infer_opened_at_from_trades(
                    position,
                    trades_from_dicts(
                        raw_trades,
                        account_index=position.account_index,
                        market_id=position.market_id,
                    ),
                )
            except LighterApiError:
                inferred_opened_at = None

        if inferred_opened_at is not None:
            self.state_store.remember(position, inferred_opened_at)
            return inferred_opened_at, "trades", ""

        if stop_order is not None and stop_order.created_at is not None:
            self.state_store.remember(position, stop_order.created_at)
            return stop_order.created_at, "stop-order", ""

        self.state_store.remember(position, collected_at)
        return collected_at, "first-seen", "Open time is first seen by this tracker."


def _friendly_account_labels(accounts: list[AccountInfo], base_account_index: int) -> list[AccountInfo]:
    sub_number = 0
    friendly_accounts: list[AccountInfo] = []
    for account in accounts:
        if account.name.strip() or account.description.strip() or account.display_name.strip():
            friendly_accounts.append(account)
            continue
        if account.index == base_account_index:
            friendly_accounts.append(replace(account, display_name="Main"))
            continue
        sub_number += 1
        friendly_accounts.append(replace(account, display_name=f"Sub {sub_number}"))
    return friendly_accounts
