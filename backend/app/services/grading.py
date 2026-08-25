"""Colour grading of a stabilized clip, into a separate file.

Gyroflow does no colour work at all. Its project holds `fov_scale`,
`lens_correction_amount`, `background_mode` and friends, and nothing else. So
grading cannot ride along in the stabilization pass and needs a second encode.

Measured on a real 10 s 1080p60 clip, which is what drives the choices here:

    HEVC 10-bit, preset medium      0.17x realtime
    HEVC 10-bit, preset superfast   0.26x
    H.264 8-bit, preset veryfast    0.71x     <- what we use
    one filtered frame to JPEG      0.32 s    <- what makes live preview possible

H.264 because the graded file is the one meant to be shared, and because the
stabilized render stays untouched next to it: nothing is lost by not archiving
this one in 10-bit.

That 0.32 s is why the preview is a real ffmpeg still frame rather than a shader
reimplementation: what is on screen goes through exactly the filters of the final
encode, so there is no parity to maintain between two colour pipelines.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from .procs import ProcessError, run_with_progress

log = logging.getLogger(__name__)

# 10-bit legal range. The analysis converts to 10 bits before measuring, so this
# holds whatever the clip's own depth is.
LEGAL_BLACK = 64.0
LEGAL_WHITE = 940.0
LEGAL_SPAN = LEGAL_WHITE - LEGAL_BLACK
FULL_SCALE = 1023.0
# The space `lutyuv` works in, which is not full scale: measured, its luma `minval`
# and `maxval` are 16 and 235 in 8 bits, so `val/maxval` puts legal white at 1.0 and
# legal black at 64/940. The expression used to divide by maxval and then compare
# against fractions of full scale (64/1023, 940/1023), mixing two normalisations:
# legal white came out at 215 instead of 235, losing twenty levels off the top of
# every stretched clip. The ratio is depth-proof, 16/235 and 64/940 being equal.
BLACK_N = LEGAL_BLACK / LEGAL_WHITE

DEFAULTS: dict[str, Any] = {
    "exposure": 0.0,        # EV, -2 .. 2
    "contrast": 1.0,        # 0.3 .. 1.7
    "saturation": 1.0,      # 0 .. 2
    "temperature": 6500,    # K, 3000 .. 10000 (6500 = untouched)
    "shadows": 0.0,         # -1 .. 1, lifts or crushes the low end
    "highlights": 0.0,      # -1 .. 1, recovers or pushes the high end
    # Where black and white sit, as a fraction of the legal range: 0 and 1 leave the
    # clip alone. Unlike everything above, these two belong to one clip and do not
    # travel: what is unused range on this shot is picture on the next.
    "black_point": 0.0,     # 0 .. 0.9
    "white_point": 1.0,     # 0.1 .. 1
}


class GradeError(RuntimeError):
    pass


@dataclass
class Analysis:
    frames: int
    y_low: float
    y_high: float
    y_avg: float
    sat_avg: float
    clipped_black: float
    clipped_white: float
    looks_log: bool
    darkest_ms: float
    median_ms: float
    brightest_ms: float

    def to_dict(self) -> dict:
        return {
            "frames": self.frames,
            "y_low": round(self.y_low, 1),
            "y_high": round(self.y_high, 1),
            "y_avg": round(self.y_avg, 1),
            "sat_avg": round(self.sat_avg, 1),
            "clipped_black": round(self.clipped_black, 4),
            "clipped_white": round(self.clipped_white, 4),
            "looks_log": self.looks_log,
            "darkest_ms": round(self.darkest_ms, 1),
            "median_ms": round(self.median_ms, 1),
            "brightest_ms": round(self.brightest_ms, 1),
            "headroom_low": round((self.y_low - LEGAL_BLACK) / LEGAL_SPAN, 4),
            "headroom_high": round((LEGAL_WHITE - self.y_high) / LEGAL_SPAN, 4),
        }


def merge_params(params: dict | None) -> dict:
    """Fill in whatever the caller left out, and drop what we do not know."""
    merged = dict(DEFAULTS)
    for key, value in (params or {}).items():
        if key in DEFAULTS:
            merged[key] = value
    return merged


def params_hash(params: dict) -> str:
    """Identity of a look, so a file already produced is never produced twice."""
    payload = json.dumps(merge_params(params), sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(payload.encode(), digest_size=5).hexdigest()


def is_neutral(params: dict) -> bool:
    return merge_params(params) == DEFAULTS


# The two that belong to one clip and never travel with a look.
PER_CLIP = ("black_point", "white_point")


def travelling(params: dict | None) -> dict:
    """The look alone: everything but this clip's own black and white points."""
    merged = merge_params(params)
    return {key: value for key, value in merged.items() if key not in PER_CLIP}


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

_STAT = re.compile(r"lavfi\.signalstats\.(\w+)=([-\d.]+)")


def analyse(source: Path) -> Analysis:
    """Measure the clip with signalstats: decode only, no encoding."""
    cmd = [
        settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostats",
        "-i", str(source),
        # `format` before `signalstats`, and it is not cosmetic: signalstats reports in
        # the source's own bit depth, while every constant here is 10-bit legal range.
        # The renders were 10-bit until the day they were not, and the same clip then
        # measured y_high 183 on a 64-940 scale with every frame "clipping black".
        # Converting first makes the numbers mean the same thing at any depth.
        "-vf", (f"fps={settings.grade_analysis_fps},scale=480:-2,format=yuv420p10le,"
                "signalstats,metadata=print:file=-"),
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise GradeError(f"analysis failed: {proc.stderr.strip()[:300]}")

    frames: list[dict[str, float]] = []
    current: dict[str, float] = {}
    for line in proc.stdout.splitlines():
        if line.startswith("frame:"):
            if current:
                frames.append(current)
            current = {}
            if (m := re.search(r"pts_time:([\d.]+)", line)):
                current["t_ms"] = float(m.group(1)) * 1000.0
        elif (m := _STAT.match(line.strip())):
            current[m.group(1)] = float(m.group(2))
    if current:
        frames.append(current)
    if not frames:
        raise GradeError("analysis produced no frame")

    def mean(key: str) -> float:
        values = [f[key] for f in frames if key in f]
        return sum(values) / len(values) if values else 0.0

    by_luma = sorted(frames, key=lambda f: f.get("YAVG", 0.0))
    y_low, y_high, y_avg = mean("YLOW"), mean("YHIGH"), mean("YAVG")

    # A log profile keeps its blacks well above legal black and its whites well
    # below legal white, whatever the scene. Measured on this footage: y_low
    # wanders between 152 and 273 and y_max touches 973, so it is *not* log:
    # the check is here for the day the camera is set to D-Log M.
    looks_log = (
        (y_low - LEGAL_BLACK) / LEGAL_SPAN > 0.12
        and (LEGAL_WHITE - y_high) / LEGAL_SPAN > 0.12
        and mean("SATAVG") < 60
    )

    return Analysis(
        frames=len(frames),
        y_low=y_low,
        y_high=y_high,
        y_avg=y_avg,
        sat_avg=mean("SATAVG"),
        # Share of frames already touching the limits, which is what decides
        # whether auto-levels may push that side at all.
        clipped_black=sum(1 for f in frames if f.get("YMIN", 999) <= LEGAL_BLACK + 1) / len(frames),
        clipped_white=sum(1 for f in frames if f.get("YMAX", 0) >= LEGAL_WHITE - 1) / len(frames),
        looks_log=looks_log,
        darkest_ms=by_luma[0].get("t_ms", 0.0),
        median_ms=by_luma[len(by_luma) // 2].get("t_ms", 0.0),
        brightest_ms=by_luma[-1].get("t_ms", 0.0),
    )


# --------------------------------------------------------------------------- #
# Filter chain
# --------------------------------------------------------------------------- #

def _curve(shadows: float, highlights: float) -> str | None:
    """Shadow lift and highlight recovery as a four-point master curve.

    Kept to two control points on purpose: enough to open the shadows or pull the
    highlights back, not enough to build the kind of S-curve that wrecks skin
    tones and skies by accident.
    """
    if abs(shadows) < 1e-3 and abs(highlights) < 1e-3:
        return None
    low = max(0.02, min(0.98, 0.25 + shadows * 0.15))
    high = max(0.02, min(0.98, 0.75 + highlights * 0.15))
    return f"curves=m='0/0 0.25/{low:.3f} 0.75/{high:.3f} 1/1'"


def levels(values: dict) -> tuple[float, float] | None:
    """The luma stretch the two points ask for, as (low point, gain), or None.

    Arithmetic, no decision: the points are what the sliders say. The browser has to
    apply exactly this while a slider moves, so the formula lives on both sides; what
    must not live twice is a judgement, and there is none here.
    """
    black = min(max(float(values.get("black_point", 0.0)), 0.0), 1.0)
    white = min(max(float(values.get("white_point", 1.0)), 0.0), 1.0)
    if white - black < 0.05:  # a stretch that steep is a bug, not an intention
        return None
    if black <= 0.0 and white >= 1.0:
        return None
    lo = BLACK_N + black * (1.0 - BLACK_N)
    hi = BLACK_N + white * (1.0 - BLACK_N)
    return lo, (1.0 - BLACK_N) / max(hi - lo, 1e-3)


def suggest_levels(analysis: dict | None) -> dict[str, float] | None:
    """Where the two points would go if measured off the clip, or None for nowhere.

    This is the judgement, and it stays here: which side is already clipped, and
    whether there is enough unused range to be worth reclaiming. The button writes
    what this returns into the sliders, so the reasoning happens once and the result
    is then visible and editable rather than applied invisibly at render time.
    """
    if not analysis:
        return None
    low = max(0.0, (analysis.get("y_low", LEGAL_BLACK) - LEGAL_BLACK) / LEGAL_SPAN)
    high = min(1.0, (analysis.get("y_high", LEGAL_WHITE) - LEGAL_BLACK) / LEGAL_SPAN)
    # Only reclaim range that is actually unused. Seen on a real clip: pushing the
    # white point on footage whose sky already touches the ceiling blows the sky out
    # completely. A side that clips is a side left alone.
    if analysis.get("clipped_black", 0.0) > 0.05 or low < 0.02:
        low = 0.0
    if analysis.get("clipped_white", 0.0) > 0.05 or high > 0.98:
        high = 1.0
    if high - low <= 0.15 or (low <= 0.0 and high >= 1.0):
        return None
    return {"black_point": round(low, 4), "white_point": round(high, 4)}


def build_filters(params: dict) -> list[str]:
    """The ffmpeg chain, in the order it has to be applied."""
    values = merge_params(params)
    chain: list[str] = []

    # Levels first: stretching a clip that sits in the middle of the range gives
    # every later step something to work with. Luma only, so contrast is recovered
    # without inventing a white balance: a per-channel stretch on a frame that is
    # half sky and half dry grass would turn it blue.
    #
    # `lutyuv` rather than `colorlevels`, which was the obvious candidate and is a
    # trap: colorlevels accepts YUV as well as RGB, and on a YUV frame its red,
    # green and blue points land on Y, U and V. Shifting the black point of chroma,
    # where neutral is the middle of the range and not zero, turns the picture
    # black. Worse, it only did so *sometimes*: with another RGB filter in the
    # chain, ffmpeg inserted a conversion and the same parameters behaved.
    if (stretch := levels(values)):
        lo, gain = stretch
        # `minval/maxval` rather than a literal: ffmpeg fills in the right pair at
        # whatever depth the clip is, and it is exactly where legal black belongs.
        chain.append(
            "lutyuv=y='clip(((val/maxval)-{lo:.5f})*{gain:.5f}+minval/maxval,"
            "minval/maxval,1)*maxval'".format(lo=lo, gain=gain)
        )

    if abs(values["exposure"]) > 1e-3:
        # `exposure` works in stops around 0, which is what the slider shows.
        chain.append(f"exposure=exposure={values['exposure']:.3f}")

    if (curve := _curve(float(values["shadows"]), float(values["highlights"]))):
        chain.append(curve)

    if abs(values["contrast"] - 1.0) > 1e-3 or abs(values["saturation"] - 1.0) > 1e-3:
        chain.append(
            f"eq=contrast={float(values['contrast']):.3f}:saturation={float(values['saturation']):.3f}"
        )

    if abs(float(values["temperature"]) - 6500) > 1:
        chain.append(f"colortemperature=temperature={int(values['temperature'])}")

    return chain


def filter_string(params: dict, extra: list[str] | None = None) -> str:
    chain = build_filters(params) + (extra or [])
    return ",".join(chain) if chain else "null"


# --------------------------------------------------------------------------- #
# Preview and render
# --------------------------------------------------------------------------- #

def preview_frame(
    source: Path,
    dest: Path,
    at_ms: float,
    params: dict,
    width: int | None = None,
) -> Path:
    """One graded frame as JPEG. Measured at 0.32 s, hence usable live."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    scale = f"scale={width or settings.grade_preview_width}:-2"
    cmd = [
        settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
        "-ss", f"{max(at_ms, 0) / 1000:.3f}",
        "-i", str(source),
        "-frames:v", "1",
        "-vf", filter_string(params, extra=[scale]),
        "-q:v", "4",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not dest.exists():
        raise GradeError(f"preview failed: {proc.stderr.strip()[:300]}")
    return dest


def render(
    source: Path,
    dest: Path,
    params: dict,
    frame_count: int = 0,
    progress_cb: Callable[[float, str], None] | None = None,
) -> Path:
    """Encode the graded clip. H.264, see the module docstring for why."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(".partial.mp4")
    partial.unlink(missing_ok=True)

    cmd = [
        settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
        "-i", str(source),
        "-vf", filter_string(params),
        "-c:v", "libx264",
        "-preset", settings.grade_x264_preset,
        "-crf", str(settings.grade_crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-an",
        "-progress", "pipe:1",
        str(partial),
    ]

    frame_re = re.compile(r"^frame=\s*(\d+)")

    def on_line(line: str) -> float | None:
        if frame_count and (m := frame_re.match(line.strip())):
            return min(int(m.group(1)) / frame_count, 0.999)
        return None

    try:
        log_tail = run_with_progress(cmd, on_line=on_line, progress_cb=progress_cb)
    except ProcessError as exc:
        partial.unlink(missing_ok=True)
        raise GradeError(f"grading failed: {exc}") from exc

    if not partial.exists() or partial.stat().st_size == 0:
        raise GradeError("grading produced no file")
    partial.replace(dest)
    log.info(
        "Graded %s (%.1f MB) with %s",
        dest.name, dest.stat().st_size / (1 << 20), filter_string(params),
    )
    return dest
