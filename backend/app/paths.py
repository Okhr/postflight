"""Paths stored in the database are always **relative to `data_dir`**.

Otherwise the database stops being portable: a sequence indexed in development
under `./data` would go missing once the stack is deployed with the volume
mounted on `/data`, and the other way around. So we only ever store
`raw/DJI_....MP4`, and resolve it when reading.
"""

from __future__ import annotations

from pathlib import Path

from .config import settings


def to_relative(path: Path | str) -> str:
    candidate = Path(path)
    root = settings.data_dir.resolve()
    try:
        return str(candidate.resolve().relative_to(root))
    except ValueError:
        # Outside the data volume: keep it absolute, for lack of anything better.
        return str(candidate)


def to_absolute(value: Path | str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return settings.data_dir / candidate


def exists(value: Path | str | None) -> bool:
    resolved = to_absolute(value)
    return bool(resolved and resolved.is_file())
