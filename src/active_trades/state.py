from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from active_trades.models import Position


class PositionStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._opened_at_by_key: dict[str, str] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        positions = payload.get("positions") if isinstance(payload, dict) else None
        if not isinstance(positions, dict):
            return
        self._opened_at_by_key = {
            str(key): str(value)
            for key, value in positions.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def opened_at_for(self, position: Position) -> datetime | None:
        self.load()
        raw_value = self._opened_at_by_key.get(position.key)
        if raw_value is None:
            return None
        try:
            parsed = datetime.fromisoformat(raw_value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def remember(self, position: Position, opened_at: datetime) -> None:
        self.load()
        self._opened_at_by_key[position.key] = opened_at.astimezone(UTC).isoformat(timespec="seconds")

    def prune(self, active_positions: set[str]) -> None:
        self.load()
        self._opened_at_by_key = {
            key: value for key, value in self._opened_at_by_key.items() if key in active_positions
        }

    def save(self) -> None:
        self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"positions": dict(sorted(self._opened_at_by_key.items()))}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
