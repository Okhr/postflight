"""Paths stored in the database are always **relative to `data_dir`**.

Otherwise the database stops being portable: a sequence indexed in development
under `./data` would go missing once the stack is deployed with the volume
mounted on `/data`, and the other way around. So we only ever store
`raw/DJI_....MP4`, and resolve it when reading.
"""

from __future__ import annotations

import uuid
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


# --------------------------------------------------------------------------- #
# Whose volume is this
# --------------------------------------------------------------------------- #

_VOLUME_ID_FILE = ".volume-id"


def read_volume_id() -> str:
    """Identity of the data volume this process sees, or "" if unmarked.

    The dispatcher writes it once; both sides read it. A worker that mounts the
    dispatcher's volume reads the very same bytes, and that equality is the whole
    test for "do we share files". It replaces what would otherwise be a flag to set
    on every worker, which is a thing to get wrong in the one direction that hurts:
    a worker wrongly told it shares the volume goes looking for files that are not
    there, and fails every job.
    """
    try:
        return (settings.data_dir / _VOLUME_ID_FILE).read_text().strip()[:64]
    except OSError:
        return ""


def ensure_volume_id() -> str:
    """Mark this volume, if it is not marked already. Dispatcher side only."""
    existing = read_volume_id()
    if existing:
        return existing
    value = uuid.uuid4().hex
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / _VOLUME_ID_FILE).write_text(value)
    except OSError:
        # Unwritable volume: every worker then simply counts as not sharing it,
        # which is the safe side of the answer.
        return ""
    return value
