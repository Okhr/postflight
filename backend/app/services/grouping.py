"""Grouping the parts of a single recording.

Cameras cut the recording at ~3.7 GB. Two consecutive parts are recognizable by
the fact that **the second one starts exactly when the first one ends**. Measured
on a real O4 pair: 0.36 s apart. The 2 s default tolerance leaves room for the
file switch without risking gluing two separate flights together (nobody takes
off again within the second).

Gyroflow's built-in detection is useless here: its DJI pattern
`/(DJI_\\d+_(\\d+)\\.MP4)$/` targeted DJI Action cameras and fails on the `_D`
suffix of goggles files (`DJI_20260811144828_0044_D.MP4`).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


@dataclass
class ClipInfo:
    id: int
    filename: str
    recorded_at: datetime
    duration_ms: float
    width: int
    height: int
    fps_num: int
    fps_den: int
    codec: str
    size_bytes: int = 0
    camera_index: int | None = None
    group_key: str | None = None


def _same_profile(a: ClipInfo, b: ClipInfo) -> bool:
    return (
        (a.width, a.height, a.fps_num, a.fps_den, a.codec)
        == (b.width, b.height, b.fps_num, b.fps_den, b.codec)
    )


def _indices_consecutive(a: ClipInfo, b: ClipInfo) -> bool:
    if a.camera_index is None or b.camera_index is None:
        return True  # signal unavailable: fall back on timing alone
    return b.camera_index == a.camera_index + 1


def _reached_the_file_limit(clip: ClipInfo, min_bytes: int) -> bool:
    """Could this part have been cut short by the camera, rather than by the pilot?

    A part that stopped well below the file limit stopped because recording stopped.
    This is the signal that actually separates the two cases, and the timing barely
    does. Measured on 179 consecutive pairs of a real 622 GB O3 collection:

    - the **51 genuine splits** all have a first part between 3.763 and 3.770 Go,
      lasting 193.7 to 196.8 s, and follow within 0.79 s at the very most;
    - the **9 pairs the timing alone glued wrongly** have a first part of 1.398 Go at
      most, and follow between 1.11 and 1.96 s later.

    So the gap leaves 0.32 s between the two populations while the size leaves a
    factor of 2.7. Going on timing alone merged 9 pairs of unrelated flights out of
    60, which then get smoothed as one curve and cut as one rush.
    """
    if not clip.size_bytes:
        return True  # signal unavailable: fall back on timing alone
    return clip.size_bytes >= min_bytes


def _contiguous(a: ClipInfo, b: ClipInfo, tolerance_s: float, min_part_bytes: int = 0) -> bool:
    if not _same_profile(a, b) or not _indices_consecutive(a, b):
        return False
    if not _reached_the_file_limit(a, min_part_bytes):
        return False
    if (a.group_key or "") != (b.group_key or ""):
        return False
    expected_start = a.recorded_at + timedelta(milliseconds=a.duration_ms)
    gap_s = abs((b.recorded_at - expected_start).total_seconds())
    return gap_s <= tolerance_s


def chain_clips(
    clips: list[ClipInfo], tolerance_s: float, min_part_bytes: int = 0
) -> list[list[ClipInfo]]:
    """Split the list into groups of contiguous parts, in chronological order."""
    ordered = sorted(
        clips,
        key=lambda c: (c.recorded_at, c.camera_index if c.camera_index is not None else 0, c.filename),
    )
    groups: list[list[ClipInfo]] = []
    for clip in ordered:
        if groups and _contiguous(groups[-1][-1], clip, tolerance_s, min_part_bytes):
            groups[-1].append(clip)
        else:
            groups.append([clip])
    return groups


def describe_group(group: list[ClipInfo]) -> str:
    if len(group) == 1:
        return f"{group[0].filename} (1 part)"
    names = ", ".join(c.filename for c in group)
    total_s = sum(c.duration_ms for c in group) / 1000
    return f"{len(group)} parts ({total_s:.1f}s): {names}"


def sequence_hash(fingerprints: list[str]) -> str:
    """Content identity of a merged sequence, from its ordered part fingerprints.

    Deliberately cheap: no need to read the merged file, or even to have produced
    it. The same parts in the same order always yield the same merged bytes, so
    this answers "have we already merged exactly this?" before doing any work, and
    it stays stable when the sequence row is deleted and rebuilt.

    Order matters: two parts joined the other way round are a different video.
    """
    digest = hashlib.blake2b(digest_size=6)
    for fingerprint in fingerprints:
        digest.update(fingerprint.encode())
        digest.update(b"\0")
    return digest.hexdigest()
