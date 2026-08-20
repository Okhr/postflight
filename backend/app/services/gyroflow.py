"""Driving Gyroflow through its CLI.

The discovery that simplifies everything: **`--preset` accepts a partial project
JSON and it carries `trim_ranges_ms`**. Verified: a preset holding
`"trim_ranges_ms": [[100000, 110000]]` renders exactly 599 frames (10 s at 59.94).
So there is no need to generate a full `.gyroflow` then patch it: a template plus
the cut bounds, in a single command.

Also measured: setting `output_width`/`output_height` is enough to change format.
Asking for 1080x1920 from a 3840x2880 source makes Gyroflow derive a 1620x2880
crop on its own, with no need to touch the lens profile's `output_dimension`.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import settings
from .procs import ProgressCallback, run_with_progress

log = logging.getLogger(__name__)

BUNDLED_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_PROGRESS = re.compile(r"Rendering progress:\s*(\d+)\s*/\s*(\d+)\s+frames")
_OPENCL_OK = re.compile(r"Initialized OpenCL", re.I)
_WGPU_OK = re.compile(r"Initialized wgpu", re.I)


class GyroflowError(RuntimeError):
    pass


@dataclass
class Template:
    id: str
    label: str
    description: str = ""
    aspect: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def output_width(self) -> int:
        return int(self.data.get("output", {}).get("output_width") or 0)

    @property
    def output_height(self) -> int:
        return int(self.data.get("output", {}).get("output_height") or 0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "aspect": self.aspect,
            "width": self.output_width,
            "height": self.output_height,
        }


@dataclass
class RenderResult:
    out_path: Path
    project_path: Path
    processing_device: str
    log_tail: str


def seed_templates() -> None:
    """Copy the bundled templates into `data/templates` so they can be edited
    without rebuilding the image."""
    settings.templates_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(BUNDLED_TEMPLATES_DIR.glob("*.json")):
        dest = settings.templates_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
            log.info("Template installed: %s", dest.name)


def _load_template_file(path: Path) -> Template | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Template illisible %s : %s", path.name, exc)
        return None
    meta = data.pop("$meta", {}) or {}
    tid = meta.get("id") or path.stem
    return Template(
        id=tid,
        label=meta.get("label") or tid,
        description=meta.get("description", ""),
        aspect=meta.get("aspect", ""),
        data=data,
    )


def list_templates() -> list[Template]:
    seed_templates()
    templates: dict[str, Template] = {}
    for directory in (BUNDLED_TEMPLATES_DIR, settings.templates_dir):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if tpl := _load_template_file(path):
                templates[tpl.id] = tpl  # data/ overrides the bundled one
    return sorted(templates.values(), key=lambda t: t.id)


def get_template(template_id: str) -> Template:
    for tpl in list_templates():
        if tpl.id == template_id:
            return tpl
    raise GyroflowError(f"template inconnu : {template_id}")


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def build_preset(
    template: Template,
    trim_ranges_ms: list[list[float]],
    out_dir: Path,
    out_filename: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preset = _deep_merge(template.data, overrides or {})
    preset["version"] = preset.get("version", 2)
    preset["trim_ranges_ms"] = trim_ranges_ms
    output = dict(preset.get("output") or {})
    output["output_folder"] = f"file://{out_dir}/"
    output["output_filename"] = out_filename
    if not settings.gyroflow_use_gpu_encode:
        # Gyroflow's GPU encoding goes through AMF/NVENC; missing on many setups
        # (AMD iGPUs among them), where it then produces corrupted frames.
        output["use_gpu"] = False
    preset["output"] = output
    return preset


def _locate_output(expected: Path, out_dir: Path, source: Path) -> Path:
    """Recover the output file when Gyroflow ignored `output_folder`.

    mtime cannot be trusted: Gyroflow copies the source file's timestamps onto
    its output, which therefore looks *older* than the render. So we search by
    name.
    """
    if expected.exists():
        return expected

    for candidate in (source.parent / expected.name, source.with_name(f"{source.stem}_stabilized.mp4")):
        if candidate.exists() and candidate != source:
            expected.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate), str(expected))
            log.info("Output recovered from %s", candidate)
            return expected

    # Last resort: an mp4 next to the source whose name derives from it.
    siblings = [
        p for p in source.parent.glob(f"{source.stem}*.mp4")
        if p != source and p.name != expected.name
    ]
    if len(siblings) == 1:
        expected.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(siblings[0]), str(expected))
        log.info("Output recovered from %s", siblings[0])
        return expected

    raise GyroflowError(
        f"no output file found (expected {expected})"
        + (f" ; candidats ambigus : {[p.name for p in siblings]}" if siblings else "")
    )


def render(
    source: Path,
    template: Template,
    trim_ranges_ms: list[list[float]],
    out_dir: Path,
    out_filename: str,
    project_path: Path,
    overrides: dict[str, Any] | None = None,
    progress_cb: ProgressCallback | None = None,
) -> RenderResult:
    if not source.exists():
        raise GyroflowError(f"source absente : {source}")
    if not shutil.which(settings.gyroflow_bin):
        raise GyroflowError("gyroflow est introuvable dans le PATH")

    out_dir.mkdir(parents=True, exist_ok=True)
    project_path.parent.mkdir(parents=True, exist_ok=True)

    preset = build_preset(template, trim_ranges_ms, out_dir, out_filename, overrides)
    project_path.write_text(json.dumps(preset, indent=2))

    device = "CPU"

    def on_line(line: str) -> float | None:
        nonlocal device
        if _OPENCL_OK.search(line):
            device = "OpenCL"
        elif _WGPU_OK.search(line):
            device = "wgpu"
        if m := _PROGRESS.search(line):
            done, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                return min(done / total, 1.0)
        return None

    cmd = [
        settings.gyroflow_bin, str(source),
        "--preset", str(project_path),
        "-f", "--stdout-progress",
    ]
    log_tail = run_with_progress(
        cmd, on_line, progress_cb, timeout=settings.gyroflow_timeout_s
    )

    out_path = _locate_output(out_dir / out_filename, out_dir, source)
    log.info(
        "Rendu %s → %s (processing: %s)", template.id, out_path.name, device
    )
    return RenderResult(
        out_path=out_path,
        project_path=project_path,
        processing_device=device,
        log_tail=log_tail,
    )
