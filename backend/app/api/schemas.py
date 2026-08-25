from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..framing import format_timecode


class BaseSchema(BaseModel):
    """Put the UTC timezone back on dates read from SQLite.

    Without it the date goes out as ISO with no suffix and the browser reads it as
    local time, a two-hour offset shown on rushes in summer.
    """

    @model_validator(mode="after")
    def _stamp_utc(self):  # noqa: ANN201
        for name, value in list(self.__dict__.items()):
            if isinstance(value, datetime) and value.tzinfo is None:
                object.__setattr__(self, name, value.replace(tzinfo=timezone.utc))
        return self


class ClipOut(BaseSchema):
    id: int
    filename: str
    part_index: int
    size_bytes: int
    duration_ms: float
    width: int
    height: int
    codec: str
    has_gyro: bool
    recorded_at: datetime | None
    camera_index: int | None
    state: str


class CutOut(BaseSchema):
    id: int
    order_index: int
    label: str
    start_frame: int
    end_frame: int
    frames: int
    duration_ms: float
    start_tc: str
    end_tc: str
    # Whether a stabilized file exists for this cut, and a graded one on top of it.
    # Two icons in the rush tree, so the state of a session is readable without
    # opening anything.
    rendered: bool = False
    graded: bool = False
    # How many files exist because of this cut, stabilized and graded together.
    # Deleting a cut deletes them, and the dialog that asks says how many.
    files: int = 0


class QueueRender(BaseSchema):
    """One file made from a sequence. Named by its profile, addressed by its id, so
    the profile shown on the row is also the way to its grading and its file."""

    id: int
    template: str
    # The graded file made from it, when there is one. A stabilized clip and its
    # graded version are two files, and the row hands over either.
    grade_id: int | None = None


class QueueCut(BaseSchema):
    """A sequence as the stabilize queue shows it."""

    id: int
    label: str
    frames: int
    duration_ms: float
    start_tc: str
    end_tc: str
    # Profiles a finished file exists for, and profiles a job is still working on.
    # Together they decide whether the row arrives ticked: what is done with the
    # chosen profile has nothing left to ask for.
    done: list[QueueRender] = Field(default_factory=list)
    busy: list[QueueRender] = Field(default_factory=list)


class QueueRush(BaseSchema):
    """A rush and the sequences marked on it. Only rushes with at least one."""

    id: int
    label: str
    folder_id: int | None = None
    recorded_at: datetime | None = None
    cuts: list[QueueCut] = Field(default_factory=list)


class RenderOut(BaseSchema):
    id: int
    sequence_id: int
    sequence_key: str = ""
    # What the interface calls things: the rush's name and the sequence's, since the
    # key is a filename and the tree next to it says "Rush 1". One vocabulary.
    sequence_label: str = ""
    cut_label: str = ""
    cut_id: int | None
    folder_id: int | None = None
    duration_ms: float = 0.0
    template: str
    state: str
    progress: float
    start_frame: int
    end_frame: int
    out_name: str | None = None
    size_bytes: int | None = None
    error: str | None = None
    processing_device: str | None = None
    created_at: datetime
    # Exposed so the UI can extrapolate an ETA from progress and elapsed time.
    started_at: datetime | None = None
    finished_at: datetime | None


class SequenceOut(BaseSchema):
    id: int
    key: str
    label: str
    folder_id: int | None = None
    state: str
    derushed: bool = False
    part_count: int
    width: int
    height: int
    fps: float
    fps_num: int
    fps_den: int
    duration_ms: float
    frame_count: int
    size_bytes: int
    recorded_at: datetime | None
    has_proxy: bool
    has_filmstrip: bool
    proxy_width: int
    proxy_height: int
    cut_count: int
    render_count: int
    has_gyro: bool = False
    # The masters that produced the merged file, in merge order.
    part_names: list[str] = Field(default_factory=list)
    merged_name: str | None = None
    error: str | None
    duration_tc: str = ""

    @model_validator(mode="after")
    def _fill_tc(self) -> "SequenceOut":
        if self.fps_num and self.frame_count:
            self.duration_tc = format_timecode(self.frame_count, self.fps_num, self.fps_den)
        return self


class SequenceDetail(SequenceOut):
    clips: list[ClipOut] = Field(default_factory=list)
    cuts: list[CutOut] = Field(default_factory=list)
    renders: list[RenderOut] = Field(default_factory=list)


class CutIn(BaseSchema):
    # The id of the cut this stands for, absent when it is a new one. Sending it
    # back is what keeps a cut's identity across an edit, and with it the renders
    # that point at it.
    id: int | None = None
    label: str = ""
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)


class CutsReplaceIn(BaseSchema):
    cuts: list[CutIn] = Field(default_factory=list)


class RenderRequest(BaseSchema):
    template: str
    # None or empty = every cut of the sequence; "full" = the whole sequence.
    cut_ids: list[int] | None = None
    whole_sequence: bool = False
    overrides: dict[str, Any] = Field(default_factory=dict)


class FolderOut(BaseSchema):
    id: int
    name: str
    color: str = ""
    parent_id: int | None = None
    position: int = 0
    # Rushes filed directly here, not counting those in a child folder: a parent
    # showing the total would double-count what the child already shows.
    sequence_count: int = 0


class FolderIn(BaseSchema):
    name: str = Field(min_length=1, max_length=120)
    parent_id: int | None = None
    # Absent means draw one: a folder made in a hurry still comes out told apart from
    # its neighbours.
    color: str | None = None


class FolderPatch(BaseSchema):
    """Absent means unchanged, which is why every field is optional.

    `parent_id` has to tell "move it to the root" apart from "leave it where it is",
    and both look like null here, so the route reads `model_fields_set` rather than
    the value.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = None
    parent_id: int | None = None
    # Where to land among the siblings it ends up with, counted from 0. Out of range is
    # clamped rather than refused: a drop past the last row means last.
    position: int | None = Field(default=None, ge=0)


class GradeOut(BaseSchema):
    id: int
    render_id: int
    sequence_id: int = 0
    sequence_key: str = ""
    render_name: str | None = None
    state: str
    progress: float
    params: dict[str, Any] = Field(default_factory=dict)
    analysis: dict[str, Any] = Field(default_factory=dict)
    # Where the two points would go if measured off this clip, or null for nowhere.
    # The judgement lives on the server (which side already clips, whether there is
    # enough unused range to bother); the button writes the answer into the sliders,
    # so it ends up visible and editable instead of applied invisibly at render time.
    suggested: dict[str, float] | None = None
    out_name: str | None = None
    size_bytes: int | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class LookOut(BaseSchema):
    id: int
    label: str
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class LookIn(BaseSchema):
    label: str
    params: dict[str, Any] = Field(default_factory=dict)


class LookPatch(BaseSchema):
    label: str | None = None
    params: dict[str, Any] | None = None


class GradeParamsIn(BaseSchema):
    params: dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseSchema):
    id: int
    kind: str
    state: str
    progress: float
    message: str
    error: str | None
    sequence_id: int | None
    sequence_key: str | None = None
    # The names the interface uses. A merge or a proxy is about a whole rush, so it
    # carries only the rush; a render or a grade is about one sequence of it.
    sequence_label: str | None = None
    cut_label: str | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TemplateOut(BaseSchema):
    id: str
    label: str
    description: str
    #: Derived from the dimensions, never stored.
    aspect: str
    width: int
    height: int
    #: Ships inside the image, so it can be edited and reset but never deleted.
    bundled: bool = False
    codec: str = ""
    bitrate: float = 0.0
    smoothness: float = 0.5
    horizon_lock: float = 0.0
    lens_correction: float = 1.0
    frame_offset_x: float = 0.0
    frame_offset_y: float = 0.0
    fov: float = 1.0


class TemplateDefaults(BaseSchema):
    """What Gyroflow puts in a fresh project, so the interface can offer to go back
    to it. `width`, `height` and `bitrate` are absent on purpose: Gyroflow derives
    them from the source file, so they have no default in a template."""

    codecs: list[str] = Field(default_factory=list)
    codec: str = ""
    smoothness: float = 0.5
    horizon_lock: float = 0.0
    lens_correction: float = 1.0
    frame_offset_x: float = 0.0
    frame_offset_y: float = 0.0
    fov: float = 1.0


class TemplateIn(BaseSchema):
    label: str = Field(min_length=1, max_length=60)
    #: Start from this template's settings instead of Gyroflow's defaults.
    copy_of: str | None = None


class TemplatePatch(BaseSchema):
    """Every field optional: what is absent stays as it is in the file.

    The bounds are the hard ones, the sliders' own ranges being narrower. Dimensions
    have to be even, which is not fussiness: 4:2:0 chroma is subsampled by two, and
    x264 refuses an odd height outright.
    """

    label: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=300)
    width: int | None = Field(default=None, ge=16, le=7680, multiple_of=2)
    height: int | None = Field(default=None, ge=16, le=7680, multiple_of=2)
    codec: str | None = None
    bitrate: float | None = Field(default=None, gt=0, le=500)
    smoothness: float | None = Field(default=None, ge=0, le=1)
    horizon_lock: float | None = Field(default=None, ge=0, le=100)
    lens_correction: float | None = Field(default=None, ge=0, le=1)
    frame_offset_x: float | None = Field(default=None, ge=-1, le=1)
    frame_offset_y: float | None = Field(default=None, ge=-1, le=1)
    fov: float | None = Field(default=None, ge=0.1, le=3)


class ScanOut(BaseSchema):
    ingested: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    sequences: list[str] = Field(default_factory=list)


class UploadOut(BaseSchema):
    filename: str
    size_bytes: int


class UploadCheckOut(BaseSchema):
    """Verdict of the pre-flight: is this file already known?"""

    fingerprint: str
    known: bool
    filename: str | None = None
    sequence_id: int | None = None


class WorkerOut(BaseSchema):
    """A registered machine, as the UI sees it.

    `capabilities` is what the worker measured on itself, not what the API can see:
    the hardware that matters is the hardware that runs the jobs.
    """

    id: int
    name: str
    capabilities: dict[str, Any]
    # What the startup benchmark measured, and what real jobs have measured since.
    # Two fields because they age differently: the first is rewritten at every
    # registration, the second only ever accumulates.
    rates: dict[str, Any]
    observed: dict[str, Any]
    shares_data: bool
    concurrency: int
    last_seen_at: datetime
    online: bool
    running: int


class StatusOut(BaseSchema):
    # No `capabilities` block: the API does not decode, warp or merge anything, so its
    # own hardware says nothing useful. What matters is each worker's, measured on the
    # machine that will do the work and reported by it.
    workers: list[WorkerOut]
    counts: dict[str, int]
    inbox_pending: int
    settings: dict[str, Any]
