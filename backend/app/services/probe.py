from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from .naming import parse_filename

log = logging.getLogger(__name__)

# Data-track tags carrying telemetry Gyroflow can use. `djmd` = DJI,
# `gpmd` = GoPro, `camm`/`mett` = assorted.
# `dbgi` (DJI debug) is deliberately absent: it travels alongside `djmd` but
# carries no telemetry, so counting it would produce false positives.
GYRO_DATA_TAGS = {"djmd", "gpmd", "camm", "mett", "rtmd"}

# Bytes read from the head and tail for the fingerprint. Hashing 3.7 GB on every
# scan would be absurd; size + both ends is enough to tell two rushes apart.
FINGERPRINT_CHUNK = 1 << 20


class ProbeError(RuntimeError):
    pass


@dataclass
class ProbeResult:
    duration_ms: float
    width: int
    height: int
    fps_num: int
    fps_den: int
    codec: str
    size_bytes: int
    has_gyro: bool
    data_tags: list[str] = field(default_factory=list)
    recorded_at: datetime | None = None
    camera_index: int | None = None


def _run_ffprobe(path: Path) -> dict:
    cmd = [
        settings.ffprobe_bin, "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed on {path.name}: {proc.stderr.strip()[:400]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"sortie ffprobe illisible pour {path.name}") from exc


def _parse_rational(value: str | None, default: tuple[int, int] = (0, 1)) -> tuple[int, int]:
    if not value or "/" not in value:
        return default
    num, den = value.split("/", 1)
    try:
        n, d = int(num), int(den)
    except ValueError:
        return default
    if n <= 0 or d <= 0:
        return default
    return n, d


def _pick_video_stream(streams: list[dict]) -> dict:
    """Careful: DJI rushes embed a 960x720 mjpeg thumbnail next to the main
    stream. Pick the largest video stream, not the first one."""
    videos = [
        s for s in streams
        if s.get("codec_type") == "video"
        and s.get("disposition", {}).get("attached_pic", 0) != 1
    ]
    if not videos:
        raise ProbeError("no video stream")
    return max(videos, key=lambda s: int(s.get("width") or 0) * int(s.get("height") or 0))


def probe(path: Path) -> ProbeResult:
    data = _run_ffprobe(path)
    streams = data.get("streams", [])
    video = _pick_video_stream(streams)

    fps_num, fps_den = _parse_rational(video.get("r_frame_rate"))
    if fps_num == 0:
        fps_num, fps_den = _parse_rational(video.get("avg_frame_rate"), (30, 1))

    duration_s = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)

    tags = [
        (s.get("codec_tag_string") or "").strip()
        for s in streams
        if s.get("codec_type") == "data"
    ]
    tags = [t for t in tags if t]

    parsed = parse_filename(path)
    recorded_at = parsed.recorded_at
    if recorded_at is None:
        # Fallback: mtime is when writing ended, so walk back by the duration.
        recorded_at = datetime.fromtimestamp(path.stat().st_mtime - duration_s, tz=timezone.utc)

    return ProbeResult(
        duration_ms=duration_s * 1000.0,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps_num=fps_num,
        fps_den=fps_den,
        codec=video.get("codec_name") or "",
        size_bytes=path.stat().st_size,
        has_gyro=any(t.lower() in GYRO_DATA_TAGS for t in tags),
        data_tags=tags,
        recorded_at=recorded_at,
        camera_index=parsed.camera_index,
    )


def fingerprint_parts(size: int, head: bytes, tail: bytes) -> str:
    """Fingerprint from bytes already in hand, rather than from a file.

    Split out so the upload pre-flight can identify a file the browser holds
    without sending it: 2 MiB of probe bytes are enough, and going through the
    very same function means the two paths can never drift apart.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    h.update(head)
    h.update(tail)
    return h.hexdigest()


def fingerprint_lengths(size: int) -> tuple[int, int]:
    """How many bytes the head and tail chunks hold for a file of `size`."""
    head = min(size, FINGERPRINT_CHUNK)
    tail = FINGERPRINT_CHUNK if size > 2 * FINGERPRINT_CHUNK else 0
    return head, tail


def fingerprint(path: Path) -> str:
    """Cheap fingerprint: size + 1 MiB from the head + 1 MiB from the tail."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(FINGERPRINT_CHUNK)
        tail = b""
        if size > 2 * FINGERPRINT_CHUNK:
            fh.seek(-FINGERPRINT_CHUNK, 2)
            tail = fh.read(FINGERPRINT_CHUNK)
    return fingerprint_parts(size, head, tail)
