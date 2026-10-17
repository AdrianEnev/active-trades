from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock

import pytest

from active_trades.exit_chart import Candle
from active_trades.journal_charts import ExitChartService
from active_trades.journal_executions import JournalError
from active_trades.journal_ledger import JournalLedger, JournalTrade
from active_trades.journal_settings import JournalSettings


def fixtures(tmp_path, *, bucket="test-charts"):
    settings = JournalSettings("sheet", tmp_path / "credentials", tmp_path / "journal.sqlite3", chart_bucket=bucket)
    trade = JournalTrade("trade-1", "lighter", 1, "0", "BTC", "long", "BOTH", "", 60_100,
                         closed_ms=179_900, exit_price=D("104"))
    metadata = {"plan_entry": "100.25", "plan_stop": "98.00", "plan_target": "104.75"}
    candles = Mock()
    candles.read.return_value = [Candle(t, D(100), D(105), D(97), D(102)) for t in (0, 60_000, 120_000)]
    publisher = Mock()
    publisher.publish.return_value = "https://storage.googleapis.com/test-charts/image.png"
    ledger = JournalLedger(settings.state_path)
    return settings, trade, metadata, candles, publisher, ledger


def test_render_publish_restart_reuses_exact_prices_and_time_boundaries(tmp_path):
    settings, trade, metadata, candles, publisher, ledger = fixtures(tmp_path)
    service = ExitChartService(settings, None, candles=candles, publisher=publisher)
    try:
        url = service.generate(trade, metadata, ledger)
        from PIL import Image
        with Image.open(metadata["exit_chart_file"]) as picture:
            manifest = picture.info
            assert any("100.25" in str(value) and "104.75" in str(value) for value in manifest.values())
        restarted = ExitChartService(settings, None, candles=Mock(), publisher=Mock())
        assert restarted.generate(trade, ledger.metadata(trade.id), ledger) == url
        restarted.candles.read.assert_not_called()
        restarted.publisher.publish.assert_not_called()
        assert publisher.publish.call_count == 1
        assert candles.read.call_count == 1
    finally:
        ledger.close()


def test_upload_failure_reuses_local_png_on_retry(tmp_path):
    settings, trade, metadata, candles, publisher, ledger = fixtures(tmp_path)
    publisher.publish.side_effect = JournalError("offline")
    service = ExitChartService(settings, None, candles=candles, publisher=publisher)
    try:
        with pytest.raises(JournalError, match="offline"):
            service.generate(trade, metadata, ledger)
        path = Path(ledger.metadata(trade.id)["exit_chart_file"])
        assert path.is_file()
        publisher.publish.side_effect = None
        service.generate(trade, ledger.metadata(trade.id), ledger)
        assert candles.read.call_count == 1
        assert publisher.publish.call_count == 2
    finally:
        ledger.close()


def test_default_delivery_selects_hosted_publisher_without_bucket(tmp_path, monkeypatch):
    settings, trade, metadata, candles, publisher, ledger = fixtures(tmp_path, bucket="")
    factory = Mock(return_value=publisher)
    monkeypatch.setattr("active_trades.chart_storage.QuaxChartPublisher", factory)
    try:
        service = ExitChartService(settings, None, candles=candles)
        service.generate(trade, metadata, ledger)
        factory.assert_called_once_with(settings)
        publisher.publish.assert_called_once()
    finally:
        ledger.close()


def test_default_host_needs_no_bucket_and_destination_change_reuses_png(tmp_path):
    settings, trade, metadata, candles, publisher, ledger = fixtures(tmp_path, bucket="")
    try:
        service = ExitChartService(settings, None, candles=candles, publisher=publisher)
        assert service.generate(trade, metadata, ledger).startswith("https://")
        assert publisher.publish.call_count == 1
        service = ExitChartService(replace(settings, chart_bucket="test-charts"), None,
                                   candles=Mock(), publisher=publisher)
        assert service.generate(trade, ledger.metadata(trade.id), ledger).startswith("https://")
        service.candles.read.assert_not_called()
        assert publisher.publish.call_count == 2
    finally:
        ledger.close()


@pytest.mark.parametrize("change", [
    {"plan_target": None}, {"plan_stop": "NaN"}, {"plan_entry": "0"},
    {"plan_stop": "101"}, {"plan_target": "99"},
])
def test_missing_or_invalid_original_plan_does_not_fabricate_prices(tmp_path, change):
    settings, trade, metadata, candles, publisher, ledger = fixtures(tmp_path)
    metadata.update(change)
    try:
        service = ExitChartService(settings, None, candles=candles, publisher=publisher)
        with pytest.raises(JournalError, match="original entry, SL and TP"):
            service.generate(trade, metadata, ledger)
        candles.read.assert_not_called()
        publisher.publish.assert_not_called()
    finally:
        ledger.close()


@pytest.mark.parametrize("bucket", ["gs://bucket", "https://bucket", "Bad Name", "a"])
def test_chart_bucket_configuration_rejects_invalid_names(tmp_path, bucket):
    with pytest.raises(ValueError, match="CHART_BUCKET"):
        JournalSettings("sheet", tmp_path / "credentials", tmp_path / "state", chart_bucket=bucket)
