"""Gyro telemetry extraction, for the chart under the derush timeline.

Gyroflow's CLI exports parsed telemetry (`--export-metadata 2:file.json`), which
is the only sane way in: the DJI `djmd` stream is proprietary, and Gyroflow's own
telemetry-parser is precisely what knows how to read it. Measured on a 4-minute
rush: 3.5 s and **61 MB** of JSON — which is why none of that ever reaches the
browser. We boil it down to a few dozen kilobytes here.

DJI does not ship raw IMU samples: `raw_imu` comes back empty and the payload is
477 083 orientation quaternions at ~1980 Hz. The gyroscope signal is recovered by
differentiating them — the relative rotation between two consecutive samples,
divided by dt, is the angular velocity a gyroscope would have measured. Cameras
that do expose raw IMU (GoPro `gpmd`) are read directly instead.

Downsampling keeps the **min and max per bucket**, not one sample out of N: plain
decimation would drop the very spikes the chart exists to show. What is drawn is
an envelope, the way an audio waveform is drawn.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)

# Points along the time axis. More than the ~900 px the chart is wide: it used to
# serve a zoomable timeline, which is gone, so this is now just headroom — 6000
# buckets over 4 min is 40 ms each, and the payload runs 340 kB. Dropping to ~1500
# would quarter that with nothing visible lost at this width; kept for now so a
# future zoom needs no rebuild of every chart on disk.
TARGET_POINTS = 6000

EXPORT_TIMEOUT_S = 300.0

# Payload shape. Bumped when the JSON changes in a way the front cannot read, so
# charts already sitting in `proxies/` are rebuilt instead of served as garbage.
CHART_FORMAT = 3

# Full scale of the IMUs in these cameras. Measured on a real rush: the flight
# itself peaks around 1100 deg/s (a flip at 51 s), while the last six samples of
# the file reach 58 000 deg/s — end-of-stream garbage, 160 rotations per second.
# Anything past full scale is not a measurement, so it is dropped rather than
# clamped: clamped to 2000 it would still set the scale of the whole chart and
# flatten the real flight. How many were dropped is reported, so nothing is
# swallowed silently.
MAX_RATE_DPS = 2000.0


# Which body axis is which, per camera. Measured on a DJI O4P rush, three ways
# that agree (2026-08-14):
#
# - **Z is yaw**: rotating about it barely moves the gravity direction expressed in
#   the body frame (0.144 of the rotation tilts gravity, against 0.999 for X and
#   0.994 for Y). Only the vertical axis behaves that way.
# - **X is pitch**: at a 534 deg/s peak the horizon slides down the frame without
#   rotating — that is a flip, not a roll.
# - **Y is roll** by elimination, confirmed on a sustained stretch where the horizon
#   visibly pivots. It is also the most frequently isolated axis (878 samples against
#   213 for X), which is what an FPV flight looks like.
#
# Only claimed where it was measured. Another camera exposes its IMU in its own
# frame, so anything unlisted keeps the neutral X/Y/Z.
AXIS_NAMES: dict[str, dict[str, str]] = {
    "DJI O4P": {"x": "Pitch", "y": "Roll", "z": "Yaw"},
    "DJI O4": {"x": "Pitch", "y": "Roll", "z": "Yaw"},
}


class GyroError(RuntimeError):
    pass


def is_current(chart: Path) -> bool:
    """Whether a chart on disk was written by this version of the payload shape."""
    try:
        with chart.open() as handle:
            return json.load(handle).get("format") == CHART_FORMAT
    except (OSError, ValueError):
        return False


def chart_path(artifact_stem: str) -> Path:
    """Where the chart of a sequence lives. One definition, three callers: the
    worker that builds it, the route that serves it, the delete that removes it."""
    return settings.proxies_dir / f"{artifact_stem}.gyro.json"


@dataclass
class Sample:
    t_ms: float
    x: float
    y: float
    z: float


def _export_telemetry(source: Path, out_json: Path) -> dict:
    """Ask Gyroflow for the parsed telemetry of `source`."""
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.unlink(missing_ok=True)
    cmd = [
        settings.gyroflow_bin,
        str(source),
        "--export-metadata",
        f"2:{out_json}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=EXPORT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise GyroError(f"telemetry export timed out after {EXPORT_TIMEOUT_S:.0f}s") from exc
    if not out_json.exists():
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise GyroError(f"telemetry export produced nothing: {tail}")
    try:
        with out_json.open() as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise GyroError(f"unreadable telemetry export: {exc}") from exc


def _plausible(x: float, y: float, z: float) -> bool:
    return max(abs(x), abs(y), abs(z)) <= MAX_RATE_DPS


def _from_raw_imu(raw_imu: list[dict]) -> tuple[list[Sample], int]:
    """Cameras that ship real IMU samples (GoPro and friends).

    `gyro` is in rad/s in the telemetry, converted here to deg/s so both paths
    produce the same unit.
    """
    samples: list[Sample] = []
    dropped = 0
    for entry in raw_imu:
        gyro = entry.get("gyro")
        timestamp = entry.get("timestamp_ms")
        if not gyro or timestamp is None or len(gyro) < 3:
            continue
        x = math.degrees(float(gyro[0]))
        y = math.degrees(float(gyro[1]))
        z = math.degrees(float(gyro[2]))
        if not _plausible(x, y, z):
            dropped += 1
            continue
        samples.append(Sample(t_ms=float(timestamp), x=x, y=y, z=z))
    return samples, dropped


def _from_quaternions(quaternions: dict[str, list[float]]) -> tuple[list[Sample], int]:
    """Differentiate orientation quaternions into angular velocity, in deg/s.

    Component order follows nalgebra's serialization, `[x, y, z, w]`. Getting that
    wrong would only swap which axis is called X, Y or Z — the magnitudes are
    identical either way, since the relative rotation between two samples does not
    depend on the labelling.

    `dq.w < 0` is negated to always take the shortest of the two rotations
    representing the same orientation change; without it every other sample would
    flip sign and the curve would be pure noise.
    """
    keys = sorted(int(k) for k in quaternions)
    samples: list[Sample] = []
    dropped = 0
    for index in range(len(keys) - 1):
        t_us, next_us = keys[index], keys[index + 1]
        dt_s = (next_us - t_us) / 1e6
        if dt_s <= 0:
            continue
        ax, ay, az, aw = quaternions[str(t_us)]
        bx, by, bz, bw = quaternions[str(next_us)]

        # dq = conj(a) * b
        dw = aw * bw + ax * bx + ay * by + az * bz
        dx = aw * bx - ax * bw - ay * bz + az * by
        dy = aw * by + ax * bz - ay * bw - az * bx
        dz = aw * bz - ax * by + ay * bx - az * bw

        scale = (-2.0 if dw < 0 else 2.0) * (180.0 / math.pi) / dt_s
        x, y, z = dx * scale, dy * scale, dz * scale
        if not _plausible(x, y, z):
            dropped += 1
            continue
        samples.append(Sample(t_ms=t_us / 1000.0, x=x, y=y, z=z))
    return samples, dropped


def _bucketize(
    samples: list[tuple[float, tuple[float, ...]]],
    width: int,
    duration_ms: float,
    points: int,
) -> list[list[float] | None]:
    """Group samples into `points` buckets, keeping the min and max of each component.

    A slot holds `[min0, max0, min1, max1, ...]`, or None where no sample landed.
    """
    span = duration_ms if duration_ms > 0 else (samples[-1][0] if samples else 1.0)
    buckets: list[list[float] | None] = [None] * points

    for t_ms, values in samples:
        index = int(t_ms / span * points)
        if index < 0 or index >= points:
            continue  # IMU runs slightly wider than the video, both ends trimmed
        slot = buckets[index]
        if slot is None:
            buckets[index] = [bound for value in values for bound in (value, value)]
        else:
            for i in range(width):
                if values[i] < slot[2 * i]:
                    slot[2 * i] = values[i]
                elif values[i] > slot[2 * i + 1]:
                    slot[2 * i + 1] = values[i]
    return buckets


def _envelope_view(
    axes: tuple[str, ...],
    samples: list[tuple[float, tuple[float, ...]]],
    duration_ms: float,
    points: int,
    decimals: int,
) -> dict:
    """Min/max pair per bucket — for a signal whose spikes are the whole point.

    Decimating instead would erase exactly what one is looking for: measured, a real
    spike reads min -1584 / max +1874 deg/s where the bucket mean shows 178.
    """
    buckets = _bucketize(samples, len(axes), duration_ms, points)
    series: dict[str, dict[str, list[float]]] = {
        name: {"min": [], "max": []} for name in axes
    }
    # A hole means the IMU skipped a beat there; carrying the previous value keeps
    # the line continuous instead of dropping it to zero.
    last = [0.0] * (2 * len(axes))
    for slot in buckets:
        values = slot if slot is not None else last
        last = values
        for i, name in enumerate(axes):
            series[name]["min"].append(round(values[2 * i], decimals))
            series[name]["max"].append(round(values[2 * i + 1], decimals))

    peak = max(
        (abs(v) for axis in series.values() for bound in axis.values() for v in bound),
        default=0.0,
    )
    return {"kind": "envelope", "series": series, "peak": round(peak, decimals)}


def _line_view(
    axes: tuple[str, ...],
    samples: list[tuple[float, tuple[float, ...]]],
    duration_ms: float,
    points: int,
    decimals: int,
) -> dict:
    """One line per axis, the bucket's midpoint — for a signal that is smooth.

    Orientation components move slowly next to a 2 kHz sampling rate, so a min/max
    envelope would draw two lines on top of each other and double the payload for
    nothing. This is also what Gyroflow does with its quaternion view.
    """
    buckets = _bucketize(samples, len(axes), duration_ms, points)
    series: dict[str, list[float]] = {name: [] for name in axes}
    last = [0.0] * (2 * len(axes))
    for slot in buckets:
        values = slot if slot is not None else last
        last = values
        for i, name in enumerate(axes):
            series[name].append(round((values[2 * i] + values[2 * i + 1]) / 2, decimals))

    peak = max((abs(v) for line in series.values() for v in line), default=0.0)
    return {"kind": "line", "series": series, "peak": round(peak, decimals)}


def build_chart(
    source: Path,
    dest: Path,
    duration_ms: float,
    points: int = TARGET_POINTS,
) -> Path:
    """Produce the compact chart data for one merged sequence.

    Two views come out of the same telemetry, because they answer different
    questions and Gyroflow shows them under separate view modes:

    - **rate**: angular velocity, deg/s. Spikes mark the shaky passages, which is
      what one derushes on. For a DJI file it is derived, not measured.
    - **quaternion**: the raw orientation components x/y/z/w, exactly what Gyroflow
      plots in its view mode 3 — and the only thing it *can* plot on these files,
      since its gyro view reads `raw_imu`, which DJI leaves empty.
    """
    scratch = settings.tmp_dir / f"{dest.stem}.telemetry.json"
    try:
        payload = _export_telemetry(source, scratch)
    finally:
        scratch.unlink(missing_ok=True)

    raw_imu = payload.get("raw_imu") or []
    quaternions = payload.get("quaternions") or {}
    if raw_imu:
        (samples, dropped), origin = _from_raw_imu(raw_imu), "raw IMU"
    elif quaternions:
        (samples, dropped), origin = _from_quaternions(quaternions), "quaternions"
    else:
        raise GyroError("telemetry holds neither raw IMU nor quaternions")
    if not samples:
        raise GyroError(f"no usable sample in the {origin}")

    source = payload.get("detected_source") or "unknown"
    views = {
        "rate": {
            "label": "Gyroscope",
            "unit": "deg/s",
            "axes": ["x", "y", "z"],
            # Named only for cameras whose axes were actually identified; the
            # quaternion view stays x/y/z/w, those are components, not angles.
            "axis_labels": AXIS_NAMES.get(source, {}),
            **_envelope_view(
                ("x", "y", "z"),
                [(s.t_ms, (s.x, s.y, s.z)) for s in samples],
                duration_ms,
                points,
                1,
            ),
        },
    }
    if quaternions:
        # No plausibility filter here: a garbage sample is still a unit quaternion,
        # so there is nothing to reject. `dropped` concerns the rate view only.
        components = [
            (int(key) / 1000.0, tuple(quaternions[key]))
            for key in sorted(quaternions, key=int)
        ]
        views["quaternion"] = {
            "label": "Quaternions",
            "unit": "",
            "axes": ["x", "y", "z", "w"],
            **_line_view(("x", "y", "z", "w"), components, duration_ms, points, 4),
        }

    spacing_ms = (samples[-1].t_ms - samples[0].t_ms) / max(len(samples) - 1, 1)
    chart = {
        "format": CHART_FORMAT,
        "source": source,
        "origin": origin,
        "sample_count": len(samples),
        "sample_rate_hz": round(1000.0 / spacing_ms, 1) if spacing_ms > 0 else 0.0,
        "duration_ms": round(duration_ms, 3),
        # The IMU rarely stops exactly with the video; showing the gap is more
        # honest than silently stretching one onto the other.
        "imu_duration_ms": round(samples[-1].t_ms - samples[0].t_ms, 3),
        "points": points,
        "dropped": dropped,
        "default_view": "quaternion" if "quaternion" in views else "rate",
        "views": views,
    }

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w") as handle:
        json.dump(chart, handle, separators=(",", ":"))
    log.info(
        "Gyro chart %s: %d samples from %s at %.0f Hz, views %s, peak %.0f deg/s, "
        "%d dropped (%.0f kB)",
        dest.name, chart["sample_count"], origin, chart["sample_rate_hz"],
        ",".join(views), views["rate"]["peak"], dropped, dest.stat().st_size / 1024,
    )
    return dest
