from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path, *, override: bool = False) -> int:
    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return 0

    loaded = 0
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue

        key, value = parsed
        current_value = os.environ.get(key)
        if current_value and not override:
            continue

        os.environ[key] = value
        loaded += 1

    return loaded


def load_shared_env(*, anchor_path: str | Path | None = None) -> int:
    candidates: list[Path] = []
    candidates.extend(_env_candidates_from(Path.cwd()))
    if anchor_path is not None:
        anchor = Path(anchor_path).expanduser().resolve()
        candidates.extend(_env_candidates_from(anchor.parent if anchor.is_file() else anchor))

    loaded = 0
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        loaded += load_env_file(resolved)
    return loaded


def _env_candidates_from(start: Path) -> list[Path]:
    resolved_start = start.expanduser().resolve()
    return [directory / ".env" for directory in (resolved_start, *resolved_start.parents)]


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped.removeprefix("export ").strip()
    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]

    return key, value
