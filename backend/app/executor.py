"""Worker side of the queue: turn a job spec into a result.

Nothing here opens the database, and nothing here knows a row id. A spec comes in,
files are produced, measured facts go back out. That is the whole contract, and it
is what lets this run on a machine the dispatcher only knows by name.

Paths inside a spec are relative to `data_dir` and are resolved against **this**
machine's data directory, so a worker sharing the dispatcher's volume and a worker
holding its own copy execute the identical spec.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import settings
from .paths import to_absolute, to_relative
from .services import grading as grading_service
from .services import gyro as gyro_service
from .services import gyroflow as gyroflow_service
from .services import merge as merge_service
from .services import proxy as proxy_service
from .services.capabilities import detect
from .services.gyroflow import Template
from .services.procs import ProcessError, ProgressCallback

log = logging.getLogger(__name__)


class SpecError(RuntimeError):
    """The spec does not describe something this worker can run."""


def _resolve(value: str | None, what: str) -> Path:
    path = to_absolute(value)
    if path is None:
        raise SpecError(f"spec carries no {what}")
    return path


def _source(spec: dict[str, Any]) -> Path:
    source = _resolve(spec.get("source"), "source")
    if not source.exists():
        # Phase 1 shares the volume, so this means the file really is gone. Once a
        # worker keeps its own copy, this is where fetching it will belong.
        raise SpecError(f"source missing: {source}")
    return source


# --------------------------------------------------------------------------- #
# One executor per job kind
# --------------------------------------------------------------------------- #

def _run_merge(spec: dict[str, Any], progress: ProgressCallback) -> dict[str, Any]:
    parts = [_resolve(p, "part") for p in spec.get("parts") or []]
    missing = [str(p) for p in parts if not p.exists()]
    if missing:
        raise SpecError(f"part(s) missing: {', '.join(missing)}")

    result = merge_service.merge_parts(parts, _resolve(spec.get("dest"), "destination"), progress)
    return {
        "path": to_relative(result.path),
        "method": result.method,
        "width": result.probe.width,
        "height": result.probe.height,
        "fps_num": result.probe.fps_num,
        "fps_den": result.probe.fps_den,
        "duration_ms": result.probe.duration_ms,
        "size_bytes": result.probe.size_bytes,
    }


def _run_proxy(spec: dict[str, Any], progress: ProgressCallback) -> dict[str, Any]:
    source = _source(spec)
    stem = spec["stem"]
    warnings: list[str] = []

    result = proxy_service.build_proxy(
        source,
        settings.proxies_dir / f"{stem}.mp4",
        detect(),
        frame_count=spec.get("frame_count") or 0,
        fps_num=spec.get("fps_num") or 0,
        fps_den=spec.get("fps_den") or 1,
        progress_cb=progress,
    )

    poster = settings.proxies_dir / f"{stem}.poster.jpg"
    duration_ms = spec.get("duration_ms") or 0.0
    try:
        proxy_service.build_poster(result.path, poster, duration_ms)
    except (ProcessError, RuntimeError) as exc:
        # A missing poster does not prevent derushing.
        warnings.append(f"poster not generated: {exc}")

    # Read from the merged master, not the proxy: the proxy has no gyro track.
    # A few seconds, against a proxy measured in minutes.
    try:
        gyro_service.build_chart(source, gyro_service.chart_path(stem), duration_ms)
    except (gyro_service.GyroError, OSError) as exc:
        warnings.append(f"gyro chart not generated: {exc}")

    for warning in warnings:
        log.warning("%s: %s", stem, warning)

    return {
        "proxy_path": to_relative(result.path),
        "proxy_width": result.width,
        "proxy_height": result.height,
        "warnings": warnings,
    }


def _run_render(spec: dict[str, Any], progress: ProgressCallback) -> dict[str, Any]:
    payload = spec.get("template") or {}
    if not payload.get("data"):
        raise SpecError("spec carries no template")
    # The template travels resolved rather than as an id: the worker has no
    # database to look it up in, and the dispatcher is the one that owns the
    # editable copies under `templates/`.
    template = Template(
        id=payload.get("id") or "template",
        label=payload.get("label") or "",
        data=payload["data"],
    )

    result = gyroflow_service.render(
        source=_source(spec),
        template=template,
        trim_ranges_ms=spec.get("trim_ranges_ms") or [],
        out_dir=settings.out_dir,
        out_filename=spec["out_filename"],
        project_path=settings.projects_dir / spec["project_filename"],
        progress_cb=progress,
    )
    return {
        "out_path": to_relative(result.out_path),
        "project_path": to_relative(result.project_path),
        "processing_device": result.processing_device,
        "log_tail": result.log_tail,
    }


def _run_grade(spec: dict[str, Any], progress: ProgressCallback) -> dict[str, Any]:
    source = _source(spec)
    dest = _resolve(spec.get("dest"), "destination")

    if dest.exists():
        # Already produced, by this worker or by a previous run whose output landed
        # here. The parameter hash is in the name, so the bytes cannot be stale.
        log.info("Grade already produced, reusing %s", dest.name)
        return {"out_path": to_relative(dest), "reused": True}

    grading_service.render(
        source,
        dest,
        spec.get("params") or {},
        frame_count=spec.get("frame_count") or 0,
        progress_cb=progress,
    )
    return {"out_path": to_relative(dest), "reused": False}


_EXECUTORS = {
    "merge": _run_merge,
    "proxy": _run_proxy,
    "render": _run_render,
    "grade": _run_grade,
}


def execute(spec: dict[str, Any], progress: ProgressCallback) -> dict[str, Any]:
    """Run one job spec and return the facts the dispatcher has to record."""
    executor = _EXECUTORS.get(spec.get("kind") or "")
    if executor is None:
        raise SpecError(f"unknown job kind: {spec.get('kind')!r}")
    return executor(spec, progress)
