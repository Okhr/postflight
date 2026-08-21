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

# What Gyroflow itself puts in a fresh project, read off `gyroflow <file>
# --export-project 1` on 1.6.3 rather than guessed. Only the settings that have a
# fixed default are here: `output_width/height` and `bitrate` are derived from the
# source file, so there is no such thing as their default in a template.
GYROFLOW_DEFAULTS: dict[str, Any] = {
    "codec": "H.264/AVC",
    "smoothness": 0.5,
    "horizon_lock": 0.0,
    "lens_correction": 1.0,
    "frame_offset_x": 0.0,
    "frame_offset_y": 0.0,
    "fov": 1.0,
}

# The codecs Gyroflow offers that make sense for a rush. Its list is longer (ProRes,
# DNxHD, PNG sequences); these three are the ones a file meant to be edited or shared
# comes out as.
CODECS = ["H.264/AVC", "H.265/HEVC", "ProRes"]

_PROGRESS = re.compile(r"Rendering progress:\s*(\d+)\s*/\s*(\d+)\s+frames")
_OPENCL_OK = re.compile(r"Initialized OpenCL", re.I)
_WGPU_OK = re.compile(r"Initialized wgpu", re.I)


class GyroflowError(RuntimeError):
    pass


def _aspect(width: int, height: int) -> str:
    """"16:9" from 1920x1080. Derived, never stored: an aspect that contradicts the
    dimensions next to it is a bug waiting to be believed."""
    if width <= 0 or height <= 0:
        return ""
    from math import gcd

    step = gcd(width, height)
    return f"{width // step}:{height // step}"


@dataclass
class Template:
    id: str
    label: str
    description: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    #: True when this id only exists in the bundled directory, so deleting the copy
    #: in `data/templates` brings the original back instead of removing it.
    bundled: bool = False

    def _output(self, key: str, fallback: Any = None) -> Any:
        return (self.data.get("output") or {}).get(key, fallback)

    def _stab(self, key: str, fallback: Any = None) -> Any:
        return (self.data.get("stabilization") or {}).get(key, fallback)

    def _smoothing(self, name: str, fallback: float) -> float:
        """`smoothing_params` is a list of {name, value}, not an object."""
        for entry in self._stab("smoothing_params") or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                return float(entry.get("value", fallback))
        return fallback

    @property
    def output_width(self) -> int:
        return int(self._output("output_width") or 0)

    @property
    def output_height(self) -> int:
        return int(self._output("output_height") or 0)

    def to_dict(self) -> dict:
        offset = self._stab("adaptive_zoom_center_offset") or [0.0, 0.0]
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "aspect": _aspect(self.output_width, self.output_height),
            "width": self.output_width,
            "height": self.output_height,
            "bundled": self.bundled,
            "codec": self._output("codec") or GYROFLOW_DEFAULTS["codec"],
            "bitrate": float(self._output("bitrate") or 0.0),
            "smoothness": self._smoothing("smoothness", GYROFLOW_DEFAULTS["smoothness"]),
            "horizon_lock": float(
                self._stab("horizon_lock_amount", GYROFLOW_DEFAULTS["horizon_lock"])
            ),
            "lens_correction": float(
                self._stab("lens_correction_amount", GYROFLOW_DEFAULTS["lens_correction"])
            ),
            "frame_offset_x": float(offset[0] if len(offset) > 0 else 0.0),
            "frame_offset_y": float(offset[1] if len(offset) > 1 else 0.0),
            "fov": float(self._stab("fov", GYROFLOW_DEFAULTS["fov"])),
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
        data=data,
    )


def _bundled_ids() -> set[str]:
    ids: set[str] = set()
    if BUNDLED_TEMPLATES_DIR.is_dir():
        for path in BUNDLED_TEMPLATES_DIR.glob("*.json"):
            if tpl := _load_template_file(path):
                ids.add(tpl.id)
    return ids


def list_templates() -> list[Template]:
    seed_templates()
    bundled = _bundled_ids()
    templates: dict[str, Template] = {}
    for directory in (BUNDLED_TEMPLATES_DIR, settings.templates_dir):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if tpl := _load_template_file(path):
                tpl.bundled = tpl.id in bundled
                templates[tpl.id] = tpl  # data/ overrides the bundled one
    return sorted(templates.values(), key=lambda t: t.id)


def get_template(template_id: str) -> Template:
    for tpl in list_templates():
        if tpl.id == template_id:
            return tpl
    raise GyroflowError(f"template inconnu : {template_id}")


_ID = re.compile(r"^[a-z0-9_]{1,40}$")


def slug(label: str) -> str:
    """An id from a label. Only ever used to name a file, hence the narrow alphabet."""
    out = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return out[:40] or "template"


def _path_of(template_id: str) -> Path:
    if not _ID.match(template_id):
        raise GyroflowError(f"identifiant de template invalide : {template_id}")
    return settings.templates_dir / f"{template_id}.json"


def _write(template_id: str, label: str, description: str, data: dict[str, Any]) -> Template:
    body = {
        "$meta": {"id": template_id, "label": label, "description": description},
        **data,
    }
    path = _path_of(template_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2) + "\n")
    return get_template(template_id)


def _set_smoothing(data: dict[str, Any], name: str, value: float) -> None:
    params = data.setdefault("stabilization", {}).setdefault("smoothing_params", [])
    for entry in params:
        if isinstance(entry, dict) and entry.get("name") == name:
            entry["value"] = value
            return
    params.append({"name": name, "value": value})


#: The seven settings the interface edits, and where each one lives in a project. The
#: rest of the file is left exactly as it was: a template carries smoothing params and
#: zoom settings nobody edits here, and rewriting the file from the form would drop
#: them.
def apply_settings(data: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(data)
    output = data.setdefault("output", {})
    stab = data.setdefault("stabilization", {})

    if (width := values.get("width")) is not None:
        output["output_width"] = int(width)
    if (height := values.get("height")) is not None:
        output["output_height"] = int(height)
    if (codec := values.get("codec")) is not None:
        output["codec"] = codec
    if (bitrate := values.get("bitrate")) is not None:
        output["bitrate"] = float(bitrate)
    if (smoothness := values.get("smoothness")) is not None:
        _set_smoothing(data, "smoothness", float(smoothness))
    if (horizon := values.get("horizon_lock")) is not None:
        stab["horizon_lock_amount"] = float(horizon)
    if (lens := values.get("lens_correction")) is not None:
        stab["lens_correction_amount"] = float(lens)
    if (fov := values.get("fov")) is not None:
        stab["fov"] = float(fov)
    if values.get("frame_offset_x") is not None or values.get("frame_offset_y") is not None:
        current = stab.get("adaptive_zoom_center_offset") or [0.0, 0.0]
        stab["adaptive_zoom_center_offset"] = [
            float(values.get("frame_offset_x", current[0] if current else 0.0)),
            float(values.get("frame_offset_y", current[1] if len(current) > 1 else 0.0)),
        ]
    return data


def save_template(template_id: str, values: dict[str, Any]) -> Template:
    """Patch one template in place. Creates the copy in `data/templates` if the only
    version so far is the bundled one, which is what makes a shipped template
    editable and resettable at once."""
    current = get_template(template_id)
    label = values.get("label") or current.label
    description = values.get("description")
    if description is None:
        description = current.description
    return _write(template_id, label, description, apply_settings(current.data, values))


def create_template(label: str, copy_of: str | None = None) -> Template:
    """A new template, from an existing one or from Gyroflow's own defaults."""
    base = get_template(copy_of).data if copy_of else {"version": 2}
    taken = {t.id for t in list_templates()}
    template_id = base_id = slug(label)
    suffix = 2
    while template_id in taken:
        template_id = f"{base_id}_{suffix}"
        suffix += 1
    return _write(template_id, label, "", copy.deepcopy(base))


def delete_template(template_id: str) -> str:
    """Remove the editable copy. Answers what actually happened.

    A bundled template cannot be deleted: its file is inside the image, and dropping
    the copy in `data/templates` is a reset, since the next listing seeds it again.
    """
    path = _path_of(template_id)
    existed = path.exists()
    if not existed and template_id not in _bundled_ids():
        raise GyroflowError(f"template inconnu : {template_id}")
    path.unlink(missing_ok=True)
    if template_id in _bundled_ids():
        seed_templates()
        return "reset"
    return "deleted"


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
