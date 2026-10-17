"""Execution-history collection and retryable Google Sheets delivery."""
from __future__ import annotations

import fcntl
import time
from dataclasses import replace
from decimal import Decimal

from active_trades.bingx_api import positions_from_dicts
from active_trades.google_journal import SheetsJournal
from active_trades.journal_executions import ExecutionReader, JournalError
from active_trades.journal_ledger import JournalLedger, replay
from active_trades.journal_plan import capture_plan
from active_trades.journal_settings import JournalSettings
from active_trades.lighter_api import positions_from_account


def _balances(positions, fills):
    lanes: dict[str, set[str]] = {}
    for fill in fills:
        lanes.setdefault(fill.market, set()).add(fill.lane)
    result = {}
    for position in positions:
        market = str(position.market_id)
        known = lanes.get(market, set())
        if "BOTH" in known and len(known) > 1:
            raise JournalError(f"{position.symbol}: mixed one-way and hedge-mode history needs a separate journal state.")
        lane = "BOTH" if position.exchange == "lighter" or known == {"BOTH"} else position.side.upper()
        has_ids = any(fill.market == market and fill.lane == lane and fill.position_id for fill in fills)
        identity = position.position_id if has_ids else ""
        key = (market, lane, identity)
        if key in result:
            raise JournalError(f"{position.symbol}: separate positions cannot be distinguished by the fill history.")
        result[key] = position.signed_size
    return result


class TradeJournal:
    def __init__(self, settings: JournalSettings, tracker, *, reader=None, writer=None, clock=None) -> None:
        self.settings = settings
        self.tracker = tracker
        self.reader = reader or ExecutionReader(tracker)
        self.writer = writer
        self.clock = clock or (lambda: int(time.time() * 1000))

    def sync(self, snapshot):
        errors = []
        try:
            self.settings.state_path.parent.mkdir(parents=True, exist_ok=True)
            # One writer per local ledger, including overlapping --once processes.
            with self.settings.state_path.with_suffix(".lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise JournalError("Another process is syncing this journal; retrying next refresh.") from None
                ledger = JournalLedger(self.settings.state_path)
                try:
                    errors = self._sync(snapshot, ledger)
                finally:
                    ledger.close()
        except JournalError as exc:
            errors.append(f"Journal: {exc}")
        except Exception:
            # Never propagate credential-bearing transport exceptions to the UI.
            errors.append("Journal: sync failed; local history is retained and updates will retry.")
        return replace(snapshot, errors=(*snapshot.errors, *errors))

    def _sync(self, snapshot, ledger):
        target = f"{self.settings.spreadsheet_id}/{self.settings.sheet_name}"
        if ledger.setting("target", target) != target:
            raise JournalError("Journal state belongs to a different spreadsheet/tab; choose a separate state path.")
        started = int(ledger.setting("started_ms", str(self.clock())))
        errors = []
        ready = []
        for section in snapshot.accounts:
            exchange, account = section.account.exchange, section.account.index
            scope = f"{exchange}:{account}"
            if section.positions_state != "available":
                errors.append(f"Journal: {scope} positions unavailable; account sync deferred.")
                continue
            try:
                state = ledger.scope(scope)
                end = self.clock()
                since = state[0] - 300_000 if state else started - self.settings.lookback_days * 86_400_000
                fills = self.reader.read(exchange, account, since, end)
                merged = {fill.id: fill for fill in ledger.executions(scope)}
                merged.update({fill.id: fill for fill in fills})
                history = list(merged.values())
                positions = [trade.position for trade in section.trades]
                current = _balances(positions, history)
                if state is None:
                    # Verify the initial snapshot did not change during history
                    # collection before deriving the opening balances of the window.
                    fresh = self._fresh_positions(exchange, account)
                    if _balances(fresh, history) != current:
                        raise JournalError(f"{scope} changed while initializing history; retrying next refresh.")
                    initial = dict(current)
                    for fill in history:
                        initial[fill.position_key] = initial.get(fill.position_key, Decimal(0)) - Decimal(fill.delta)
                else:
                    initial = state[1]
                trades = replay(history, initial)
                remaining = {trade.key: trade.signed_quantity for trade in trades if trade.quantity}
                # Positions entirely older than the window have no executions yet.
                for key, value in initial.items():
                    if value and not any(fill.position_key == key for fill in history):
                        remaining[key] = value
                if {k: v for k, v in remaining.items() if v} != {k: v for k, v in current.items() if v}:
                    raise JournalError(f"{scope} fills and open quantities do not yet reconcile; updates deferred.")
                ledger.save(scope, fills, end, initial)
                incomplete = [trade for trade in trades if not trade.complete and trade.quantity]
                if incomplete or any(value and not any(fill.position_key == key for fill in history) for key, value in initial.items()):
                    errors.append(f"Journal: {scope} has positions opened before available history; those rows are deferred. Increase the initial lookback using a new state file.")
                for trade in trades:
                    if not trade.complete or (trade.closed_ms is not None and trade.closed_ms < started):
                        continue
                    metadata = ledger.metadata(trade.id)
                    matching = [row for row in section.trades if str(row.position.market_id) == trade.market
                                and row.position.side == trade.side
                                and (not trade.position_id or row.position.position_id == trade.position_id)]
                    if trade.quantity and len(matching) == 1:
                        trade.current_risk = matching[0].risk_usd
                        if not trade.exited and metadata.get("original_risk") is None and trade.current_risk is not None:
                            metadata["original_risk"] = str(trade.current_risk)
                        capture_plan(trade, matching[0], section, metadata)
                    if not metadata.get("capital") and self.settings.capital is not None:
                        metadata["capital"] = str(self.settings.capital)
                    if metadata.get("original_risk") is not None:
                        trade.original_risk = Decimal(metadata["original_risk"])
                    trade.capital = Decimal(metadata["capital"]) if metadata.get("capital") else None
                    trade.potential_rr = Decimal(metadata["potential_rr"]) if metadata.get("potential_rr") else None
                    ledger.remember(trade.id, metadata)
                    ready.append(trade)
            except JournalError as exc:
                errors.append(f"Journal: {exc}")
            except Exception:
                errors.append(f"Journal: {scope} history read failed; account sync will retry.")
        if ready:
            if self.writer is None:
                from active_trades.journal_charts import ExitChartService
                self.writer = SheetsJournal(self.settings, charts=ExitChartService(self.settings, self.tracker))
            self.writer.sync(ready, ledger)
            if isinstance(self.writer, SheetsJournal):
                errors.extend(self.writer.chart_errors)
        return errors

    def _fresh_positions(self, exchange, account):
        if exchange == "lighter":
            return positions_from_account(self.tracker.client.fetch_account_by_index(account), account)
        return positions_from_dicts(self.tracker.bingx_client.fetch_positions())
