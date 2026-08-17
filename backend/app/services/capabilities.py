"""Hardware capability detection, run when the worker starts.

The stack has to run both on a machine with no GPU and on a host with `/dev/dri`
mapped in. Rather than trusting an environment variable, we probe for real: build
a small HEVC 10-bit sample and try to decode it through VAAPI. The result drives
the ffmpeg command lines and is surfaced in the UI.

Gyroflow fends for itself: it tries OpenCL, then wgpu, then falls back to the CPU.
Measured in-container on a 3840x2880 → 1080p rush: **23 fps** with `/dev/dri`
(rusticl), **~8.7 fps** on the CPU alone. So we never force it — we just read its
render logs to see what it picked.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)

_OPENCL_ICD_DIR = Path("/etc/OpenCL/vendors")
_DRI_DIR = Path("/dev/dri")


@dataclass
class Capabilities:
    ffmpeg_version: str = ""
    gyroflow_version: str = ""
    mp4_merge_available: bool = False
    dri_devices: list[str] = field(default_factory=list)
    opencl_icds: list[str] = field(default_factory=list)
    vaapi_decode: bool = False
    vaapi_device: str | None = None
    notes: list[str] = field(default_factory=list)
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def hwaccel(self) -> str:
        return "vaapi" if self.vaapi_decode else "cpu"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["detected_at"] = self.detected_at.isoformat()
        data["hwaccel"] = self.hwaccel
        return data


_cache: Capabilities | None = None


def _run(cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _tool_version(binary: str, args: list[str], pattern: str) -> str:
    if not shutil.which(binary):
        return ""
    try:
        proc = _run([binary, *args], timeout=60)
    except (subprocess.SubprocessError, OSError):
        return ""
    blob = f"{proc.stdout}\n{proc.stderr}"
    if m := re.search(pattern, blob, re.I):
        return m.group(0).strip()
    return ""


def _probe_vaapi(device: str) -> tuple[bool, str]:
    """Build a HEVC 10-bit sample and try to decode it through VAAPI.

    Probing for real is necessary: on some setups (AMD iGPU + Mesa) VAAPI
    *decoding* works while `scale_vaapi`/`h264_vaapi` hang. So the GPU is only
    ever used to decode, never to scale or encode.

    Even then, decoding a real 3840x2880 HEVC 10-bit stream has been seen to wedge
    the amdgpu driver, which this small sample does not catch — `VS_HWACCEL=cpu`
    stays the safe choice on that hardware.
    """
    if not Path(device).exists():
        return False, f"{device} is missing"

    sample = settings.tmp_dir / "caps_probe_hevc10.mp4"
    try:
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        make = _run([
            settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-t", "0.5",
            "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
            "-x265-params", "log-level=none", str(sample),
        ], timeout=120)
        if make.returncode != 0 or not sample.exists():
            return False, "could not build the HEVC 10-bit test sample"

        decode = _run([
            settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostats",
            "-hwaccel", "vaapi", "-hwaccel_device", device,
            "-i", str(sample), "-f", "null", "-",
        ], timeout=120)
        if decode.returncode == 0:
            return True, ""
        return False, f"VAAPI decoding refused: {decode.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        return False, "VAAPI decoding hung (timeout) → falling back to CPU"
    except OSError as exc:
        return False, f"VAAPI probe failed: {exc}"
    finally:
        sample.unlink(missing_ok=True)


def detect(force: bool = False) -> Capabilities:
    global _cache
    if _cache is not None and not force:
        return _cache

    caps = Capabilities()
    caps.ffmpeg_version = _tool_version(settings.ffmpeg_bin, ["-version"], r"ffmpeg version \S+")
    caps.gyroflow_version = _tool_version(settings.gyroflow_bin, ["--version"], r"Gyroflow v\S+")
    caps.mp4_merge_available = shutil.which(settings.mp4_merge_bin) is not None

    if _DRI_DIR.is_dir():
        caps.dri_devices = sorted(p.name for p in _DRI_DIR.iterdir() if p.name.startswith("render"))
    if _OPENCL_ICD_DIR.is_dir():
        caps.opencl_icds = sorted(p.name for p in _OPENCL_ICD_DIR.glob("*.icd"))

    # With no render node, neither VAAPI nor rusticl has any hardware to talk to:
    # probing is pointless, and the message should say what to do rather than
    # report a missing file.
    if not caps.dri_devices:
        caps.notes.append(
            "no GPU mapped into the container: decoding and stabilization run on "
            "the CPU (about 3x slower). If the host has /dev/dri, restart with "
            "-f docker-compose.yml -f docker-compose.gpu.yml."
        )

    mode = settings.hwaccel.lower()
    if mode == "cpu":
        caps.notes.append("hwaccel pinned to CPU by configuration")
    elif mode not in {"auto", "vaapi"}:
        caps.notes.append(f"unknown hwaccel value ({settings.hwaccel}) → falling back to CPU")
    elif not caps.dri_devices:
        if mode == "vaapi":
            caps.notes.append("VAAPI requested but no GPU mapped in → falling back to CPU")
    else:
        device = settings.vaapi_device
        if not Path(device).exists():
            # The render node is not necessarily called renderD128: on a machine
            # with two GPUs, the first one exposed may be renderD129.
            device = str(_DRI_DIR / caps.dri_devices[0])
            caps.notes.append(f"{settings.vaapi_device} is missing, probing {device} instead")
        ok, note = _probe_vaapi(device)
        caps.vaapi_decode = ok
        caps.vaapi_device = device if ok else None
        if note:
            caps.notes.append(note)
        if mode == "vaapi" and not ok:
            caps.notes.append("VAAPI requested but unavailable → falling back to CPU")

    if not caps.mp4_merge_available:
        caps.notes.append(
            "mp4_merge is missing: cannot join split rushes without losing the gyro"
        )
    if not caps.opencl_icds:
        caps.notes.append("no OpenCL ICD: Gyroflow will render on the CPU (~3x slower)")

    log.info(
        "Capabilities: hwaccel=%s dri=%s opencl=%s gyroflow=%s",
        caps.hwaccel, caps.dri_devices or "-", caps.opencl_icds or "-", caps.gyroflow_version or "?",
    )
    for note in caps.notes:
        log.warning("Capabilities: %s", note)

    _cache = caps
    return caps
