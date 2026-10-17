from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class JournalSettings:
    spreadsheet_id: str
    credentials_path: Path
    state_path: Path
    sheet_name: str = "Sheet1"
    timezone: str = "Europe/Sofia"
    capital: Decimal | None = None
    lookback_days: int = 30
    breakeven_tolerance: Decimal = Decimal("1")
    chart_bucket: str = ""

    def __post_init__(self) -> None:
        if re.sub(r"\s+", "", self.sheet_name).casefold() == "copyofsheet1":
            raise ValueError("Copy of Sheet1 is the preserved original and cannot be a journal sync target.")
        if self.chart_bucket and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", self.chart_bucket):
            raise ValueError("ACTIVE_TRADES_JOURNAL_CHART_BUCKET must be a Google Cloud Storage bucket name, without gs:// or a URL.")

    @classmethod
    def from_env(cls, state_directory: Path) -> JournalSettings | None:
        enabled = os.getenv("ACTIVE_TRADES_JOURNAL_ENABLED", "false").strip().lower()
        if enabled in {"false", "0", "no", ""}:
            return None
        if enabled not in {"true", "1", "yes"}:
            raise ValueError("ACTIVE_TRADES_JOURNAL_ENABLED must be true or false.")
        target = os.getenv("ACTIVE_TRADES_JOURNAL_SPREADSHEET", "").strip()
        match = re.fullmatch(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)(?:/[^\s]*)?", target)
        spreadsheet_id = match.group(1) if match else target
        if not re.fullmatch(r"[\w-]+", spreadsheet_id):
            raise ValueError("Set ACTIVE_TRADES_JOURNAL_SPREADSHEET to the Google Sheets URL or ID.")
        credentials = os.getenv("ACTIVE_TRADES_JOURNAL_CREDENTIALS", "").strip()
        if not credentials or not Path(credentials).expanduser().is_file():
            raise ValueError("ACTIVE_TRADES_JOURNAL_CREDENTIALS must point to a local service-account JSON file.")
        timezone = os.getenv("ACTIVE_TRADES_JOURNAL_TIMEZONE", "Europe/Sofia").strip()
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("ACTIVE_TRADES_JOURNAL_TIMEZONE must be an IANA timezone.") from None
        try:
            days = int(os.getenv("ACTIVE_TRADES_JOURNAL_LOOKBACK_DAYS", "30"))
        except ValueError:
            raise ValueError("ACTIVE_TRADES_JOURNAL_LOOKBACK_DAYS must be an integer.") from None
        if not 1 <= days <= 90:
            raise ValueError("ACTIVE_TRADES_JOURNAL_LOOKBACK_DAYS must be between 1 and 90.")
        capital = _amount("ACTIVE_TRADES_JOURNAL_CAPITAL", None)
        tolerance = _amount("ACTIVE_TRADES_JOURNAL_BE_TOLERANCE", Decimal("1"))
        if capital is not None and capital <= 0:
            raise ValueError("ACTIVE_TRADES_JOURNAL_CAPITAL must be greater than zero.")
        assert tolerance is not None
        if tolerance < 0:
            raise ValueError("ACTIVE_TRADES_JOURNAL_BE_TOLERANCE cannot be negative.")
        sheet_name = os.getenv("ACTIVE_TRADES_JOURNAL_SHEET", "Sheet1").strip()
        if not sheet_name:
            raise ValueError("ACTIVE_TRADES_JOURNAL_SHEET cannot be empty.")
        return cls(
            spreadsheet_id, Path(credentials).expanduser(),
            Path(os.getenv("ACTIVE_TRADES_JOURNAL_STATE") or state_directory / "journal.sqlite3").expanduser(),
            sheet_name, timezone, capital, days, tolerance,
            os.getenv("ACTIVE_TRADES_JOURNAL_CHART_BUCKET", "").strip(),
        )


def _amount(name: str, default: Decimal | None) -> Decimal | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = Decimal(raw)
        if value.is_finite():
            return value
    except InvalidOperation:
        pass
    raise ValueError(f"{name} must be a finite number.")
