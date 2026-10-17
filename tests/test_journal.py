from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from active_trades.google_journal import GoogleSheetsClient, HEADERS, SheetsJournal
from active_trades.journal import TradeJournal
from active_trades.journal_executions import (
    Execution, ExecutionReader, JournalError, bingx_execution, lighter_execution,
)
from active_trades.journal_ledger import JournalLedger, replay
from active_trades.journal_plan import capture_plan, potential_rr
from active_trades.journal_settings import JournalSettings
from active_trades.models import AccountInfo, AccountSnapshot, ActiveTrade, LimitOrder, Position, StopOrder, TrackerSnapshot


T = int(datetime(2026, 10, 16, 10, tzinfo=UTC).timestamp() * 1000)


def fill(n, delta, price="100", *, time=None, exchange="lighter", account=1, market="0", symbol="BTC", **kw):
    return Execution(f"{exchange}:{account}:{n}", exchange, account, market, symbol,
                     time if time is not None else T + n * 1000, str(delta), str(price), kw.pop("pnl", None), **kw)


def config(tmp_path, **kw):
    return JournalSettings("test_sheet", tmp_path / "credentials.json", tmp_path / "journal.sqlite3", **kw)


def test_long_partial_and_final_exit_keep_one_lifecycle():
    fills = [fill(1, 10), fill(2, -5, "102")]
    trade, = replay(fills, {})
    assert (trade.quantity, trade.exited, trade.pnl, trade.closed_ms) == (D(5), D(5), D(10), None)
    identity = trade.id
    trade, = replay(fills + [fill(3, -5, "98")], {})
    assert trade.id == identity
    assert (trade.quantity, trade.pnl, trade.closed_ms) == (0, 0, T + 3000)


def test_short_partials_and_fee_accounting():
    trade, = replay([fill(1, -10, fee="-1"), fill(2, 5, "98", fee="-0.5")], {})
    assert trade.side == "short"
    assert trade.pnl == D("8.5")


def test_scale_in_uses_remaining_average_cost():
    trade, = replay([fill(1, 10), fill(2, -5, 110), fill(3, 5, 120), fill(4, -10, 115)], {})
    assert trade.entered == 15
    assert trade.entry_price == 110
    assert trade.pnl == 100


def test_reversal_splits_old_and_new_position_and_fees():
    old, new = replay([fill(1, 10), fill(2, -15, 110, fee="-3")], {})
    assert (old.side, old.quantity, old.pnl) == ("long", 0, 98)
    assert (new.side, new.quantity, new.pnl) == ("short", 5, -1)
    assert old.id != new.id


def test_round_trip_between_polls_and_reentry_have_separate_ids():
    trades = replay([fill(1, 1), fill(2, -1, 101), fill(3, 1)], {})
    assert len(trades) == 2
    assert trades[0].closed_ms == T + 2000
    assert trades[1].closed_ms is None


def test_missing_initial_history_is_not_reported_as_complete():
    old, new = replay([fill(1, -5), fill(2, 2)], {("0", "BOTH", ""): D(5)})
    assert not old.complete
    assert new.complete


def test_lighter_quantity_gap_is_rejected():
    with pytest.raises(JournalError, match="quantity gap"):
        replay([fill(1, 5, before="0"), fill(2, -2, before="6")], {})


def test_hedged_long_and_short_are_separate():
    trades = replay([fill(1, 5, lane="LONG"), fill(2, -3, lane="SHORT"), fill(3, -2, lane="LONG")], {})
    assert [(t.side, t.quantity) for t in trades] == [("long", 3), ("short", 3)]


def test_reported_realized_pnl_is_used_when_available():
    trade, = replay([fill(1, 5), fill(2, -2, 110, pnl="19.875")], {})
    assert trade.pnl == D("19.875")
    assert not trade.calculated_pnl


def test_actual_bingx_payload_shape():
    raw = {"_journal_id": "stable", "symbol": "TRX-USDT", "orderId": "12345678901234567890",
           "side": "SELL", "positionSide": "LONG", "volume": "3000", "price": "0.33181",
           "commission": "-0.497715", "currency": "USDT", "filledTm": "2026-10-15T18:27:27.000Z"}
    execution = bingx_execution(raw, {})
    assert execution.delta == "-3000"
    assert execution.fee == "-0.497715"
    assert execution.pnl is None
    assert execution.time_ms == 1792088847000


def test_lighter_parser_uses_queried_account_role_and_real_before_size():
    raw = {"trade_id_str": "9007199254740999", "market_id": 0, "market_symbol": "BTC",
           "ask_account_id": 1, "bid_account_id": 2, "size": "5", "price": "100",
           "is_maker_ask": True, "maker_position_size_before": "10", "timestamp": T}
    execution = lighter_execution(raw, 1, {})
    assert execution.id.endswith("9007199254740999")
    assert execution.before == "10"
    assert execution.delta == "-5"


def test_repeated_lighter_cursor_cannot_advance_checkpoint():
    client = Mock()
    client.fetch_market_symbols.return_value = {0: "BTC"}
    client.get_json.return_value = {"code": 200, "trades": [], "next_cursor": "repeat"}
    tracker = SimpleNamespace(client=client, auth_provider=Mock())
    with pytest.raises(JournalError, match="repeated a cursor"):
        ExecutionReader(tracker).read("lighter", 1, T, T + 1000)


def test_bingx_identical_fills_retain_multiplicity_and_restart_ids(monkeypatch):
    raw = {"symbol": "BTC-USDT", "orderId": "123", "side": "BUY", "positionSide": "LONG",
           "volume": "1", "price": "100", "commission": "-0.1", "currency": "USDT",
           "filledTm": "2026-10-16T10:00:00.000Z"}
    client = Mock()
    client._get_json.return_value = {"data": {"fill_orders": [raw, raw.copy()]}}
    tracker = SimpleNamespace(bingx_client=client)
    first = ExecutionReader(tracker).read("bingx", 0, T, T + 1000)
    second = ExecutionReader(tracker).read("bingx", 0, T, T + 1000)
    assert len({f.id for f in first}) == 2
    assert [f.id for f in first] == [f.id for f in second]
    raw["commission"] = "-0.2"
    corrected = ExecutionReader(tracker).read("bingx", 0, T, T + 1000)
    assert corrected[0].id == first[0].id


class FakeSheets:
    def __init__(self):
        header = [""] * 17
        for index, label in HEADERS.items():
            header[index] = label
        header[13] = "P/L $\n0 $"
        self.rows = {4: header}
        self.metadata = []
        self.batches = []
        self.timeout_after_apply = False

    def request(self, method, suffix="", *, params=None, body=None):
        if method == "GET" and not suffix:
            return {"sheets": [{"properties": {"sheetId": 0, "title": "Sheet1", "gridProperties": {"rowCount": 1000}}}],
                    "developerMetadata": []}
        if method == "POST" and suffix == "/developerMetadata:search":
            return {"matchedDeveloperMetadata": [{"developerMetadata": deepcopy(item)} for item in self.metadata]}
        if method == "GET":
            if "P5%3AP" in suffix:
                return {"values": [[self.rows.get(row, [""] * 17)[15]] for row in range(4, max(self.rows) + 1)]}
            if "A1%3AQ5" in suffix:
                return {"values": [self.rows.get(row, [""] * 17)[:] for row in range(5)]}
            return {"values": [self.rows.get(row, [""] * 17)[:] for row in range(4, max(self.rows) + 1)]}
        if not body.get("requests"):
            raise JournalError("Google Sheets HTTP 400; Must specify at least one request.")
        self.batches.append(body)
        for request in body["requests"]:
            if "updateCells" in request:
                update = request["updateCells"]
                start = update["start"]
                value = update["rows"][0]["values"][0].get("userEnteredValue", {})
                self.rows.setdefault(start["rowIndex"], [""] * 17)[start["columnIndex"]] = next(iter(value.values()), "")
            if "createDeveloperMetadata" in request:
                self.metadata.append(request["createDeveloperMetadata"]["developerMetadata"])
        if self.timeout_after_apply:
            self.timeout_after_apply = False
            raise JournalError("Simulated lost response")
        return {}


@pytest.fixture
def sheet(tmp_path):
    client = FakeSheets()
    ledger = JournalLedger(tmp_path / "test.sqlite3")
    writer = SheetsJournal(config(tmp_path, capital=D(1000)), client)
    yield writer, client, ledger
    ledger.close()


def partial_trade():
    trade, = replay([fill(1, 10), fill(2, -5, 102)], {})
    trade.original_risk = D(20)
    trade.capital = D(1000)
    return trade


def test_partial_profit_updates_open_row_without_final_outcome(sheet):
    writer, client, ledger = sheet
    trade = partial_trade()
    assert writer.sync([trade], ledger) == 1
    row = client.rows[5]
    assert row[2] == "Open"
    assert row[13] == 10
    assert row[8] == 0.02
    assert row[14] == 0.01
    assert row[11:13] == ["-", ""]
    assert row[16] == ""
    assert row[10] == row[15] == ""
    assert writer.sync([trade], ledger) == 0
    assert len(client.batches) == 1


def test_final_close_updates_same_row(sheet):
    writer, client, ledger = sheet
    writer.sync([partial_trade()], ledger)
    trade, = replay([fill(1, 10), fill(2, -5, 102), fill(3, -5, 103)], {})
    trade.capital = D(1000)
    writer.sync([trade], ledger)
    assert len(client.metadata) == 1
    assert client.rows[5][2] == "Closed"
    assert client.rows[5][12:15] == ["W", 25, 0.025]
    assert isinstance(client.rows[5][11], (int, float))


def test_closed_position_writes_chart_reference_and_retries_without_duplicates(sheet):
    writer, client, ledger = sheet
    writer.charts = Mock()
    writer.charts.generate.return_value = "https://storage.googleapis.com/charts/test.png"
    trade, = replay([fill(1, 10), fill(2, -10, 103)], {})
    client.timeout_after_apply = True
    with pytest.raises(JournalError, match="lost response"):
        writer.sync([trade], ledger)
    assert client.rows[5][15] == 'https://storage.googleapis.com/charts/test.png'
    images = [r["updateCells"] for r in client.batches[0]["requests"]
              if "updateCells" in r and r["updateCells"]["start"]["columnIndex"] == 15]
    assert images[0]["rows"][0]["values"][0]["userEnteredValue"] == {"stringValue": client.rows[5][15]}
    assert images[0]["rows"][0]["values"][0]["textFormatRuns"][0]["format"]["link"]["uri"] == client.rows[5][15]
    assert not any("updateDimensionProperties" in request for request in client.batches[0]["requests"])
    writer.sync([trade], ledger)
    assert len(client.metadata) == 1
    assert writer.charts.generate.call_count == 1
    batches = len(client.batches)
    assert writer.sync([trade], ledger) == 0
    assert len(client.batches) == batches


def test_chart_failure_keeps_close_and_profit_updates_and_retries(sheet):
    writer, client, ledger = sheet
    writer.charts = Mock()
    writer.charts.generate.side_effect = JournalError("image upload unavailable")
    trade, = replay([fill(1, 10), fill(2, -10, 103)], {})
    writer.sync([trade], ledger)
    assert client.rows[5][2] == "Closed"
    assert client.rows[5][13] == 30
    assert client.rows[5][15] == ""
    assert "image upload unavailable" in writer.chart_errors[0]
    writer.charts.generate.side_effect = None
    writer.charts.generate.return_value = "https://storage.googleapis.com/charts/retry.png"
    writer.sync([trade], ledger)
    assert client.rows[5][15] == "https://storage.googleapis.com/charts/retry.png"
    assert not writer.chart_errors


def test_existing_manual_exit_chart_is_preserved_and_partial_exit_has_no_chart(sheet):
    writer, client, ledger = sheet
    writer.charts = Mock()
    writer.sync([partial_trade()], ledger)
    writer.charts.generate.assert_not_called()
    client.rows[5][15] = '=IMAGE("https://example.com/manual.png")'
    trade, = replay([fill(1, 10), fill(2, -10, 103)], {})
    writer.sync([trade], ledger)
    writer.charts.generate.assert_not_called()
    assert client.rows[5][15] == '=IMAGE("https://example.com/manual.png")'


def test_chart_formula_is_read_even_when_calculated_value_is_blank(sheet):
    writer, client, ledger = sheet
    writer.charts = Mock()
    writer.sync([partial_trade()], ledger)
    formula = '=IMAGE("https://example.com/manual.png")'
    original = client.request
    def request(method, suffix="", **kwargs):
        response = original(method, suffix, **kwargs)
        if method == "GET" and "P5%3AP" in suffix:
            response["values"] = [["Charts Exit"], [formula]]
        return response
    client.request = request
    trade, = replay([fill(1, 10), fill(2, -10, 103)], {})
    writer.sync([trade], ledger)
    writer.charts.generate.assert_not_called()


def test_adopts_matching_manual_open_row_preserving_fields_and_charts(sheet):
    writer, client, ledger = sheet
    row = [71, "16.10.2026 (F)", "Open", "overnight", "1H", "My setup", "BTC", "Long",
           0.01, 7, "entry chart", "-", "", 9.5, "", "exit chart", "risk free at 1R"]
    client.rows[10] = row[:]
    writer.sync([partial_trade()], ledger)
    current = client.rows[10]
    for index in (0, 1, 3, 4, 5, 8, 9, 10, 15):
        assert current[index] == row[index]
    assert current[16] == "risk free at 1R"
    assert current[13] == 10
    assert len(client.metadata) == 1
    assert client.metadata[0]["location"]["dimensionRange"]["startIndex"] == 10


def test_legacy_sync_blocks_are_removed_without_rewriting_manual_description(sheet):
    writer, client, ledger = sheet
    client.rows[5] = [1, "16.10.2026", "Open", "", "", "", "BTC", "Long"] + [""] * 9
    manual = "risk free at 1R\nMy second line."
    client.rows[5][16] = manual + "\n[active-trades] Technical status. [/active-trades]\nMy later update."
    writer.sync([partial_trade()], ledger)
    assert client.rows[5][16] == manual + "\nMy later update."
    batches = len(client.batches)
    assert writer.sync([partial_trade()], ledger) == 0
    assert len(client.batches) == batches


def test_existing_template_row_keeps_its_format_and_new_money_uses_currency(sheet):
    writer, client, ledger = sheet
    writer.sync([partial_trade()], ledger)
    requests = client.batches[0]["requests"]
    assert not any("copyPaste" in item for item in requests)
    formats = [item["repeatCell"] for item in requests if "repeatCell" in item]
    pnl = next(item for item in formats if item["range"]["startColumnIndex"] == 13)
    assert pnl["cell"]["userEnteredFormat"]["numberFormat"] == {"type": "CURRENCY", "pattern": '"$"#,##0.00'}
    hour = next(item for item in formats if item["range"]["startColumnIndex"] == 3)
    assert hour["cell"]["userEnteredFormat"]["numberFormat"]["pattern"] == "h:mm"


@pytest.mark.parametrize("name", ["Copy of Sheet1", "copy of sheet1", " COPY OF SHEET1 "])
def test_preserved_original_cannot_be_selected_as_a_sync_target(tmp_path, name):
    with pytest.raises(ValueError, match="preserved original"):
        config(tmp_path, sheet_name=name)


def test_timeout_after_server_commit_does_not_duplicate_row(sheet):
    writer, client, ledger = sheet
    client.timeout_after_apply = True
    with pytest.raises(JournalError):
        writer.sync([partial_trade()], ledger)
    writer.sync([partial_trade()], ledger)
    assert len(client.metadata) == 1
    assert set(client.rows) == {4, 5}


def test_row_metadata_search_handles_omitted_zero_sheet_id(sheet):
    writer, client, ledger = sheet
    trade = partial_trade()
    writer.sync([trade], ledger)
    del client.metadata[0]["location"]["dimensionRange"]["sheetId"]
    # Row tags are absent from spreadsheets.get; search is the source of truth.
    assert writer.sync([trade], ledger) == 0
    assert len(client.metadata) == 1
    assert len(client.batches) == 1


def test_row_insertion_moves_binding_and_update_follows_it(sheet):
    writer, client, ledger = sheet
    trade = partial_trade()
    writer.sync([trade], ledger)
    client.rows[12] = client.rows.pop(5)
    client.metadata[0]["location"]["dimensionRange"].update(startIndex=12, endIndex=13)
    trade.pnl = D(15)
    writer.sync([trade], ledger)
    assert client.rows[12][13] == 15
    assert 5 not in client.rows


def test_ambiguous_account_match_stops_before_any_write(sheet):
    writer, client, ledger = sheet
    client.rows[5] = [1, "16.10.2026", "Open", "", "", "", "BTC", "Long"] + [""] * 9
    first = partial_trade()
    second = replace(first, id="other-account", account=2)
    with pytest.raises(JournalError, match="Multiple positions"):
        writer.sync([first, second], ledger)
    assert not client.batches


def test_missing_capital_keeps_percentages_blank(sheet):
    writer, client, ledger = sheet
    trade = partial_trade()
    trade.capital = None
    writer.sync([trade], ledger)
    assert client.rows[5][8] == client.rows[5][14] == ""
    assert client.rows[5][13] == 10


def test_invalid_headers_never_write(sheet):
    writer, client, ledger = sheet
    client.rows[4][6] = "Wrong layout"
    with pytest.raises(JournalError, match="headers"):
        writer.sync([partial_trade()], ledger)
    assert not client.batches


def test_summary_ranges_expand_when_rows_cross_template_limit(sheet):
    writer, client, ledger = sheet
    client.rows[2] = [""] * 17
    client.rows[2][10] = '=COUNTIF(M6:M100, "W")'
    client.rows[2][13] = '=SUM(N6:N939)'
    client.rows[99] = [95, "15.10.2026", "Closed", "", "", "", "ETH", "Long"] + [""] * 9
    writer.sync([partial_trade()], ledger)
    assert client.rows[2][10] == '=COUNTIF(M6:M, "W")'
    assert client.rows[2][13] == '=SUM(N6:N939)'
    assert client.rows[100][0] == 96


def test_access_check_tests_write_authorization_without_changes(sheet):
    writer, client, _ = sheet
    original_rows = deepcopy(client.rows)
    writer.check()
    assert len(client.batches) == 1
    probe, = client.batches[0]["requests"]
    replacement = probe["findReplace"]
    assert replacement["find"] == replacement["replacement"]
    assert replacement["includeFormulas"] is False
    assert replacement["matchEntireCell"] is True
    assert replacement["range"] == {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
                                     "startColumnIndex": 0, "endColumnIndex": 1}
    assert client.rows == original_rows
    assert not client.metadata


def test_access_check_rejects_viewer_access(sheet):
    writer, client, _ = sheet
    request = client.request
    def viewer_request(method, suffix="", **kwargs):
        if method == "POST" and suffix == ":batchUpdate":
            raise JournalError("Google Sheets HTTP 403; write access denied.")
        return request(method, suffix, **kwargs)
    client.request = viewer_request
    with pytest.raises(JournalError, match="write access denied"):
        writer.check()
    assert not client.batches


@pytest.mark.parametrize("status", [403, 429, 500])
def test_google_transport_errors_do_not_expose_credentials(status):
    client = GoogleSheetsClient.__new__(GoogleSheetsClient)
    client.base = "https://sheets.googleapis.com/v4/spreadsheets/test"
    client.timeout = 1
    client.session = Mock()
    client.session.request.return_value.status_code = status
    client.session.request.return_value.text = "secret-token"
    client.session.request.return_value.json.return_value = {"error": {"message": "secret-token"}}
    with pytest.raises(JournalError) as raised:
        client.request("POST", ":batchUpdate", body={"requests": []})
    assert "secret-token" not in str(raised.value)
    assert str(status) in str(raised.value)


def test_google_write_denied_names_service_account_and_editor_fix():
    client = GoogleSheetsClient.__new__(GoogleSheetsClient)
    client.service_account_email = "journal@example.iam.gserviceaccount.com"
    response = Mock(status_code=403)
    response.json.return_value = {"error": {"status": "PERMISSION_DENIED", "message": "The caller does not have permission"}}
    message = client._error_message(response, "POST")
    assert "write access denied" in message
    assert "journal@example.iam.gserviceaccount.com Editor access" in message
    assert "enable the Google Sheets API" not in message


@pytest.mark.parametrize("reason,expected", [
    ("SERVICE_DISABLED", "enable the Google Sheets API"),
    ("ACCESS_TOKEN_SCOPE_INSUFFICIENT", "lacks the Google Sheets write scope"),
])
def test_google_error_reason_distinguishes_configuration_from_sharing(reason, expected):
    client = GoogleSheetsClient.__new__(GoogleSheetsClient)
    response = Mock(status_code=403)
    response.json.return_value = {"error": {"details": [{"reason": reason}]}}
    assert expected in client._error_message(response, "POST")


def test_google_read_denied_does_not_claim_write_failure():
    client = GoogleSheetsClient.__new__(GoogleSheetsClient)
    response = Mock(status_code=403)
    response.json.return_value = {"error": {"message": "secret-token"}}
    message = client._error_message(response, "GET")
    assert "read access denied" in message
    assert "secret-token" not in message


def test_google_connection_failure_is_sanitized():
    client = GoogleSheetsClient.__new__(GoogleSheetsClient)
    client.base, client.timeout, client.session = "https://example.invalid", 1, Mock()
    client.session.request.side_effect = RuntimeError("secret-token")
    with pytest.raises(JournalError) as raised:
        client.request("GET")
    assert "secret-token" not in str(raised.value)


def snapshot(quantity=10, *, available=True):
    position = Position(1, 0, "BTC", "long", D(quantity), D(100))
    rows = (ActiveTrade(1, position, None, "", D(98), D(quantity) * 2),) if quantity else ()
    return TrackerSnapshot(datetime.fromtimestamp((T + 10_000) / 1000, UTC), (
        AccountSnapshot(AccountInfo(1), rows, positions_state="available" if available else "unavailable"),
    ))


def test_service_restart_dedup_catchup_and_google_failure_retry(tmp_path):
    reader = Mock()
    reader.read.return_value = [fill(1, 10)]
    writer = Mock()
    writer.sync.side_effect = JournalError("offline")
    settings = config(tmp_path, capital=D(1000))
    service = TradeJournal(settings, Mock(), reader=reader, writer=writer, clock=lambda: T)
    service._fresh_positions = lambda *_: [snapshot().accounts[0].trades[0].position]
    assert "offline" in service.sync(snapshot()).errors[0]
    ledger = JournalLedger(settings.state_path)
    assert len(ledger.executions("lighter:1")) == 1
    ledger.close()
    reader.read.return_value = [fill(1, 10), fill(2, -5, 102)]
    writer.sync.side_effect = None
    restarted = TradeJournal(settings, Mock(), reader=reader, writer=writer, clock=lambda: T + 20_000)
    assert not restarted.sync(snapshot(5)).errors
    trade, = writer.sync.call_args.args[0]
    assert trade.pnl == 10
    assert trade.original_risk == 20  # A partial must not shrink the risk baseline.
    ledger = JournalLedger(settings.state_path)
    assert len(ledger.executions("lighter:1")) == 2
    ledger.close()


def test_failed_or_incomplete_position_read_cannot_close_rows(tmp_path):
    reader, writer = Mock(), Mock()
    service = TradeJournal(config(tmp_path), Mock(), reader=reader, writer=writer)
    assert service.sync(snapshot(0, available=False)).errors
    reader.read.assert_not_called()
    writer.sync.assert_not_called()


def test_quantity_mismatch_does_not_advance_saved_checkpoint(tmp_path):
    reader = Mock()
    reader.read.return_value = [fill(1, 10)]
    settings = config(tmp_path)
    writer = Mock()
    service = TradeJournal(settings, Mock(), reader=reader, writer=writer, clock=lambda: T)
    service._fresh_positions = lambda *_: [snapshot().accounts[0].trades[0].position]
    assert not service.sync(snapshot()).errors
    reader.read.return_value = [fill(2, -5)]
    service.clock = lambda: T + 5000
    assert service.sync(snapshot(0)).errors
    ledger = JournalLedger(settings.state_path)
    assert ledger.scope("lighter:1")[0] == T
    ledger.close()


def test_journal_disabled_requires_no_google_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("ACTIVE_TRADES_JOURNAL_ENABLED", raising=False)
    assert JournalSettings.from_env(tmp_path) is None


def test_google_url_timezone_and_capital_configuration(monkeypatch, tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}")
    monkeypatch.setenv("ACTIVE_TRADES_JOURNAL_ENABLED", "true")
    monkeypatch.setenv("ACTIVE_TRADES_JOURNAL_SPREADSHEET", "https://docs.google.com/spreadsheets/d/abc_123/edit?gid=0#gid=0")
    monkeypatch.setenv("ACTIVE_TRADES_JOURNAL_CREDENTIALS", str(credentials))
    monkeypatch.setenv("ACTIVE_TRADES_JOURNAL_CAPITAL", "1730")
    settings = JournalSettings.from_env(tmp_path)
    assert settings.spreadsheet_id == "abc_123"
    assert settings.capital == 1730
    assert settings.timezone == "Europe/Sofia"
    monkeypatch.setenv("ACTIVE_TRADES_JOURNAL_CAPITAL", "NaN")
    with pytest.raises(ValueError, match="finite"):
        JournalSettings.from_env(tmp_path)


@pytest.mark.parametrize("side,entry,stop,target,expected", [
    ("long", "100", "98", "107", "3.5"),
    ("short", "100", "102", "93", "3.5"),
    ("long", "100", "100", "110", None),
    ("long", "100", "101", "110", None),
    ("short", "100", "99", "90", None),
    ("long", "100", "98", "99", None),
    ("short", "100", "102", "101", None),
    ("short", "100", "102", "0", None),
    ("long", "NaN", "98", "110", None),
    ("long", "100", "Infinity", "110", None),
    ("long", "100", "98", "NaN", None),
])
def test_potential_rr_requires_a_valid_directional_plan(side, entry, stop, target, expected):
    result = potential_rr(D(entry), D(stop), D(target), side)
    assert result == (D(expected) if expected is not None else None)


def plan_snapshot(*, side="long", exchange="lighter"):
    position = Position(1, 0, "BTC", side, D(10), D(100), exchange=exchange, position_id="position-1")
    stop = D(98) if side == "long" else D(102)
    active = ActiveTrade(1, position, None, "", stop, D(20), risk_state="at-risk")
    target = StopOrder(1, 0, "sell" if side == "long" else "buy", D(108) if side == "long" else D(92),
                       remaining_base_amount=D(10), order_type="take-profit", reduce_only=True, status="open", exchange=exchange)
    account = AccountSnapshot(AccountInfo(1, exchange=exchange), (active,), stop_orders=(target,))
    trade, = replay([fill(1, 10 if side == "long" else -10, exchange=exchange)], {})
    return trade, active, account


@pytest.mark.parametrize("side", ["long", "short"])
def test_plan_uses_furthest_profitable_target_including_reducing_limits(side):
    trade, active, account = plan_snapshot(side=side)
    limit = LimitOrder(1, 0, "BTC", "sell" if side == "long" else "buy", D(110) if side == "long" else D(90), D(5), reduce_only=True)
    account = replace(account, limit_orders=(limit,))
    metadata = {}
    capture_plan(trade, active, account, metadata)
    assert D(metadata["potential_rr"]) == 5
    assert D(metadata["plan_target"]) == limit.price


@pytest.mark.parametrize("changes", [
    {"exchange": "bingx"}, {"account_index": 2}, {"market_id": 1},
    {"reduce_only": False}, {"side": "buy"}, {"status": "filled"},
    {"order_type": "stop-loss"}, {"trigger_price": D(99)},
    {"remaining_base_amount": D(0)}, {"raw": {"trigger_status": "parent-order"}},
])
def test_unrelated_or_inactive_orders_cannot_supply_potential_rr(changes):
    trade, active, account = plan_snapshot()
    account = replace(account, stop_orders=(replace(account.stop_orders[0], **changes),))
    metadata = {}
    capture_plan(trade, active, account, metadata)
    assert "potential_rr" not in metadata


@pytest.mark.parametrize("raw", [{"positionSide": "SHORT"}, {"positionId": "other-position"}])
def test_tp_cannot_be_taken_from_another_bingx_hedge_leg_or_position(raw):
    trade, active, account = plan_snapshot(exchange="bingx")
    account = replace(account, stop_orders=(replace(account.stop_orders[0], raw=raw),))
    metadata = {}
    capture_plan(trade, active, account, metadata)
    assert "potential_rr" not in metadata


def test_missing_original_stop_after_partial_does_not_invent_rr():
    trade, active, account = plan_snapshot()
    trade.exited = D(5)
    metadata = {}
    capture_plan(trade, active, account, metadata)
    assert metadata == {}


def test_tp_added_after_partial_uses_previously_saved_entry_and_stop():
    trade, active, account = plan_snapshot()
    metadata = {}
    capture_plan(trade, active, replace(account, stop_orders=()), metadata)
    assert "potential_rr" not in metadata
    trade.exited = D(5)
    capture_plan(trade, replace(active, stop_loss=D(100), risk_usd=D(0)), account, metadata)
    assert D(metadata["potential_rr"]) == 4
    assert metadata["plan_stop"] == "98"


def test_plan_and_rr_survive_restart_and_moved_stops(tmp_path):
    trade, active, account = plan_snapshot()
    snap = TrackerSnapshot(datetime.fromtimestamp(T / 1000, UTC), (account,))
    reader, writer = Mock(), Mock()
    reader.read.return_value = [fill(1, 10)]
    settings = config(tmp_path)
    service = TradeJournal(settings, Mock(), reader=reader, writer=writer, clock=lambda: T)
    service._fresh_positions = lambda *_: [active.position]
    assert not service.sync(snap).errors
    assert writer.sync.call_args.args[0][0].potential_rr == 4
    reader.read.return_value += [fill(2, -5, 102)]
    moved = replace(active, position=replace(active.position, size=D(5)), stop_loss=D(100), risk_usd=D(0))
    account = replace(account, trades=(moved,), stop_orders=(replace(account.stop_orders[0], trigger_price=D(120)),))
    restarted = TradeJournal(settings, Mock(), reader=reader, writer=writer, clock=lambda: T + 20_000)
    assert not restarted.sync(replace(snap, accounts=(account,))).errors
    assert writer.sync.call_args.args[0][0].potential_rr == 4


def test_full_journal_close_renders_saved_plan_and_links_chart(sheet):
    from active_trades.exit_chart import Candle
    from active_trades.journal_charts import ExitChartService
    writer, client, ledger = sheet
    settings = replace(writer.settings, chart_bucket="test-charts")
    reader, candles, publisher = Mock(), Mock(), Mock()
    candles.read.return_value = [Candle(T + offset, D(100), D(109), D(97), D(102))
                                 for offset in (-60_000, 0, 60_000, 120_000, 180_000)]
    publisher.publish.return_value = "https://storage.googleapis.com/test-charts/chart.png"
    writer.charts = ExitChartService(settings, None, candles=candles, publisher=publisher)
    trade, active, account = plan_snapshot()
    opened = TrackerSnapshot(datetime.fromtimestamp(T / 1000, UTC), (account,))
    reader.read.return_value = [fill(1, 10)]
    service = TradeJournal(settings, Mock(), reader=reader, writer=writer, clock=lambda: T)
    service._fresh_positions = lambda *_: [active.position]
    assert not service.sync(opened).errors
    publisher.publish.assert_not_called()
    reader.read.return_value = [fill(1, 10), fill(2, -10, "108", time=T + 180_000)]
    service.clock = lambda: T + 180_001
    closed = replace(opened, accounts=(replace(account, trades=(), stop_orders=()),))
    assert not service.sync(closed).errors
    assert client.rows[5][2] == "Closed"
    assert client.rows[5][15] == 'https://storage.googleapis.com/test-charts/chart.png'
    persisted = JournalLedger(settings.state_path)
    try:
        saved = persisted.metadata(trade.id)
    finally:
        persisted.close()
    assert saved["plan_stop"] == "98" and saved["plan_target"] == "108"
    assert saved["exit_chart_url"].endswith("chart.png")
    assert Path(saved["exit_chart_file"]).is_file()


def test_new_row_rr_rounding_preserves_manual_rr_on_later_sync(sheet):
    writer, client, ledger = sheet
    trade = partial_trade()
    trade.potential_rr = D("3.456")
    writer.sync([trade], ledger)
    assert client.rows[5][9] == 3.46
    client.rows[5][9] = 7
    writer.sync([trade], ledger)
    assert client.rows[5][9] == 7


@pytest.mark.parametrize("offset,letter", list(enumerate("MTWTFSS")))
def test_entry_date_keeps_numeric_date_with_weekday_initial(sheet, offset, letter):
    writer, client, ledger = sheet
    opened = datetime(2026, 10, 12, 10, tzinfo=UTC) + timedelta(days=offset)
    trade, = replay([fill(1, 10, time=int(opened.timestamp() * 1000))], {})
    writer.sync([trade], ledger)
    assert isinstance(client.rows[5][1], (int, float))
    assert client.rows[5][11] == "-"
    formats = [item["repeatCell"] for item in client.batches[0]["requests"] if "repeatCell" in item]
    date = next(item for item in formats if item["range"]["startColumnIndex"] == 1)
    assert date["cell"]["userEnteredFormat"]["numberFormat"] == {"type": "DATE", "pattern": f'dd.mm.yyyy" ({letter})"'}


def test_entry_date_weekday_uses_journal_timezone_at_midnight(sheet):
    writer, client, ledger = sheet
    opened = datetime(2026, 10, 16, 22, tzinfo=UTC)  # Saturday in Sofia.
    trade, = replay([fill(1, 10, time=int(opened.timestamp() * 1000))], {})
    writer.sync([trade], ledger)
    assert client.rows[5][1] == 46312
    date = next(item["repeatCell"] for item in client.batches[0]["requests"]
                if "repeatCell" in item and item["repeatCell"]["range"]["startColumnIndex"] == 1)
    assert date["cell"]["userEnteredFormat"]["numberFormat"]["pattern"] == 'dd.mm.yyyy" (S)"'


def test_previously_created_row_gets_date_format_and_close_placeholder_once(sheet):
    writer, client, ledger = sheet
    trade = partial_trade()
    writer.sync([trade], ledger)
    metadata = ledger.metadata(trade.id)
    metadata.pop("entry_date_format")
    ledger.remember(trade.id, metadata)
    client.rows[5][11] = ""
    assert writer.sync([trade], ledger) == 1
    assert client.rows[5][11] == "-"
    assert writer.sync([trade], ledger) == 0


def close_formats(batch):
    return [item["repeatCell"]["cell"]["userEnteredFormat"]["numberFormat"] for item in batch["requests"]
            if "repeatCell" in item and item["repeatCell"]["range"]["startColumnIndex"] == 11]


def test_close_date_keeps_numeric_date_with_weekday_initial_in_journal_timezone(sheet):
    writer, client, ledger = sheet
    writer.sync([partial_trade()], ledger)
    assert close_formats(client.batches[0]) == [{"type": "DATE", "pattern": "dd.mm.yyyy"}]
    closed = int(datetime(2026, 10, 16, 22, tzinfo=UTC).timestamp() * 1000)  # Saturday in Sofia.
    trade, = replay([fill(1, 10), fill(2, -5, 102), fill(3, -5, 103, time=closed)], {})
    trade.capital = D(1000)
    writer.sync([trade], ledger)
    assert client.rows[5][11] == 46312
    assert close_formats(client.batches[1]) == [{"type": "DATE", "pattern": 'dd.mm.yyyy" (S)"'}]
    assert writer.sync([trade], ledger) == 0
    assert len(client.batches) == 2


def test_previously_closed_row_gets_close_weekday_format_once(sheet):
    writer, client, ledger = sheet
    trade, = replay([fill(1, 10), fill(2, -10, 102)], {})  # Closed Friday 16.10.2026.
    trade.capital = D(1000)
    writer.sync([trade], ledger)
    metadata = ledger.metadata(trade.id)
    metadata.pop("close_date_format")
    ledger.remember(trade.id, metadata)
    assert writer.sync([trade], ledger) == 1
    requests = client.batches[1]["requests"]
    assert len(requests) == 1
    assert close_formats(client.batches[1]) == [{"type": "DATE", "pattern": 'dd.mm.yyyy" (F)"'}]
    assert writer.sync([trade], ledger) == 0
