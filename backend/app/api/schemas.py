from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..framing import format_timecode


class BaseSchema(BaseModel):
    """Put the UTC timezone back on dates read from SQLite.

    Without it the date goes out as ISO with no suffix and the browser reads it as
    local time — a two-hour offset shown on rushes in summer.
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


class RenderOut(BaseSchema):
    id: int
    sequence_id: int
    sequence_key: str = ""
    cut_id: int | None
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
    color: str = ""
    state: str
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


class RegroupRequest(BaseSchema):
    # Two possible inputs: the clips directly, or the sequences whose parts should
    # be joined (what the UI does, since it only ever handles sequences).
    clip_ids: list[int] = Field(default_factory=list)
    sequence_ids: list[int] = Field(default_factory=list)
    label: str | None = None
    force: bool = False


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
    out_name: str | None = None
    size_bytes: int | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


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
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TemplateOut(BaseSchema):
    id: str
    label: str
    description: str
    aspect: str
    width: int
    height: int


class ScanOut(BaseSchema):
    ingested: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
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


class StatusOut(BaseSchema):
    capabilities: dict[str, Any]
    counts: dict[str, int]
    inbox_pending: int
    settings: dict[str, Any]
