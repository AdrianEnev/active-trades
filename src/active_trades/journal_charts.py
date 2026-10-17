"""Render closed-position charts once and retain pending image delivery locally."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from active_trades.exit_chart import ExitChartSpec, render_exit_chart
from active_trades.journal_executions import JournalError


class ExitChartService:
    def __init__(self, settings, tracker, *, candles=None, publisher=None, renderer=None):
        self.settings = settings
        self.tracker = tracker
        self.candles = candles
        self.publisher = publisher
        self.renderer = renderer or render_exit_chart

    def generate(self, trade, metadata, ledger) -> str:
        if trade.closed_ms is None:
            raise JournalError("Exit charts require a fully closed position.")
        try:
            entry, stop, target = (Decimal(metadata[key]) for key in ("plan_entry", "plan_stop", "plan_target"))
            direction = 1 if trade.side == "long" else -1
            valid = all(value.is_finite() and value > 0 for value in (entry, stop, target))
            valid = valid and (entry - stop) * direction > 0 and (target - entry) * direction > 0
        except (KeyError, InvalidOperation, TypeError, ValueError):
            valid = False
        if not valid:
            raise JournalError(f"{trade.symbol}: exit chart needs the original entry, SL and TP captured while the position was open.")
        spec = ExitChartSpec(
            symbol=trade.symbol, exchange=trade.exchange, side=trade.side,
            opened_ms=trade.opened_ms, closed_ms=trade.closed_ms,
            entry=entry, stop=stop, target=target, exit_price=trade.exit_price,
            timezone=self.settings.timezone,
        )
        identity = hashlib.sha256(f"{self.settings.spreadsheet_id}/{self.settings.sheet_name}/{trade.id}".encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps({
            "version": 1, "identity": identity, "opened": trade.opened_ms, "closed": trade.closed_ms,
            "entry": str(entry), "stop": str(stop), "target": str(target),
            "exit": str(trade.exit_price), "timezone": self.settings.timezone,
            "symbol": trade.symbol, "exchange": trade.exchange, "side": trade.side,
        }, sort_keys=True).encode()).hexdigest()
        destination = f"gcs:{self.settings.chart_bucket}" if self.settings.chart_bucket else "quax"
        if (metadata.get("exit_chart_fingerprint") == fingerprint and metadata.get("exit_chart_url")
                and metadata.get("exit_chart_destination") == destination):
            return metadata["exit_chart_url"]
        directory = self.settings.state_path.parent / "exit_charts"
        path = directory / f"{identity}-{fingerprint}.png"
        if not path.is_file():
            if self.candles is None:
                from active_trades.chart_candles import CandleReader
                self.candles = CandleReader(self.tracker)
            candles = self.candles.read(trade)
            directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp.png")
            try:
                self.renderer(spec, candles, temporary)
                temporary.replace(path)
            except ImportError:
                raise JournalError('Install chart support with: pip install -e ".[journal]"') from None
            finally:
                temporary.unlink(missing_ok=True)
        metadata.update(exit_chart_file=str(path))
        ledger.remember(trade.id, metadata)
        if self.publisher is None:
            from active_trades.chart_storage import QuaxChartPublisher, GCSChartPublisher
            publisher_type = GCSChartPublisher if self.settings.chart_bucket else QuaxChartPublisher
            self.publisher = publisher_type(self.settings)
        # Stable content keys support cached upload responses across retries.
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        url = self.publisher.publish(path, f"exit-charts/{identity}/{digest}.png")
        metadata.update(exit_chart_fingerprint=fingerprint, exit_chart_url=url,
                        exit_chart_destination=destination)
        ledger.remember(trade.id, metadata)
        return url
