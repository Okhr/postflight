"""Hardware capability detection, run when the worker starts.

One image has to run well on whatever machine it lands on: an AMD iGPU, an NVIDIA
card, an Intel iGPU, or nothing at all. So nothing here is assumed from an
environment variable or from the presence of a driver file: every accelerated path
is **probed by running it**, and whatever fails is simply not used.

That is not caution for its own sake. Measured on 2026-08-17 on the dev machine of
the day (i7-7700K + RTX 3090, driver 535.261.03): kernel module and userspace at
the same version, `/dev/nvidia*` present and world-writable, no NVRM error in the
kernel log, `nvidia-smi` perfectly happy, and `cuInit(0)` returning
`CUDA_ERROR_UNKNOWN`, on the host, outside any container. Every static check said
"GPU ready"; only running a decode caught it.

Two independent paths, detected separately because one can work while the other
does not:

- **ffmpeg decode** for proxies: `cuda` (NVDEC), then `vaapi` (AMD radeonsi and
  Intel iHD/i965), then the CPU. NVDEC first because a discrete card beats an
  iGPU on the machines that have both.
- **Gyroflow's warp**: we do not drive it. Gyroflow tries OpenCL, then wgpu (which
  means Vulkan), then the CPU on its own, and `render()` reads back from its log
  what it picked. What we do here is answer the question the UI needs *before* a
  render: is there a real **GPU** device on either path, or only a CPU one?
  Counting the installed ICD files answers that wrong. The image ships five of
  them, and on a machine whose driver stack was wedged they enumerated exactly one
  device: the CPU.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)

_OPENCL_ICD_DIR = Path("/etc/OpenCL/vendors")
_DRI_DIR = Path("/dev/dri")
_NVIDIA_DEV = Path("/dev/nvidia0")

# Decode backends, in the order they are tried when `PF_HWACCEL=auto`.
DECODE_BACKENDS = ("cuda", "vaapi")

# The probe sample: HEVC 10-bit, because that is what the rushes are and because
# 10-bit is exactly where hardware support gets patchy (a decoder that handles
# 8-bit HEVC may refuse Main10). 640x480 rather than something smaller: NVDEC has
# a minimum frame size, and a probe that failed only for being tiny would disable
# a working GPU.
_SAMPLE_SIZE = "640x480"

# What proves the hardware really did the decoding, per backend.
#
# The exit code is not enough, measured on ffmpeg 7.1.1: asking for `-hwaccel cuda`
# on a codec the chip cannot handle exits **0** and decodes in software, and adding
# `-hwaccel_output_format cuda` does not change that. A card too old for HEVC Main10
# would therefore be labelled CUDA while every proxy ran on the CPU, which is
# exactly the lie this module exists to prevent. Verbose output does tell the truth:
# `NVDEC capabilities:` appears when NVDEC runs and is absent when it does not.
#
# Only cuda is listed, because only cuda was measured. VAAPI keeps the exit code as
# its single signal rather than a marker invented from memory, and its device
# creation failing is what this machine proved the exit code does catch.
_HARDWARE_PROOF = {"cuda": re.compile(r"NVDEC capabilities", re.I)}


def hardware_confirmed(backend: str, log: str) -> bool:
    """Whether `log` proves the hardware ran, for backends where we know the marker."""
    proof = _HARDWARE_PROOF.get(backend)
    return proof is None or bool(proof.search(log))


@dataclass
class OpenCLDevice:
    platform: str
    name: str
    kind: str  # "GPU" | "CPU" | "ACCELERATOR" | "other"

    @property
    def is_gpu(self) -> bool:
        return self.kind == "GPU"

    def __str__(self) -> str:
        return f"{self.name} ({self.platform})"


@dataclass
class Capabilities:
    ffmpeg_version: str = ""
    gyroflow_version: str = ""
    mp4_merge_available: bool = False

    dri_devices: list[str] = field(default_factory=list)
    nvidia_present: bool = False

    # What the decode probe settled on, and the device it needs (VAAPI only).
    decode_backend: str = "cpu"
    decode_device: str | None = None
    # Why each candidate was refused, kept so the UI can explain a CPU fallback
    # instead of merely announcing it. Empty string = that backend works.
    decode_probes: dict[str, str] = field(default_factory=dict)

    opencl_icds: list[str] = field(default_factory=list)
    opencl_devices: list[OpenCLDevice] = field(default_factory=list)
    # Names of the Vulkan devices that are real GPUs. Gyroflow's second choice
    # after OpenCL, so a machine with Vulkan and no ICD still warps on its GPU.
    vulkan_devices: list[str] = field(default_factory=list)

    notes: list[str] = field(default_factory=list)
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def hwaccel(self) -> str:
        """Backend used for decoding: `cuda`, `vaapi` or `cpu`."""
        return self.decode_backend

    @property
    def opencl_gpu(self) -> OpenCLDevice | None:
        """The OpenCL GPU Gyroflow will most likely pick, if there is one."""
        return next((d for d in self.opencl_devices if d.is_gpu), None)

    @property
    def stabilize_device(self) -> str:
        """What Gyroflow will most likely warp on, in its own order of preference:
        OpenCL, then wgpu over Vulkan, then the CPU."""
        gpu = self.opencl_gpu
        if gpu:
            return str(gpu)
        if self.vulkan_devices:
            return f"{self.vulkan_devices[0]} (Vulkan)"
        return "CPU"

    @property
    def stabilize_on_gpu(self) -> bool:
        return bool(self.opencl_gpu or self.vulkan_devices)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["detected_at"] = self.detected_at.isoformat()
        data["hwaccel"] = self.hwaccel
        data["stabilize_device"] = self.stabilize_device
        data["stabilize_on_gpu"] = self.stabilize_on_gpu
        data["opencl_gpu"] = str(self.opencl_gpu) if self.opencl_gpu else None
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


# --------------------------------------------------------------------------- #
# OpenCL: what devices actually exist
# --------------------------------------------------------------------------- #

def parse_clinfo(payload: str) -> list[OpenCLDevice]:
    """Read `clinfo --json` into a flat list of devices.

    Schema, checked against clinfo 3.0.25 rather than guessed: `platforms` and
    `devices` are two **parallel** lists, and each entry of `devices` is
    `{"online": [ …device… ]}`, and devices are not nested inside their platform.
    A platform that exposes nothing (rusticl and Clover with no supported GPU)
    still gets an entry, with an empty or missing list.

    Anything unreadable yields an empty list: a chart of the hardware is a nicety,
    and must never be the reason the worker refuses to start.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    platforms = data.get("platforms") or []
    groups = data.get("devices") or []
    devices: list[OpenCLDevice] = []

    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        platform = ""
        if index < len(platforms) and isinstance(platforms[index], dict):
            platform = platforms[index].get("CL_PLATFORM_NAME") or ""
        for entry in group.get("online") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("CL_DEVICE_NAME") or ""
            if not name:
                continue
            devices.append(
                OpenCLDevice(platform=platform, name=name, kind=_device_kind(entry))
            )
    return devices


def _device_kind(entry: dict) -> str:
    """`CL_DEVICE_TYPE` is `{"raw": 2, "type": ["CL_DEVICE_TYPE_CPU"]}`."""
    raw = entry.get("CL_DEVICE_TYPE")
    names: list[str] = []
    if isinstance(raw, dict):
        names = [str(n) for n in (raw.get("type") or [])]
    elif isinstance(raw, str):
        names = [raw]
    for name in names:
        for kind in ("GPU", "CPU", "ACCELERATOR"):
            if name.endswith(kind):
                return kind
    return "other"


def parse_vulkaninfo(payload: str) -> list[str]:
    """Names of the Vulkan devices that are real GPUs, from `vulkaninfo --summary`.

    Gyroflow tries OpenCL first and **wgpu**, which means Vulkan, second. A machine
    with a working Vulkan driver and no OpenCL ICD therefore still warps on its GPU,
    and reporting only OpenCL would repeat one layer down the very lie this module
    exists to kill.

    Format, captured on vulkan-tools 1.4 rather than guessed: a `Devices:` section,
    one `GPUn:` block per device, then indented `key = value` lines. Mesa's software
    rasterizer announces itself as PHYSICAL_DEVICE_TYPE_CPU, which is how it gets
    filtered out despite being listed as a device like any other.
    """
    devices: list[str] = []
    name = ""
    kind = ""

    def flush() -> None:
        if name and "GPU" in kind:
            devices.append(name)

    for raw in payload.splitlines():
        line = raw.strip()
        if line.startswith("GPU") and line.endswith(":"):
            flush()
            name, kind = "", ""
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "deviceName":
            name = value
        elif key == "deviceType":
            kind = value
    flush()
    return devices


def _vulkan_devices() -> list[str]:
    if not shutil.which("vulkaninfo"):
        return []
    try:
        # Non-zero exit when no driver answers, which is not an error here: the
        # question was whether a GPU exists, and the answer is simply no.
        proc = _run(["vulkaninfo", "--summary"], timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("vulkaninfo unusable: %s", exc)
        return []
    return parse_vulkaninfo(proc.stdout)


def _opencl_devices() -> list[OpenCLDevice]:
    if not shutil.which("clinfo"):
        return []
    try:
        proc = _run(["clinfo", "--json"], timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("clinfo unusable: %s", exc)
        return []
    # clinfo exits non-zero when a platform reports no device, which is not an
    # error for us: the output is still valid JSON describing what does exist.
    return parse_clinfo(proc.stdout)


# --------------------------------------------------------------------------- #
# Decode: probe each backend by actually decoding
# --------------------------------------------------------------------------- #

def _build_sample() -> Path | None:
    """Encode the little HEVC 10-bit clip every decode probe runs against.

    In a private directory, and **not** in a filename built from the pid. Two
    processes probe at once (the API and the worker are started together by compose)
    and they share the volume, but each is pid 1 inside its own container: a
    pid-derived name is the same name on both sides. Measured on 2026-08-19, that is
    exactly what happened, one of them deleting the sample while the other was still
    decoding it, and NVDEC reported broken on a machine where it works. A directory
    from mkdtemp is unique by construction, with no namespace to reason about.
    """
    try:
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="caps_probe_", dir=settings.tmp_dir))
    except OSError as exc:
        log.warning("HEVC 10-bit probe sample not built: %s", exc)
        return None
    sample = scratch / "hevc10.mp4"
    try:
        made = _run(
            [
                settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc2=size={_SAMPLE_SIZE}:rate=30", "-t", "0.5",
                "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
                "-x265-params", "log-level=none", str(sample),
            ],
            timeout=180,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("HEVC 10-bit probe sample not built: %s", exc)
        shutil.rmtree(scratch, ignore_errors=True)
        return None
    if made.returncode != 0 or not sample.exists():
        shutil.rmtree(scratch, ignore_errors=True)
        return None
    return sample


def _complaints(log: str) -> str:
    """The lines worth showing out of a verbose ffmpeg log."""
    lines = [
        line.strip()
        for line in log.splitlines()
        if re.search(r"error|failed|unknown|cannot|no device", line, re.I)
    ]
    return "\n".join(lines[-4:])[:300] or log.strip()[-300:]


def _try_decode(sample: Path, backend: str, flags: list[str]) -> tuple[bool, str]:
    """Decode the sample through `flags`, and say why if the hardware did not run.

    Verbose on purpose: the exit code catches a device that cannot be created, and
    the log is the only thing that catches a flag ffmpeg accepted and then ignored.
    See `_HARDWARE_PROOF`.
    """
    try:
        proc = _run(
            [
                settings.ffmpeg_bin, "-hide_banner", "-loglevel", "verbose", "-nostats",
                *flags, "-i", str(sample), "-f", "null", "-",
            ],
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        # A hung decode is worse than a slow one: it wedged a GPU on this project
        # once already. Treat it as unusable.
        return False, "the decode hung (timeout)"
    except OSError as exc:
        return False, f"probe failed: {exc}"

    log = f"{proc.stderr}\n{proc.stdout}"
    if proc.returncode != 0:
        return False, _complaints(log)
    if not hardware_confirmed(backend, log):
        return False, (
            "ffmpeg accepted the flag and then decoded in software: this chip does "
            "not handle HEVC 10-bit"
        )
    return True, ""


def _vaapi_device(caps: Capabilities) -> str:
    """The render node to probe. Not necessarily renderD128: on a machine with two
    GPUs the first one exposed may be renderD129."""
    configured = settings.vaapi_device
    if Path(configured).exists():
        return configured
    fallback = str(_DRI_DIR / caps.dri_devices[0])
    caps.notes.append(f"{configured} is missing, probing {fallback} instead")
    return fallback


def _probe_backend(name: str, sample: Path, caps: Capabilities) -> tuple[bool, str]:
    if name == "cuda":
        if not caps.nvidia_present:
            return False, "no /dev/nvidia0: no NVIDIA GPU in this container"
        return _try_decode(sample, name, ["-hwaccel", "cuda"])
    if name == "vaapi":
        if not caps.dri_devices:
            return False, "no render node in /dev/dri"
        device = _vaapi_device(caps)
        ok, why = _try_decode(sample, name, ["-hwaccel", "vaapi", "-hwaccel_device", device])
        if ok:
            caps.decode_device = device
        return ok, why
    return False, f"unknown backend {name}"


def backends_to_probe(mode: str) -> tuple[list[str], str]:
    """Turn a `PF_HWACCEL` value into the candidates worth probing, plus a note.

    Split out from the probing itself so the policy is testable without a GPU, an
    ffmpeg, or any particular machine: everything below this function needs
    hardware to say anything, this one does not.
    """
    mode = (mode or "auto").strip().lower()
    if mode == "cpu":
        return [], "decoding pinned to the CPU by configuration"
    if mode == "auto":
        return list(DECODE_BACKENDS), ""
    if mode in DECODE_BACKENDS:
        return [mode], ""
    return [], f"unknown PF_HWACCEL value ({mode}) → CPU decoding"


def _detect_decode(caps: Capabilities) -> None:
    """Settle on a decode backend, probing only what is worth probing."""
    wanted, note = backends_to_probe(settings.hwaccel)
    if note:
        caps.notes.append(note)
    if not wanted:
        return

    sample = _build_sample()
    if sample is None:
        caps.notes.append("HEVC 10-bit probe sample could not be built → CPU decoding")
        return

    try:
        for name in wanted:
            ok, why = _probe_backend(name, sample, caps)
            caps.decode_probes[name] = "" if ok else why
            if ok:
                caps.decode_backend = name
                return
            log.info("Decode backend %s unusable: %s", name, why.replace("\n", " ")[:160])
    finally:
        shutil.rmtree(sample.parent, ignore_errors=True)

    if len(wanted) == 1:
        caps.notes.append(f"{wanted[0]} requested but unusable → CPU decoding")


# --------------------------------------------------------------------------- #

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
    caps.nvidia_present = _NVIDIA_DEV.exists()

    _detect_decode(caps)
    caps.opencl_devices = _opencl_devices()
    caps.vulkan_devices = _vulkan_devices()

    if not caps.dri_devices and not caps.nvidia_present:
        caps.notes.append(
            "no GPU mapped into the container: decoding and stabilization run on "
            "the CPU. If the host has one, restart with the matching override: "
            "docker-compose.gpu.yml for AMD/Intel, docker-compose.nvidia.yml for NVIDIA."
        )
    if not caps.mp4_merge_available:
        caps.notes.append(
            "mp4_merge is missing: cannot join split rushes without losing the gyro"
        )
    if caps.opencl_gpu is None and caps.vulkan_devices:
        caps.notes.append(
            f"no OpenCL GPU, but Vulkan exposes {caps.vulkan_devices[0]}: Gyroflow "
            "should fall back to wgpu, which is a GPU path even if we have not "
            "measured it against OpenCL. Its render log says which one it took."
        )
    elif caps.opencl_gpu is None:
        # The interesting half of the message is *why*, and the ICD list is what
        # says whether the vendor's driver ever made it into the container.
        caps.notes.append(
            "no OpenCL GPU and no Vulkan GPU: Gyroflow will warp on the CPU (about "
            f"3x slower). ICDs installed: {', '.join(caps.opencl_icds) or 'none'}."
        )

    log.info(
        "Capabilities: decode=%s%s stabilize=%s dri=%s nvidia=%s gyroflow=%s",
        caps.decode_backend,
        f" ({caps.decode_device})" if caps.decode_device else "",
        caps.stabilize_device,
        caps.dri_devices or "-",
        caps.nvidia_present,
        caps.gyroflow_version or "?",
    )
    for note in caps.notes:
        log.warning("Capabilities: %s", note)

    _cache = caps
    return caps
