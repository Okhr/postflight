"""Joining the parts of a sequence without losing the telemetry.

The crucial part, verified by hand: **ffmpeg cannot remux these files**. The
`djmd` stream (the gyro) has codec `none`, which both mp4 and mkv reject:

    mp4: "Could not find tag for codec none in stream #1"
    mkv: "Only audio, video and subtitles are supported for Matroska"

So we use `mp4_merge`, the very tool Gyroflow uses internally: it concatenates
the raw `mdat` boxes and rewrites `stbl` (`stts`/`stsz`/`stss`/`stsc`,
`stco`→`co64`), patching the durations in `mvhd`/`tkhd`/`mdhd`. Every track
survives. Measured: 4.4 s for 4 GB, gyro continuous on the way out.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .probe import ProbeResult, probe
from .procs import ProgressCallback, run_with_progress

log = logging.getLogger(__name__)

_MERGE_PROGRESS = re.compile(r"Merging\.\.\.\s*([\d.]+)\s*%")

# Tolerated gap between the merged duration and the sum of the parts.
DURATION_TOLERANCE_MS = 250.0


class MergeError(RuntimeError):
    pass


@dataclass
class MergeResult:
    path: Path
    probe: ProbeResult
    method: str          # "hardlink" | "copy" | "mp4_merge"
    log_tail: str = ""


def _link_or_copy(source: Path, dest: Path, progress_cb: ProgressCallback | None) -> str:
    """Single-part sequence: no merge, just a link.

    A hardlink costs zero bytes when `raw/` and `merged/` sit on the same
    filesystem, which is the default. Otherwise we copy.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.link(source, dest)
        return "hardlink"
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK):
            raise
        log.info("hardlink impossible (%s), copie de %s", exc.strerror, source.name)
        if progress_cb:
            progress_cb(0.05, f"copie de {source.name}")
        shutil.copy2(source, dest)
        return "copy"


def merge_parts(
    parts: list[Path],
    dest: Path,
    progress_cb: ProgressCallback | None = None,
) -> MergeResult:
    if not parts:
        raise MergeError("no part to merge")
    for part in parts:
        if not part.exists():
            raise MergeError(f"part manquante : {part}")

    per_part = [probe(p) for p in parts]
    expected_ms = sum(p.duration_ms for p in per_part)
    gyro_expected = any(p.has_gyro for p in per_part)

    dest.parent.mkdir(parents=True, exist_ok=True)
    log_tail = ""

    if len(parts) == 1:
        method = _link_or_copy(parts[0], dest, progress_cb)
    else:
        if not shutil.which(settings.mp4_merge_bin):
            raise MergeError(
                "mp4_merge not found; ffmpeg cannot join these files without "
                "destroying the gyro stream"
            )
        tmp = dest.with_suffix(".partial.mp4")
        tmp.unlink(missing_ok=True)
        cmd = [settings.mp4_merge_bin, *[str(p) for p in parts], "--out", str(tmp)]

        def on_line(line: str) -> float | None:
            if m := _MERGE_PROGRESS.search(line):
                return float(m.group(1)) / 100.0
            return None

        try:
            log_tail = run_with_progress(cmd, on_line, progress_cb, timeout=3600)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        if not tmp.exists():
            raise MergeError(f"mp4_merge n'a produit aucun fichier\n{log_tail}")
        tmp.replace(dest)
        method = "mp4_merge"

    result = probe(dest)

    if abs(result.duration_ms - expected_ms) > DURATION_TOLERANCE_MS:
        raise MergeError(
            f"merged duration is off: {result.duration_ms:.0f} ms "
            f"instead of the expected {expected_ms:.0f} ms"
        )
    if gyro_expected and not result.has_gyro:
        raise MergeError(
            "the gyro stream vanished during the merge: Gyroflow could not "
            "stabilize this file"
        )

    log.info(
        "Merge %s: %d part(s) → %s (%.1fs, gyro=%s)",
        method, len(parts), dest.name, result.duration_ms / 1000, result.has_gyro,
    )
    return MergeResult(path=dest, probe=result, method=method, log_tail=log_tail)
