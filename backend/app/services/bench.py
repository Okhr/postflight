"""What this machine can actually do, measured by doing it.

`capabilities.py` answers "is there a GPU here". This module answers the question
the dispatcher actually needs, which is "how fast is this machine at each of the four
jobs". They are not the same question, and treating them as one would have got the
scheduling backwards: measured on 2026-08-19, during a Gyroflow render the RTX 3090
sat at 13% while the CPU was at 676% of 800%. An RTX 3090 renders at the same rate as
a Radeon 890M iGPU, because on neither machine is the GPU what limits the render. No
prior derived from hardware could have known that. Running the work does.

So every rate here comes from running the real service on a real rush: 0.5 s of DJI
O3 footage baked into the worker image. Real, because a render needs a gyro track and
nothing can synthesize or trim one. ffmpeg refuses to mux the `djmd` stream at all
(codec `none`), and copying it into a MOV carries the bytes but loses the track
identity, after which Gyroflow reads no gyro whatsoever.

**These numbers rank machines, they do not predict durations.** Measured with this
very code: 27 img/s on the benchmark clip against 22.7 img/s on a real 272 s
sequence, and a proxy at 56 img/s against 0.9x realtime on a real one. A clip this
short never leaves the page cache and spends a visible share of its life in process
startup, so every rate here is optimistic by a roughly fixed factor. Harmless for
choosing between two machines, which is all the dispatcher does with it, and
`dispatch.observe` replaces the estimate with the real thing as soon as real jobs
finish.

**Why so short.** Tested both: 0.5 s (30 frames) reports the render at 27.2 then 26.8
img/s across two runs, a 1.8 s clip at 38.8 then 36.4. The short clip is *more*
repeatable, weighs 9 MB instead of 25, and halves the startup cost.

**A missing rate never means "cannot do this".** A benchmark step can fail for
reasons that have nothing to do with the real job (no clip in the image, a tool
absent in a dev checkout). Capability gating lives in `capabilities.py`; this module
only ranks, and an unknown rate is treated as unknown by `dispatch.rate_for`.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..framing import duration_to_frames
from . import grading as grading_service
from . import gyroflow as gyroflow_service
from . import merge as merge_service
from . import proxy as proxy_service
from .capabilities import Capabilities
from .probe import probe

log = logging.getLogger(__name__)

MB = 1 << 20

# 1080p landscape, the format nearly every real render asks for. Measuring another
# output size would measure another machine.
BENCH_TEMPLATE = "h_1080"

# A look that exercises the whole filter chain rather than a neutral one, which
# would compile to `null` and measure a plain transcode: exposure, the shadow and
# highlight curve, eq, and colortemperature, which forces the RGB conversions a real
# grade pays for.
BENCH_LOOK: dict[str, Any] = {
    "exposure": 0.3,
    "contrast": 1.1,
    "saturation": 1.1,
    "shadows": 0.2,
    "highlights": -0.2,
    "temperature": 6000,
}


@dataclass
class Benchmark:
    """One measured rate per job kind, plus what it cost to find out."""

    clip: str = ""
    clip_frames: int = 0
    # Megabytes of input per second through mp4_merge.
    merge_mbps: float | None = None
    proxy_fps: float | None = None
    render_fps: float | None = None
    grade_fps: float | None = None
    # Filled in by the worker, not here: only that side can time a transfer to the
    # dispatcher.
    link_mbps: float | None = None
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)
    measured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["measured_at"] = self.measured_at.isoformat()
        return data


class _Rate:
    """A progress callback that also measures the rate progress moved at.

    Timed from the first reported progress to the last rather than from the process
    launch: Gyroflow spends 1.4 s of a 3.4 s benchmark render loading its 12344 lens
    profiles, and counting that would report a machine 40% slower than it is.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.first: tuple[float, float] | None = None
        self.last: tuple[float, float] | None = None

    def __call__(self, progress: float, message: str = "") -> None:
        if progress <= 0.0:
            return
        now = time.monotonic()
        if self.first is None:
            self.first = (progress, now)
        self.last = (progress, now)

    def rate(self, work: float) -> float | None:
        """`work` units per second: frames for an encode, megabytes for a merge."""
        if self.first is None or self.last is None:
            return None
        spread = self.last[0] - self.first[0]
        span = self.last[1] - self.first[1]
        if spread > 0.05 and span > 0.05:
            return spread * work / span
        # One progress line, or all of them inside a single tick. A proxy of this
        # clip finishes before ffmpeg's second `-progress` report, so this is the
        # normal path there, not an edge case: the best that can be said then is how
        # long the run took to reach the point it did report.
        span = self.last[1] - self.started
        return self.last[0] * work / span if span > 0.01 else None


# --------------------------------------------------------------------------- #
# One measurement per job kind, each running the service the real job runs
# --------------------------------------------------------------------------- #

def _merge(clip: Path, scratch: Path, bench: Benchmark) -> None:
    """mp4_merge, on the clip joined to itself.

    Optimistic and knowingly so: 20 MB never leaves the page cache, where a real
    4 GB merge is pure I/O. It still ranks machines, and it doubles as the only
    check that mp4_merge works on this one at all.
    """
    dest = scratch / "merged.mp4"
    rate = _Rate()
    started = time.monotonic()
    merge_service.merge_parts([clip, clip], dest, rate)
    elapsed = time.monotonic() - started
    megabytes = 2 * clip.stat().st_size / MB
    bench.merge_mbps = rate.rate(megabytes) or (megabytes / elapsed if elapsed else None)
    dest.unlink(missing_ok=True)


def _proxy(clip: Path, caps: Capabilities, frames: int, info: Any, scratch: Path, bench: Benchmark) -> Path:
    dest = scratch / "proxy.mp4"
    rate = _Rate()
    started = time.monotonic()
    proxy_service.build_proxy(
        clip, dest, caps,
        frame_count=frames, fps_num=info.fps_num, fps_den=info.fps_den,
        progress_cb=rate,
    )
    elapsed = time.monotonic() - started
    bench.proxy_fps = rate.rate(frames) or (frames / elapsed if elapsed else None)
    return dest


def _render(clip: Path, frames: int, scratch: Path, bench: Benchmark) -> Path:
    template = gyroflow_service.get_template(BENCH_TEMPLATE)
    rate = _Rate()
    started = time.monotonic()
    result = gyroflow_service.render(
        source=clip,
        template=template,
        trim_ranges_ms=[],
        out_dir=scratch,
        out_filename="render.mp4",
        project_path=scratch / "render.gyroflow.json",
        progress_cb=rate,
    )
    elapsed = time.monotonic() - started
    bench.render_fps = rate.rate(frames) or (frames / elapsed if elapsed else None)
    return result.out_path


def _grade(source: Path, frames: int, scratch: Path, bench: Benchmark) -> None:
    rate = _Rate()
    started = time.monotonic()
    grading_service.render(
        source, scratch / "graded.mp4", BENCH_LOOK, frame_count=frames, progress_cb=rate
    )
    elapsed = time.monotonic() - started
    bench.grade_fps = rate.rate(frames) or (frames / elapsed if elapsed else None)


# --------------------------------------------------------------------------- #

def measure(caps: Capabilities) -> Benchmark:
    """Run the four steps on the baked clip and report the rates.

    Never raises. A step that breaks leaves its rate unknown and writes down why:
    a worker that cannot be ranked must still be a worker that runs.
    """
    clip = settings.bench_clip
    bench = Benchmark(clip=clip.name)
    if not clip.is_file():
        bench.notes.append(f"no benchmark clip at {clip}: this worker cannot be ranked")
        return bench

    try:
        info = probe(clip)
    except Exception as exc:  # noqa: BLE001
        bench.notes.append(f"benchmark clip unreadable: {exc}")
        return bench
    frames = duration_to_frames(info.duration_ms, info.fps_num, info.fps_den)
    bench.clip_frames = frames

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="bench_", dir=settings.tmp_dir))
    started = time.monotonic()
    try:
        # Grading takes the render's output, which is what it consumes in the real
        # pipeline: a 1080p H.264 file, not a 4K master.
        graded_source: Path | None = None
        for name, step in (
            ("merge", lambda: _merge(clip, scratch, bench)),
            ("proxy", lambda: _proxy(clip, caps, frames, info, scratch, bench)),
            ("render", lambda: _render(clip, frames, scratch, bench)),
        ):
            try:
                produced = step()
            except Exception as exc:  # noqa: BLE001 (one broken step must not lose the others)
                bench.notes.append(f"{name} not measured: {str(exc).splitlines()[0][:180]}")
                continue
            if name in ("proxy", "render") and isinstance(produced, Path):
                graded_source = produced  # the render wins, being second

        if graded_source is not None:
            try:
                _grade(graded_source, frames, scratch, bench)
            except Exception as exc:  # noqa: BLE001
                bench.notes.append(f"grade not measured: {str(exc).splitlines()[0][:180]}")
        else:
            bench.notes.append("grade not measured: nothing was produced to grade")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        bench.elapsed_s = round(time.monotonic() - started, 2)

    log.info(
        "Benchmark on %s (%d frames) in %.1fs: merge=%s proxy=%s render=%s grade=%s",
        bench.clip, frames, bench.elapsed_s,
        _fmt(bench.merge_mbps, "MB/s"), _fmt(bench.proxy_fps, "img/s"),
        _fmt(bench.render_fps, "img/s"), _fmt(bench.grade_fps, "img/s"),
    )
    for note in bench.notes:
        log.warning("Benchmark: %s", note)
    return bench


def _fmt(value: float | None, unit: str) -> str:
    return f"{value:.1f} {unit}" if value else "?"
