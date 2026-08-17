"""Camera filename parsing.

The non-obvious part, measured on real O4 rushes: **the timestamp in the name is
UTC**, while the file `mtime` is the *local* time at which writing ended
(≈ start + duration). Both are useful: the name gives the exact start, the mtime
gives an independent cross-check when the name cannot be read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# DJI O3/O4 (goggles): DJI_20260811144828_0044_D.MP4
_DJI_GOGGLES = re.compile(r"^DJI_(?P<ts>\d{14})_(?P<idx>\d{3,5})(?:_(?P<suffix>[A-Z]+))?$", re.I)
# DJI Action / Mini: DJI_0001_0044.MP4  (no timestamp)
_DJI_ACTION = re.compile(r"^DJI_(?P<group>\d{4})_(?P<idx>\d{4})$", re.I)
# GoPro 6+: GX010044.MP4 (chapter 01, clip 0044); GoPro 1-5: GOPR0044 / GP010044
_GOPRO_NEW = re.compile(r"^G[XH](?P<chapter>\d{2})(?P<clip>\d{4})$", re.I)
_GOPRO_OLD = re.compile(r"^(?:GOPR|GP(?P<chapter>\d{2}))(?P<clip>\d{4})$", re.I)


@dataclass(frozen=True)
class ParsedName:
    recorded_at: datetime | None      # UTC
    camera_index: int | None          # index that increments from one part to the next
    group_key: str | None             # what must match between two parts
    kind: str


def parse_filename(path: Path | str) -> ParsedName:
    stem = Path(path).stem

    if m := _DJI_GOGGLES.match(stem):
        try:
            recorded = datetime.strptime(m.group("ts"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            recorded = None
        return ParsedName(recorded, int(m.group("idx")), m.group("suffix") or "", "dji_goggles")

    if m := _DJI_ACTION.match(stem):
        return ParsedName(None, int(m.group("idx")), m.group("group"), "dji_action")

    if m := _GOPRO_NEW.match(stem):
        # On GoPro it is the *chapter* that increments; the clip number stays put.
        return ParsedName(None, int(m.group("chapter")), m.group("clip"), "gopro")

    if m := _GOPRO_OLD.match(stem):
        chapter = m.group("chapter")
        return ParsedName(None, int(chapter) if chapter else 0, m.group("clip"), "gopro_old")

    return ParsedName(None, None, None, "unknown")


def sequence_key(filename: str) -> str:
    """A stable, readable key naming a sequence, derived from its first part."""
    return Path(filename).stem
