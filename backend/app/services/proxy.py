"""Building proxies and filmstrips.

The master is HEVC 10-bit 3840x2880, which no browser can play. Derushing
therefore happens on an 8-bit H.264 proxy: smaller, and steppable frame by frame.

The measurements that dictate the command line, on an AMD iGPU (Radeon 890M):
- **VAAPI** decode + CPU scale + x264 veryfast: 0.79x realtime
- CPU-only decode: 0.53x
- a full VAAPI chain (`scale_vaapi`, `h264_vaapi`): **hangs or segfaults** on
  AMD iGPU + Mesa. So the GPU is used for decoding only.

The backend is not fixed: `capabilities.detect()` probes NVDEC (`cuda`) and VAAPI
by really decoding, and this module just spends whatever it was handed. Only the
decoding is ever accelerated: scale and encode stay on the CPU on every vendor,
which is the lesson of the `scale_vaapi` hang above.

Beware: even VAAPI *decoding* wedged the amdgpu driver on a real 3840x2880
HEVC 10-bit stream (unkillable ffmpeg, GPU stuck). Hence `VS_HWACCEL=cpu` as the
recommended default on that hardware: the 1.6x gain is not worth the risk.

The filmstrip is extracted **from the proxy**, not the master: same picture, ten
times cheaper to decode.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .capabilities import Capabilities
from .probe import probe
from .procs import ProgressCallback, run_with_progress

log = logging.getLogger(__name__)

_FFMPEG_FRAME = re.compile(r"^frame=\s*(\d+)")


@dataclass
class ProxyResult:
    path: Path
    width: int
    height: int
    log_tail: str = ""


def _decode_flags(caps: Capabilities) -> list[str]:
    """Flags for whichever backend the probe settled on.

    Nothing is checked here: `capabilities.detect()` has already decoded an HEVC
    10-bit sample through this exact path on this exact machine. Scaling and
    encoding stay on the CPU whatever the backend, for the reasons above.
    """
    if caps.decode_backend == "cuda":
        return ["-hwaccel", "cuda"]
    if caps.decode_backend == "vaapi" and caps.decode_device:
        return ["-hwaccel", "vaapi", "-hwaccel_device", caps.decode_device]
    return []


def build_proxy(
    source: Path,
    dest: Path,
    caps: Capabilities,
    frame_count: int,
    fps_num: int,
    fps_den: int,
    progress_cb: ProgressCallback | None = None,
) -> ProxyResult:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial.mp4")
    tmp.unlink(missing_ok=True)

    fps = (fps_num / fps_den) if fps_den else 30.0
    # One-second GOP: the browser seeks the keyframe then decodes forward.
    # Shorter means snappier scrubbing but a bigger file.
    gop = max(2, round(fps))

    cmd = [
        settings.ffmpeg_bin, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
        *_decode_flags(caps),
        "-i", str(source),
        "-map", "0:v:0", "-an", "-dn", "-sn",
        "-vf", f"scale=-2:{settings.proxy_height}",
        "-c:v", "libx264", "-preset", settings.proxy_x264_preset,
        "-crf", str(settings.proxy_crf), "-pix_fmt", "yuv420p",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        str(tmp),
    ]

    def on_line(line: str) -> float | None:
        if frame_count > 0 and (m := _FFMPEG_FRAME.match(line)):
            return int(m.group(1)) / frame_count
        return None

    try:
        log_tail = run_with_progress(cmd, on_line, progress_cb, timeout=6 * 3600)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    if not tmp.exists():
        raise RuntimeError("ffmpeg n'a produit aucun proxy")
    tmp.replace(dest)

    info = probe(dest)
    log.info("Proxy %s : %dx%d", dest.name, info.width, info.height)
    return ProxyResult(path=dest, width=info.width, height=info.height, log_tail=log_tail)


def build_filmstrip(
    proxy_path: Path,
    dest: Path,
    duration_ms: float,
    columns: int | None = None,
    thumb_height: int | None = None,
) -> Path:
    """A single image, N thumbnails side by side, shown under the slider."""
    columns = columns or settings.filmstrip_columns
    thumb_height = thumb_height or settings.filmstrip_thumb_height
    duration_s = max(duration_ms / 1000.0, 0.001)
    rate = max(columns / duration_s, 0.001)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial.jpg")
    tmp.unlink(missing_ok=True)

    cmd = [
        settings.ffmpeg_bin, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
        "-i", str(proxy_path),
        "-vf", f"fps={rate:.6f},scale=-1:{thumb_height},tile={columns}x1",
        "-frames:v", "1", "-q:v", "4",
        str(tmp),
    ]
    run_with_progress(cmd, timeout=3600)
    if not tmp.exists():
        raise RuntimeError("ffmpeg n'a produit aucune pellicule")
    tmp.replace(dest)
    return dest


def build_poster(proxy_path: Path, dest: Path, duration_ms: float) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial.jpg")
    tmp.unlink(missing_ok=True)
    at_s = max(0.0, duration_ms / 1000.0 * 0.25)
    cmd = [
        settings.ffmpeg_bin, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
        "-ss", f"{at_s:.3f}", "-i", str(proxy_path),
        "-frames:v", "1", "-vf", "scale=-2:360", "-q:v", "4",
        str(tmp),
    ]
    run_with_progress(cmd, timeout=600)
    if not tmp.exists():
        raise RuntimeError("ffmpeg n'a produit aucune vignette")
    tmp.replace(dest)
    return dest
