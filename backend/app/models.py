from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Column, Index, JSON, UniqueConstraint
from sqlmodel import Field, SQLModel

from .timeutil import utcnow  # noqa: F401  (re-exported for modules expecting it here)


# --------------------------------------------------------------------------- #
# State enums
# --------------------------------------------------------------------------- #

class ClipState(str, enum.Enum):
    SEEN = "seen"            # spotted in inbox, size not stable yet
    INGESTED = "ingested"    # moved into raw/ and probed
    MERGED = "merged"        # folded into a merged file
    FAILED = "failed"


class SequenceState(str, enum.Enum):
    NEW = "new"              # parts identified, nothing produced yet
    MERGING = "merging"
    MERGED = "merged"        # merged/ ready, gyro continuous
    PROXYING = "proxying"
    READY = "ready"          # proxy ready → can be derushed
    FAILED = "failed"


class JobKind(str, enum.Enum):
    MERGE = "merge"
    PROXY = "proxy"
    RENDER = "render"
    GRADE = "grade"


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATES = {JobState.DONE, JobState.FAILED, JobState.CANCELLED}


class RenderState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class GradeState(str, enum.Enum):
    DRAFT = "draft"          # parameters being adjusted, nothing produced yet
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #

class Sequence(SQLModel, table=True):
    """One continuous recording, possibly split across several files.

    `merged_path` points at the single file produced by mp4_merge (or a hardlink
    to the lone part). It is the only file Gyroflow will ever see: continuous
    gyro, so one smoothing pass, so no seam.
    """

    __tablename__ = "sequence"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)          # derived from the first part name
    label: str = ""
    # Free-form tag the user pins on a rush to find it again in a long session.
    # A palette token, not a CSS colour: the front decides how it looks.
    color: str = ""
    state: SequenceState = Field(default=SequenceState.NEW, index=True)

    # Content identity: hash of the ordered part fingerprints. The same parts in
    # the same order always merge to the same bytes, so an existing merged file can
    # be adopted instead of being produced all over again.
    content_hash: str = Field(default="", index=True)

    part_count: int = 0
    width: int = 0
    height: int = 0
    fps_num: int = 0
    fps_den: int = 1
    duration_ms: float = 0.0
    frame_count: int = 0
    size_bytes: int = 0
    recorded_at: Optional[datetime] = None             # UTC, start of the first part

    merged_path: Optional[str] = None
    proxy_path: Optional[str] = None
    filmstrip_path: Optional[str] = None
    proxy_width: int = 0
    proxy_height: int = 0

    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def artifact_stem(self) -> str:
        """Base name of every file derived from this sequence.

        The hash is part of the name on purpose: it makes the produced files
        self-describing, so they can be found again after the database row is gone,
        with no extra bookkeeping table.
        """
        return f"{self.key}__{self.content_hash}" if self.content_hash else self.key


class Clip(SQLModel, table=True):
    """A raw file, straight off the SD card."""

    __tablename__ = "clip"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_clip_fingerprint"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    sequence_id: Optional[int] = Field(default=None, foreign_key="sequence.id", index=True)
    part_index: int = 0                                # 0-based within the sequence

    filename: str
    raw_path: Optional[str] = None
    size_bytes: int = 0
    # size + hash of the first/last megabytes: hashing 3.7 GB on every scan is absurd
    fingerprint: str = Field(index=True)

    # ffprobe metadata
    duration_ms: float = 0.0
    width: int = 0
    height: int = 0
    fps_num: int = 0
    fps_den: int = 1
    codec: str = ""
    has_gyro: bool = False
    # Timestamp taken from the filename, in UTC (DJI names in UTC)
    recorded_at: Optional[datetime] = None
    # Camera file index (0044) when we can read it
    camera_index: Optional[int] = None

    state: ClipState = Field(default=ClipState.SEEN, index=True)
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Cut(SQLModel, table=True):
    """A zone kept while derushing. Bounds are frames, inclusive."""

    __tablename__ = "cut"

    id: Optional[int] = Field(default=None, primary_key=True)
    sequence_id: int = Field(foreign_key="sequence.id", index=True)
    order_index: int = 0
    label: str = ""
    start_frame: int = 0
    end_frame: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class Render(SQLModel, table=True):
    __tablename__ = "render"

    id: Optional[int] = Field(default=None, primary_key=True)
    sequence_id: int = Field(foreign_key="sequence.id", index=True)
    cut_id: Optional[int] = Field(default=None, foreign_key="cut.id", index=True)
    template: str = ""
    state: RenderState = Field(default=RenderState.QUEUED, index=True)
    progress: float = 0.0

    start_frame: int = 0
    end_frame: int = 0
    out_path: Optional[str] = None
    project_path: Optional[str] = None
    overrides: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    # What Gyroflow actually used for warping ("OpenCL", "CPU"…), read from its
    # logs. A dedicated field rather than parsed out of `log_tail`, which we truncate.
    processing_device: Optional[str] = None
    error: Optional[str] = None
    log_tail: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class Grade(SQLModel, table=True):
    """Colour grading of one stabilized clip, into a separate file.

    One row per render, holding the current parameters. The output is named after
    the hash of those parameters, so going back to a look already produced costs
    nothing, and two looks can sit side by side.
    """

    __tablename__ = "grade"

    id: Optional[int] = Field(default=None, primary_key=True)
    render_id: int = Field(index=True, unique=True)

    params: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # What signalstats measured on the clip: percentiles, clipping, and the
    # timestamps worth previewing. Grading on a single lucky frame is the surest
    # way to get it wrong.
    analysis: dict = Field(default_factory=dict, sa_column=Column(JSON))

    params_hash: str = ""
    out_path: Optional[str] = None
    state: GradeState = Field(default=GradeState.DRAFT, index=True)
    progress: float = 0.0
    error: Optional[str] = None
    log_tail: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class Job(SQLModel, table=True):
    """Work queue. It is also the API ↔ worker channel: two separate processes
    sharing nothing but this table (SQLite in WAL mode)."""

    __tablename__ = "job"

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: JobKind = Field(index=True)
    state: JobState = Field(default=JobState.QUEUED, index=True)
    priority: int = 0                                   # lower runs first
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    sequence_id: Optional[int] = Field(default=None, index=True)
    render_id: Optional[int] = Field(default=None, index=True)
    grade_id: Optional[int] = Field(default=None, index=True)

    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
    attempts: int = 0

    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


Index("ix_job_pick", Job.__table__.c.state, Job.__table__.c.priority, Job.__table__.c.id)
