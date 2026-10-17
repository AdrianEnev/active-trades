"""Google Sheets writer for the supplied C2 Arc journal (headers on row 5)."""
from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from active_trades.journal_executions import JournalError
from active_trades.journal_ledger import JournalTrade
from active_trades.journal_settings import JournalSettings


METADATA_KEY = "active_trades_id"
HEADERS = {0: "Nr", 1: "Entry Date", 2: "Trade Active?", 3: "Hour", 4: "TF", 5: "Setup",
           6: "Asset", 7: "S/L", 8: "Risk", 9: "Potential RR", 10: "Charts Entry",
           11: "Close Date", 12: "W/L/BE", 14: "Profit %", 15: "Charts Exit", 16: "Description"}


class GoogleSheetsClient:
    def __init__(self, settings: JournalSettings, *, timeout: int = 15) -> None:
        try:
            from google.oauth2.service_account import Credentials
            from google.auth.transport.requests import AuthorizedSession
        except ImportError:
            raise JournalError('Install Google Sheets support with: pip install -e ".[journal]"') from None
        try:
            credentials = Credentials.from_service_account_file(
                str(settings.credentials_path), scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            self.session = AuthorizedSession(credentials)
            self.service_account_email = credentials.service_account_email
        except Exception:
            raise JournalError("Could not load the Google service-account credentials file.") from None
        self.base = f"https://sheets.googleapis.com/v4/spreadsheets/{settings.spreadsheet_id}"
        self.timeout = timeout

    def request(self, method: str, suffix: str = "", *, params=None, body=None) -> dict:
        try:
            response = self.session.request(method, self.base + suffix, params=params, json=body,
                                            timeout=self.timeout, allow_redirects=False)
            status = response.status_code
            if status >= 300:
                # Developer-metadata search uses POST but does not write.
                operation = "GET" if suffix == "/developerMetadata:search" else method
                raise JournalError(self._error_message(response, operation))
            result = response.json()
            if not isinstance(result, dict):
                raise JournalError("Google Sheets returned an unexpected response.")
            return result
        except JournalError:
            raise
        except Exception:
            raise JournalError("Google Sheets request failed; pending journal updates will retry.") from None

    def _error_message(self, response, method: str) -> str:
        """Explain known Google failures without echoing arbitrary response text."""
        status = response.status_code
        error = {}
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                error = payload["error"]
        except Exception:
            pass
        reasons = {
            detail.get("reason") for key in ("details", "errors")
            for detail in error.get(key, []) if isinstance(detail, dict)
            and isinstance(detail.get("reason"), str)
        }
        account = getattr(self, "service_account_email", None) or "the configured service account"
        if reasons & {"SERVICE_DISABLED", "accessNotConfigured"}:
            detail = "enable the Google Sheets API in the service account's Google Cloud project"
        elif reasons & {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"}:
            detail = "the access token lacks the Google Sheets write scope; restart the updated script"
        elif status == 403 and method.upper() != "GET":
            detail = f"write access denied; in the journal's Share dialog grant {account} Editor access; if already Editor, check protected ranges"
        elif status == 403:
            detail = f"read access denied; share the journal with {account}"
        elif status == 401:
            detail = "authentication rejected; check the service-account credentials and system clock"
        elif status == 404:
            detail = f"spreadsheet not found or inaccessible; check the spreadsheet ID and sharing with {account}"
        elif status == 429:
            detail = "request quota exceeded; pending updates will retry on the next refresh"
        else:
            detail = "request rejected; pending updates will retry on the next refresh"
        return f"Google Sheets HTTP {status}; {detail}."

    def close(self) -> None:
        self.session.close()


def asset_key(value: str) -> str:
    value = re.sub(r"[^A-Z0-9]", "", value.upper())
    for suffix in ("USDT", "USDC"):
        if value.endswith(suffix):
            return value[:-len(suffix)]
    return value


def _date(value):
    if isinstance(value, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=value)).date()
    match = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})(?!\d)", str(value))
    if match:
        day, month, year = map(int, match.groups())
        try:
            return datetime(year + 2000 if year < 100 else year, month, day).date()
        except ValueError:
            return None
    return None


def _serial(value: datetime) -> float:
    return (value.replace(tzinfo=None) - datetime(1899, 12, 30)).total_seconds() / 86400


def _date_pattern(value: datetime) -> str:
    # Sheets has no single-letter weekday token. Keep the date numeric and add
    # the English weekday initial as a literal in this individual cell's format.
    weekday = ("M", "T", "W", "T", "F", "S", "S")[value.weekday()]
    return f'dd.mm.yyyy" ({weekday})"'


def _number_format(sheet: int, row: int, column: int, kind: str, pattern: str) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sheet, "startRowIndex": row, "endRowIndex": row + 1,
                  "startColumnIndex": column, "endColumnIndex": column + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": kind, "pattern": pattern}}},
        "fields": "userEnteredFormat.numberFormat",
    }}


def _manual_description(value: str) -> str:
    """Remove legacy generated blocks while retaining the user's description."""
    return re.sub(r"(?:\r?\n)?\[active-trades\].*?\[/active-trades\]", "", value, flags=re.S)


def _cell(value) -> dict:
    if value is None or value == "":
        return {}  # With fields=userEnteredValue this clears exactly this cell.
    if isinstance(value, (float, int, Decimal)):
        return {"userEnteredValue": {"numberValue": float(value)}}
    return {"userEnteredValue": {"stringValue": str(value)}}


def _write(sheet: int, row: int, column: int, value) -> dict:
    return {"updateCells": {"start": {"sheetId": sheet, "rowIndex": row, "columnIndex": column},
                            "rows": [{"values": [_cell(value)]}], "fields": "userEnteredValue"}}


class SheetsJournal:
    def __init__(self, settings: JournalSettings, client=None, *, charts=None) -> None:
        self.settings = settings
        self.client = client or GoogleSheetsClient(settings)
        self.charts = charts
        self.chart_errors: list[str] = []

    def _read_sheet(self):
        info = self.client.request("GET", params={"fields": "sheets(properties)"})
        matches = [sheet["properties"] for sheet in info.get("sheets", [])
                   if sheet["properties"]["title"] == self.settings.sheet_name]
        if len(matches) != 1:
            raise JournalError("Journal tab was not found; set ACTIVE_TRADES_JOURNAL_SHEET to its exact name.")
        properties = matches[0]
        name = "'" + self.settings.sheet_name.replace("'", "''") + "'"
        values = self.client.request("GET", "/values/" + quote(f"{name}!A5:Q", safe=""), params={
            "valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER",
        }).get("values", [])
        header = values[0] if values else []
        if any(index >= len(header) or str(header[index]).strip() != label for index, label in HEADERS.items()) or len(header) <= 13 or not str(header[13]).startswith("P/L $"):
            raise JournalError("Journal headers do not match the supplied C2 Arc layout on row 5; no cells changed.")
        rows = {index + 4: row + [""] * (17 - len(row)) for index, row in enumerate(values[1:], 1)}
        if self.charts is not None:
            # Read formulas as well as URLs so existing user chart references
            # survive even when a formula's calculated value is blank.
            chart_values = self.client.request("GET", "/values/" + quote(f"{name}!P5:P", safe=""),
                                               params={"valueRenderOption": "FORMULA"}).get("values", [])
            for offset, value in enumerate(chart_values[1:], 1):
                if value and value[0] != "":
                    rows.setdefault(offset + 4, [""] * 17)[15] = value[0]
        # Spreadsheet.developerMetadata does not contain row/dimension tags.
        # Search retrieves those identities regardless of their grid location.
        metadata = self.client.request("POST", "/developerMetadata:search", body={
            "dataFilters": [{"developerMetadataLookup": {
                "metadataKey": METADATA_KEY, "visibility": "DOCUMENT",
            }}],
        })
        info["developerMetadata"] = [item["developerMetadata"] for item in metadata.get("matchedDeveloperMetadata", [])]
        return info, properties, rows

    def check(self) -> None:
        """Validate read/write authorization and layout without changing cells."""
        _, properties, _ = self._read_sheet()
        # Google rejects empty batches. A literal find/replace of a value with
        # itself is a valid write-authorized request, even if A1 has no match.
        # Limit it to A1 and skip formulas; contents and formatting stay intact.
        probe = "__active_trades_write_access_check__"
        self.client.request("POST", ":batchUpdate", body={"requests": [{"findReplace": {
            "range": {"sheetId": properties["sheetId"], "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "find": probe, "replacement": probe, "matchCase": True,
            "matchEntireCell": True, "searchByRegex": False, "includeFormulas": False,
        }}]})

    def sync(self, trades: list[JournalTrade], ledger) -> int:
        self.chart_errors = []
        if not trades:
            return 0
        info, properties, rows = self._read_sheet()
        sheet_id = properties["sheetId"]
        grid_rows = properties["gridProperties"]["rowCount"]
        bindings: dict[str, int] = {}
        occupied: set[int] = set()
        for item in info.get("developerMetadata", []):
            location = item.get("location", {}).get("dimensionRange", {})
            # Google can omit the protobuf default sheetId=0 in dimension tags.
            if item.get("metadataKey") != METADATA_KEY or location.get("sheetId", 0) != sheet_id:
                continue
            if location.get("dimension") != "ROWS" or location.get("endIndex") != location.get("startIndex", -1) + 1:
                raise JournalError("Journal row identity metadata is invalid; no cells changed.")
            identity, row = item.get("metadataValue"), location["startIndex"]
            if identity in bindings or row in occupied or row < 5:
                raise JournalError("Duplicate journal row identities found; no cells changed.")
            bindings[identity] = row
            occupied.add(row)

        zone = ZoneInfo(self.settings.timezone)
        # Match existing manual OPEN rows only when asset, side and date agree.
        # A shared match across two accounts is ambiguous and must never be guessed.
        candidates: dict[str, list[int]] = {}
        for trade in trades:
            if trade.id in bindings:
                continue
            opened = datetime.fromtimestamp(trade.opened_ms / 1000, UTC).astimezone(zone).date()
            closed_date = datetime.fromtimestamp(trade.closed_ms / 1000, UTC).astimezone(zone).date() if trade.closed_ms else None
            same_position = [r for r, row in rows.items() if r not in occupied
                             and (str(row[2]).lower() == "open" or
                                  (closed_date is not None and str(row[2]).lower() == "closed" and _date(row[11]) == closed_date))
                             and asset_key(str(row[6])) == asset_key(trade.symbol)
                             and str(row[7]).lower() == trade.side]
            candidates[trade.id] = [r for r in same_position if _date(rows[r][1]) == opened]
            if same_position and not candidates[trade.id]:
                raise JournalError(f"Journal {trade.symbol}: an existing Open row has a different entry date; reconcile it before syncing.")
        counts = Counter(r for found in candidates.values() for r in found)
        if any(len(found) > 1 or any(counts[r] > 1 for r in found) for found in candidates.values()):
            raise JournalError("Multiple positions match an existing journal row; reconcile the duplicate Open rows before syncing.")

        next_row = max([4, *[r for r, row in rows.items() if any(v != "" for v in row)], *occupied]) + 1
        next_number = max([0, *[int(row[0]) for row in rows.values() if isinstance(row[0], (int, float))]]) + 1
        requests: list[dict] = []
        added_rows = False
        written = 0
        remembered: list[tuple[str, dict]] = []
        for trade in sorted(trades, key=lambda item: (item.opened_ms, item.id)):
            linked = trade.id in bindings
            found = candidates.get(trade.id, [])
            new = not linked and not found
            row_index = bindings[trade.id] if linked else found[0] if found else next_row
            existing = rows.get(row_index, [""] * 17)
            if linked and (asset_key(str(existing[6])) != asset_key(trade.symbol) or str(existing[7]).lower() != trade.side):
                raise JournalError("A linked journal row was moved or replaced without its identity; no cells changed.")
            if new:
                added_rows = True
                next_row += 1
                if row_index >= grid_rows:
                    requests.append({"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": row_index - grid_rows + 1}})
                    grid_rows = row_index + 1
                    if row_index > 5:
                        for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
                            requests.append({"copyPaste": {
                                "source": {"sheetId": sheet_id, "startRowIndex": row_index - 1, "endRowIndex": row_index, "startColumnIndex": 0, "endColumnIndex": 17},
                                "destination": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 0, "endColumnIndex": 17},
                                "pasteType": paste_type,
                            }})
            metadata = ledger.metadata(trade.id)
            capital = Decimal(metadata["capital"]) if metadata.get("capital") else trade.capital
            # An adopted manual risk percentage is authoritative.
            manual_risk = existing[8]
            risk = trade.original_risk
            if not new and isinstance(manual_risk, (int, float)) and capital:
                risk = Decimal(str(manual_risk)) * capital
            opened = datetime.fromtimestamp(trade.opened_ms / 1000, UTC).astimezone(zone)
            desired: dict[int, object] = {2: "Closed" if trade.closed_ms is not None else "Open"}
            if new:
                desired.update({0: next_number, 1: int(_serial(opened)), 3: _serial(opened) % 1,
                                6: re.sub(r"-(USDT|USDC)$", "", trade.symbol), 7: trade.side.title()})
                next_number += 1
            script_date = new or (linked and isinstance(existing[1], (int, float)) and existing[1] == int(_serial(opened)))
            if trade.closed_ms is None and (new or script_date and existing[11] == ""):
                desired[11] = "-"
            if risk is not None and capital and (new or existing[8] == ""):
                desired[8] = float(risk / capital)
            if trade.potential_rr is not None and (new or existing[9] == ""):
                desired[9] = float(round(trade.potential_rr, 2))
            if trade.exited:
                desired[13] = float(round(trade.pnl, 8))
                if capital:
                    desired[14] = float(trade.pnl / capital)
            if trade.closed_ms is not None:
                closed = datetime.fromtimestamp(trade.closed_ms / 1000, UTC).astimezone(zone)
                desired[11] = int(_serial(closed))
                desired[12] = "BE" if abs(trade.pnl) <= self.settings.breakeven_tolerance else "W" if trade.pnl > 0 else "L"
                if self.charts is not None and existing[15] == "":
                    try:
                        url = self.charts.generate(trade, metadata, ledger)
                        if not isinstance(url, str) or not url.startswith("https://") or any(c in url for c in ('"', "\r", "\n")):
                            raise JournalError("Exit chart publisher returned an invalid image URL.")
                        reference = _write(sheet_id, row_index, 15, url)
                        reference["updateCells"]["rows"][0]["values"][0]["textFormatRuns"] = [
                            {"startIndex": 0, "format": {"link": {"uri": url}}},
                        ]
                        reference["updateCells"]["fields"] = "userEnteredValue,textFormatRuns"
                        requests.append(reference)
                        changed_chart = True
                    except JournalError as exc:
                        self.chart_errors.append(f"Journal chart: {exc}")
                        changed_chart = False
                    except Exception:
                        self.chart_errors.append(f"Journal chart: {trade.symbol} image delivery failed; it will retry next refresh.")
                        changed_chart = False
                else:
                    changed_chart = False
            else:
                changed_chart = False
            # Description belongs to the user. Clean up blocks written by older
            # versions, then leave it alone on subsequent syncs and new rows.
            description = str(existing[16])
            cleaned_description = _manual_description(description)
            if cleaned_description != description:
                desired[16] = cleaned_description
            changed = changed_chart
            for column, value in desired.items():
                if existing[column] != value:
                    requests.append(_write(sheet_id, row_index, column, value))
                    changed = True
            date_pattern = _date_pattern(opened)
            if script_date and metadata.get("entry_date_format") != date_pattern:
                requests.append(_number_format(sheet_id, row_index, 1, "DATE", date_pattern))
                metadata["entry_date_format"] = date_pattern
                changed = True
            if trade.closed_ms is not None:
                # The script owns the close date of a closed row, so its weekday
                # format is (re)applied with the value or once for older rows.
                close_pattern = _date_pattern(closed)
                if existing[11] != desired[11] or metadata.get("close_date_format") != close_pattern:
                    requests.append(_number_format(sheet_id, row_index, 11, "DATE", close_pattern))
                    metadata["close_date_format"] = close_pattern
                    changed = True
            elif new:
                requests.append(_number_format(sheet_id, row_index, 11, "DATE", "dd.mm.yyyy"))
            if new:
                for column, pattern, kind in ((3, "h:mm", "TIME"), (8, "0.00%", "PERCENT"), (14, "0.00%", "PERCENT"), (13, '"$"#,##0.00', "CURRENCY")):
                    requests.append(_number_format(sheet_id, row_index, column, kind, pattern))
            if not linked:
                requests.append({"createDeveloperMetadata": {"developerMetadata": {
                    "metadataKey": METADATA_KEY, "metadataValue": trade.id, "visibility": "DOCUMENT",
                    "location": {"dimensionRange": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": row_index, "endIndex": row_index + 1}},
                }}})
                changed = True
            if changed:
                written += 1
            remembered.append((trade.id, {**metadata, "capital": str(capital) if capital else None}))
        if requests:
            if added_rows and next_row > 100:
                # The template's W/L counters stop at row 100 and P/L sums at
                # row 939. Extend only affected summary ranges as rows grow.
                name = "'" + self.settings.sheet_name.replace("'", "''") + "'"
                summary = self.client.request("GET", "/values/" + quote(f"{name}!A1:Q5", safe=""),
                                              params={"valueRenderOption": "FORMULA"}).get("values", [])
                for r, row in enumerate(summary):
                    for c, value in enumerate(row):
                        if not isinstance(value, str) or not value.startswith("="):
                            continue
                        def extend(match):
                            start, column, end_column, end = match.groups()
                            if column == end_column and int(end) < next_row:
                                return f"{start}:{end_column}"
                            return match.group(0)
                        updated = re.sub(r"(?<![\w!])(([A-Q])6):([A-Q])(\d+)\b", extend, value)
                        if updated != value:
                            request = _write(sheet_id, r, c, updated)
                            request["updateCells"]["rows"][0]["values"][0] = {"userEnteredValue": {"formulaValue": updated}}
                            requests.append(request)
            # New cells and identity are committed atomically. An uncertain response
            # is recovered by reading metadata again, never blind append retries.
            self.client.request("POST", ":batchUpdate", body={"requests": requests})
        for identity, metadata in remembered:
            ledger.remember(identity, metadata)
        return written
