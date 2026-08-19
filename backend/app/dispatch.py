"""Dispatcher side of the queue: hand jobs out, take results back.

The database belongs to the dispatcher alone. A worker never opens it: it asks for
a job over HTTP, receives a **self-contained spec**, and posts back the facts it
measured. That is what lets a worker run on another machine, and it is why the
spec carries the resolved template rather than a template id, and file *paths*
rather than database ids.

Paths in a spec stay relative to `data_dir`, the same convention the database
already uses (see `paths.py`). Each side resolves them against its own data
directory, so the very same spec works whether the worker shares the dispatcher's
volume or keeps a copy in its own.

Two rules hold the whole thing together:

- **Attribution is atomic.** A conditional `UPDATE ... WHERE state='queued'` is
  what makes a job go to exactly one worker. A read followed by a write would hand
  the same job to two machines that both start decoding.
- **A job is held on a lease.** The worker renews it while it works, and a lease
  nobody renews goes back into the queue. That is what makes a worker one can
  simply switch off, which is the point of the whole exercise.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, select

from .config import settings
from .framing import cut_to_trim_range_ms, duration_to_frames
from .models import (
    Clip,
    ClipState,
    Cut,
    Grade,
    GradeState,
    Job,
    JobKind,
    JobState,
    Render,
    RenderState,
    Sequence,
    SequenceState,
    Worker,
    utcnow,
)
from .paths import read_volume_id, to_absolute, to_relative
from .pipeline import enqueue_proxy
from .services import grading as grading_service
from .services import gyroflow as gyroflow_service
from .timeutil import as_utc

log = logging.getLogger(__name__)

# How long a claimed job stays claimed without news from its worker. Long enough
# that a busy machine has many chances to be heard, short enough that switching a
# worker off does not idle the queue for minutes.
LEASE_S = 60.0

# The heartbeat renews the lease **and** carries the progress the UI draws, so its
# cadence is set by the progress bar and not by the lease. At 15 s the bar advanced
# in visible jumps (measured: 0.19 then 0.22 then 0.45), which is worse than what
# the shared-database version did at roughly one write per second. Two seconds costs
# one small POST per worker per two seconds, which is less database traffic than
# before, and it gives the lease thirty chances to be renewed before it lapses.
HEARTBEAT_S = 2.0

# How often the dispatcher looks for leases nobody renewed. Deliberately not the
# heartbeat cadence: a job is only late once the lease has lapsed, so checking more
# often than that buys nothing.
REAP_INTERVAL_S = 15.0

# A job that keeps outliving its lease is not unlucky, it is killing its worker.
# Requeueing it forever would grind the whole queue on a single poisoned job.
MAX_ATTEMPTS = 3


# A worker asks for work every second, so silence for a whole lease means it is
# not there any more. Same number as the lease on purpose: one notion of "gone".
ONLINE_S = LEASE_S

# Weight of one real measurement against everything measured before it. 0.3 lets the
# average follow a machine that genuinely changed (a GPU finally mapped in) within a
# handful of jobs, without letting one unlucky job rewrite what is known about it.
OBSERVE_ALPHA = 0.3

# Under this, a job says more about process startup than about the machine. The
# startup benchmark runs on 30 frames and is optimistic by a fixed-ish factor for
# exactly that reason; a 30-frame cut would teach the average the same lie.
OBSERVE_MIN_FRAMES = 300
OBSERVE_MIN_MB = 200.0

# Which rate ranks which kind of job, and in what unit.
RATE_KEYS = {
    JobKind.MERGE: "merge_mbps",
    JobKind.PROXY: "proxy_fps",
    JobKind.RENDER: "render_fps",
    JobKind.GRADE: "grade_fps",
}


def is_online(worker: Worker) -> bool:
    return (utcnow() - as_utc(worker.last_seen_at)).total_seconds() < ONLINE_S


class PrepareError(RuntimeError):
    """The job cannot even be described: nothing to run, so fail it outright."""


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def upsert_worker(
    session: Session,
    name: str,
    capabilities: dict[str, Any],
    concurrency: int,
    rates: dict[str, Any] | None = None,
    volume_id: str = "",
) -> Worker:
    """Register a worker, or update the one that already carries this name.

    Keyed by name and not by a generated id, so a worker that restarts is the same
    worker: its history stays attached to the machine rather than to the process.
    That is also why `observed` is not touched here: it is what real jobs proved
    about this machine, and a container restart must not throw it away.
    """
    worker = session.exec(select(Worker).where(Worker.name == name)).first()
    if worker is None:
        worker = Worker(name=name)
        log.info("Worker %s registered", name)
    worker.capabilities = capabilities
    worker.rates = rates or {}
    # Same volume as the dispatcher means files never have to travel. Measured by
    # comparing marks on the two volumes rather than configured, because the failing
    # direction of a wrong flag is silent: a worker told it shares the volume looks
    # for files that are not there and fails every job it takes.
    worker.shares_data = bool(volume_id) and volume_id == read_volume_id()
    worker.concurrency = max(1, concurrency)
    worker.last_seen_at = utcnow()
    session.add(worker)
    session.commit()
    session.refresh(worker)
    log.info(
        "Worker %s: shares_data=%s rates=%s",
        name, worker.shares_data,
        {k: round(v, 1) for k, v in (rates or {}).items() if isinstance(v, (int, float))},
    )
    return worker


def rate_for(worker: Worker, kind: JobKind) -> float | None:
    """How fast this worker is at this kind of job, in the kind's own unit.

    What real jobs measured beats the startup benchmark, which runs on half a second
    of footage and overstates by a roughly fixed factor (measured: 28 img/s against
    22.7 on a real sequence). Unknown stays unknown, and callers treat it as such: a
    worker that could not be benchmarked must look neither infinitely fast nor
    infinitely slow.
    """
    key = RATE_KEYS[kind]
    observed = (worker.observed or {}).get(key)
    if observed:
        return float(observed)
    value = (worker.rates or {}).get(key)
    return float(value) if value else None


# --------------------------------------------------------------------------- #
# Spec building, one per job kind
# --------------------------------------------------------------------------- #

def _sequence_of(session: Session, job: Job) -> Sequence:
    sequence = session.get(Sequence, job.payload.get("sequence_id") or job.sequence_id)
    if sequence is None:
        raise PrepareError(f"sequence {job.sequence_id} not found")
    return sequence


def _prepare_merge(session: Session, job: Job) -> dict[str, Any]:
    sequence = _sequence_of(session, job)
    clips = session.exec(
        select(Clip).where(Clip.sequence_id == sequence.id).order_by(Clip.part_index)  # type: ignore[arg-type]
    ).all()
    parts = [c.raw_path for c in clips if c.raw_path]
    if not parts:
        raise PrepareError(f"sequence {sequence.key} has no part")

    sequence.state = SequenceState.MERGING
    sequence.error = None
    session.add(sequence)
    session.commit()

    return {
        "parts": parts,
        "dest": to_relative(settings.merged_dir / f"{sequence.artifact_stem}.mp4"),
    }


def _prepare_proxy(session: Session, job: Job) -> dict[str, Any]:
    sequence = _sequence_of(session, job)
    if not sequence.merged_path:
        raise PrepareError(f"merged file missing for {sequence.key}")

    sequence.state = SequenceState.PROXYING
    sequence.error = None
    session.add(sequence)
    session.commit()

    return {
        "source": sequence.merged_path,
        "stem": sequence.artifact_stem,
        "frame_count": sequence.frame_count,
        "fps_num": sequence.fps_num,
        "fps_den": sequence.fps_den,
        "duration_ms": sequence.duration_ms,
    }


def _prepare_render(session: Session, job: Job) -> dict[str, Any]:
    render = session.get(Render, job.payload.get("render_id") or job.render_id)
    if render is None:
        raise PrepareError(f"render {job.render_id} not found")
    sequence = session.get(Sequence, render.sequence_id)
    if sequence is None or not sequence.merged_path:
        raise PrepareError("sequence or merged file not found")

    template = gyroflow_service.get_template(render.template)

    if render.cut_id is not None:
        cut = session.get(Cut, render.cut_id)
        if cut is None:
            raise PrepareError(f"cut {render.cut_id} not found")
        trim = [
            cut_to_trim_range_ms(cut.start_frame, cut.end_frame, sequence.fps_num, sequence.fps_den)
        ]
        suffix = f"c{cut.order_index:02d}"
        render.start_frame, render.end_frame = cut.start_frame, cut.end_frame
    else:
        trim = []  # whole sequence
        suffix = "full"
        render.start_frame, render.end_frame = 0, max(0, sequence.frame_count - 1)

    render.state = RenderState.RUNNING
    render.started_at = utcnow()
    render.error = None
    session.add(render)
    session.commit()

    return {
        "source": sequence.merged_path,
        "template": {"id": template.id, "label": template.label, "data": template.data},
        "trim_ranges_ms": trim,
        "out_filename": f"{sequence.key}__{template.id}__{suffix}.mp4",
        "project_filename": f"{sequence.key}__{template.id}__{suffix}.gyroflow.json",
        "overrides": render.overrides or {},
    }


def _prepare_grade(session: Session, job: Job) -> dict[str, Any]:
    grade = session.get(Grade, job.payload.get("grade_id") or job.grade_id)
    if grade is None:
        raise PrepareError(f"grade {job.grade_id} not found")
    render = session.get(Render, grade.render_id)
    if render is None or not render.out_path:
        raise PrepareError("stabilized clip not found")

    # The graded file is named after the clip *and* the look, so two looks live side
    # by side and a look already produced is never produced again. The hash is
    # computed here because the dispatcher has to store it anyway.
    grade.params_hash = grading_service.params_hash(grade.params)
    grade.state = GradeState.RUNNING
    grade.started_at = utcnow()
    grade.error = None
    session.add(grade)
    session.commit()

    stem = to_absolute(render.out_path).stem  # type: ignore[union-attr]
    return {
        "source": render.out_path,
        "dest": to_relative(settings.graded_dir / f"{stem}__{grade.params_hash}.mp4"),
        "params": grade.params,
        "analysis": grade.analysis or None,
        "frame_count": max(render.end_frame - render.start_frame + 1, 0),
    }


_PREPARERS = {
    JobKind.MERGE: _prepare_merge,
    JobKind.PROXY: _prepare_proxy,
    JobKind.RENDER: _prepare_render,
    JobKind.GRADE: _prepare_grade,
}


def prepare(session: Session, job: Job) -> dict[str, Any]:
    """Describe a claimed job well enough that a machine with no database can run it."""
    preparer = _PREPARERS.get(job.kind)
    if preparer is None:
        raise PrepareError(f"unknown job kind: {job.kind}")
    spec = preparer(session, job)
    spec["kind"] = job.kind.value
    spec["job_id"] = job.id
    return spec


# --------------------------------------------------------------------------- #
# Claiming
# --------------------------------------------------------------------------- #

def _take(session: Session, job_id: int, worker_id: int) -> bool:
    """Move one queued job to running, atomically. False if someone got it first."""
    now = utcnow()
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.state == JobState.QUEUED)  # type: ignore[arg-type]
        .values(
            state=JobState.RUNNING,
            worker_id=worker_id,
            started_at=now,
            lease_expires_at=now + timedelta(seconds=LEASE_S),
            attempts=Job.attempts + 1,
            progress=0.0,
            message="",
            error=None,
        )
    )
    session.commit()
    return result.rowcount == 1


def claim(session: Session, worker: Worker) -> tuple[Job, dict[str, Any]] | None:
    """Give this worker the highest-priority job it can have, spec included.

    A job that cannot be described is failed here rather than handed out, so a
    stale reference never travels to a worker only to come back as an error.
    """
    worker.last_seen_at = utcnow()
    session.add(worker)
    session.commit()

    while True:
        candidate = session.exec(
            select(Job)
            .where(Job.state == JobState.QUEUED)
            .order_by(Job.priority, Job.id)  # type: ignore[arg-type]
            .limit(1)
        ).first()
        if candidate is None or candidate.id is None:
            return None
        if not _take(session, candidate.id, worker.id or 0):
            continue  # another worker was faster; look at what is left

        job = session.get(Job, candidate.id)
        assert job is not None
        try:
            spec = prepare(session, job)
        except Exception as exc:  # noqa: BLE001 (any breakage here fails the job, not the claim)
            log.warning("Job %s#%s cannot be prepared: %s", job.kind.value, job.id, exc)
            session.rollback()
            _fail(session, job, f"cannot be prepared: {exc}")
            continue
        log.info("Job %s#%s → %s", job.kind.value, job.id, worker.name)
        return job, spec


# --------------------------------------------------------------------------- #
# Progress, completion, failure
# --------------------------------------------------------------------------- #

def _held_by(session: Session, job_id: int, worker_id: int) -> Job | None:
    """The job, if this worker is still the one holding it.

    Anything else (requeued after a lease expiry, already finished, taken over)
    means the worker must stop: two machines writing the same output file is the
    one outcome worth going out of the way to prevent.
    """
    job = session.get(Job, job_id)
    if job is None or job.state != JobState.RUNNING or job.worker_id != worker_id:
        return None
    return job


def heartbeat(
    session: Session, job_id: int, worker_id: int, progress: float, message: str
) -> bool:
    """Renew the lease and record progress. False tells the worker to give up."""
    job = _held_by(session, job_id, worker_id)
    if job is None:
        return False
    job.progress = max(0.0, min(1.0, progress))
    if message:
        job.message = message[:300]
    job.lease_expires_at = utcnow() + timedelta(seconds=LEASE_S)
    session.add(job)

    # A render carries its own progress bar, read by the derush page.
    if job.kind == JobKind.RENDER:
        render = session.get(Render, job.payload.get("render_id") or job.render_id)
        if render is not None:
            render.progress = job.progress
            session.add(render)
    session.commit()
    return True


def _apply_merge(session: Session, job: Job, result: dict[str, Any]) -> None:
    sequence = _sequence_of(session, job)
    clips = session.exec(
        select(Clip).where(Clip.sequence_id == sequence.id).order_by(Clip.part_index)  # type: ignore[arg-type]
    ).all()

    # The values measured on the merged file are authoritative, not the estimates
    # summed from the parts.
    sequence.merged_path = result["path"]
    sequence.width = result["width"]
    sequence.height = result["height"]
    sequence.fps_num = result["fps_num"]
    sequence.fps_den = result["fps_den"]
    sequence.duration_ms = result["duration_ms"]
    sequence.frame_count = duration_to_frames(
        result["duration_ms"], result["fps_num"], result["fps_den"]
    )
    sequence.size_bytes = result["size_bytes"]
    sequence.state = SequenceState.MERGED
    sequence.updated_at = utcnow()
    session.add(sequence)
    for clip in clips:
        clip.state = ClipState.MERGED
        session.add(clip)
    session.commit()

    # Deleting the parts is the dispatcher's business, not the worker's: the copy
    # that matters is the one on the dispatcher's volume, and a worker only ever
    # holds a copy of it.
    if settings.purge_parts_after_merge and result.get("method") == "mp4_merge":
        for clip in clips:
            part = to_absolute(clip.raw_path)
            if part is not None:
                part.unlink(missing_ok=True)
        log.info("Raw parts deleted for %s (merge verified)", sequence.key)

    enqueue_proxy(session, sequence)


def _apply_proxy(session: Session, job: Job, result: dict[str, Any]) -> None:
    sequence = _sequence_of(session, job)
    sequence.proxy_path = result["proxy_path"]
    sequence.proxy_width = result["proxy_width"]
    sequence.proxy_height = result["proxy_height"]
    sequence.filmstrip_path = result.get("filmstrip_path")
    sequence.state = SequenceState.READY
    sequence.updated_at = utcnow()
    session.add(sequence)
    session.commit()
    for warning in result.get("warnings") or []:
        log.warning("Proxy of %s: %s", sequence.key, warning)


def _apply_render(session: Session, job: Job, result: dict[str, Any]) -> None:
    render = session.get(Render, job.payload.get("render_id") or job.render_id)
    if render is None:
        raise PrepareError(f"render {job.render_id} vanished mid-job")
    render.out_path = result["out_path"]
    render.project_path = result.get("project_path")
    render.processing_device = result.get("processing_device")
    render.log_tail = (result.get("log_tail") or "")[-4000:]
    render.state = RenderState.DONE
    render.progress = 1.0
    render.finished_at = utcnow()
    session.add(render)
    session.commit()


def _apply_grade(session: Session, job: Job, result: dict[str, Any]) -> None:
    grade = session.get(Grade, job.payload.get("grade_id") or job.grade_id)
    if grade is None:
        raise PrepareError(f"grade {job.grade_id} vanished mid-job")
    grade.out_path = result["out_path"]
    grade.state = GradeState.DONE
    grade.progress = 1.0
    grade.finished_at = utcnow()
    session.add(grade)
    session.commit()


_APPLIERS = {
    JobKind.MERGE: _apply_merge,
    JobKind.PROXY: _apply_proxy,
    JobKind.RENDER: _apply_render,
    JobKind.GRADE: _apply_grade,
}


def _magnitude(session: Session, job: Job, result: dict[str, Any]) -> float | None:
    """How much work the job actually was, in the unit its rate is measured in."""
    if job.kind == JobKind.MERGE:
        return (result.get("size_bytes") or 0) / (1 << 20)
    if job.kind == JobKind.PROXY:
        sequence = session.get(Sequence, job.payload.get("sequence_id") or job.sequence_id)
        return float(sequence.frame_count) if sequence else None

    render_id = job.payload.get("render_id") or job.render_id
    if job.kind == JobKind.GRADE:
        grade = session.get(Grade, job.payload.get("grade_id") or job.grade_id)
        render_id = grade.render_id if grade else None
    render = session.get(Render, render_id) if render_id else None
    if render is None:
        return None
    return float(max(render.end_frame - render.start_frame + 1, 0))


def observe(
    session: Session, job: Job, worker_id: int, result: dict[str, Any], elapsed_s: float
) -> None:
    """Fold what a finished job measured into the worker's moving average.

    This is the half of the ranking that is not a guess. The startup benchmark runs
    on 30 frames and reports 28 img/s where a real sequence does 22.7; every real job
    that completes replaces a little more of that estimate with the truth, at no cost,
    since the worker had to report its elapsed time anyway.

    `elapsed_s` is what the *worker* timed around the work itself, not what the
    dispatcher can compute from the job row: on a worker that does not share the
    volume, the row would also count the minutes spent fetching a 4 GB master, and
    call the machine slow for having a thin cable.
    """
    if elapsed_s <= 0 or not worker_id:
        return
    worker = session.get(Worker, worker_id)
    if worker is None:
        return
    magnitude = _magnitude(session, job, result)
    if magnitude is None:
        return
    if magnitude < (OBSERVE_MIN_MB if job.kind == JobKind.MERGE else OBSERVE_MIN_FRAMES):
        return

    key = RATE_KEYS[job.kind]
    sample = magnitude / elapsed_s
    observed = dict(worker.observed or {})
    previous = observed.get(key)
    observed[key] = (
        sample if not previous
        else (1 - OBSERVE_ALPHA) * float(previous) + OBSERVE_ALPHA * sample
    )
    observed[f"{key}_n"] = int(observed.get(f"{key}_n") or 0) + 1
    worker.observed = observed
    session.add(worker)
    log.info(
        "Worker %s: %s now %.1f (this job %.1f, sample %d)",
        worker.name, key, observed[key], sample, observed[f"{key}_n"],
    )


def complete(
    session: Session,
    job_id: int,
    worker_id: int,
    result: dict[str, Any],
    elapsed_s: float = 0.0,
) -> bool:
    """Record what a worker produced. False if the job is no longer its own."""
    job = _held_by(session, job_id, worker_id)
    if job is None:
        return False
    applier = _APPLIERS.get(job.kind)
    if applier is None:
        _fail(session, job, f"unknown job kind: {job.kind}")
        return True
    try:
        applier(session, job, result)
    except Exception as exc:  # noqa: BLE001 (the work is done, only the recording broke)
        session.rollback()
        log.exception("Job %s#%s: result could not be applied", job.kind.value, job.id)
        _fail(session, job, f"result could not be applied: {exc}")
        return True

    observe(session, job, worker_id, result, elapsed_s)

    job.state = JobState.DONE
    job.progress = 1.0
    job.error = None
    job.worker_id = worker_id
    job.lease_expires_at = None
    job.finished_at = utcnow()
    session.add(job)
    session.commit()
    return True


def _fail(session: Session, job: Job, message: str) -> None:
    """Mark the job failed, and whatever it was producing along with it."""
    log.error("Job %s#%s failed: %s", job.kind.value, job.id, message)

    if job.sequence_id:
        sequence = session.get(Sequence, job.sequence_id)
        if sequence is not None:
            sequence.state = SequenceState.FAILED
            sequence.error = message[:2000]
            sequence.updated_at = utcnow()
            session.add(sequence)
    if job.kind == JobKind.RENDER:
        render = session.get(Render, job.payload.get("render_id") or job.render_id)
        if render is not None:
            render.state = RenderState.FAILED
            render.error = message[:2000]
            render.finished_at = utcnow()
            session.add(render)
    if job.kind == JobKind.GRADE:
        grade = session.get(Grade, job.payload.get("grade_id") or job.grade_id)
        if grade is not None:
            grade.state = GradeState.FAILED
            grade.error = message[:2000]
            grade.finished_at = utcnow()
            session.add(grade)

    job.state = JobState.FAILED
    job.error = message[:2000]
    job.lease_expires_at = None
    job.finished_at = utcnow()
    session.add(job)
    session.commit()


def fail(session: Session, job_id: int, worker_id: int, error: str) -> bool:
    """A worker reporting its own failure. Terminal: only a lease expiry retries."""
    job = _held_by(session, job_id, worker_id)
    if job is None:
        return False
    _fail(session, job, error)
    return True


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #

def _requeue(session: Session, job: Job, why: str) -> None:
    job.state = JobState.QUEUED
    job.worker_id = None
    job.lease_expires_at = None
    job.started_at = None
    job.progress = 0.0
    job.message = why
    session.add(job)
    # A render left in `running` would show a progress bar nobody is moving.
    if job.kind == JobKind.RENDER:
        render = session.get(Render, job.payload.get("render_id") or job.render_id)
        if render is not None:
            render.state = RenderState.QUEUED
            render.progress = 0.0
            session.add(render)
    if job.kind == JobKind.GRADE:
        grade = session.get(Grade, job.payload.get("grade_id") or job.grade_id)
        if grade is not None:
            grade.state = GradeState.QUEUED
            grade.progress = 0.0
            session.add(grade)


def reap_expired(session: Session) -> int:
    """Requeue the jobs whose worker stopped saying it was alive.

    This is the only automatic retry in the system, and it is what makes a worker
    something you can just switch off mid-render.
    """
    now = utcnow()
    running = session.exec(select(Job).where(Job.state == JobState.RUNNING)).all()
    reaped = 0
    for job in running:
        deadline = as_utc(job.lease_expires_at) if job.lease_expires_at else None
        # A null lease is a job from before leases existed, or one written by a
        # crashed claim: either way nobody is holding it.
        if deadline is not None and deadline > now:
            continue
        if job.attempts >= MAX_ATTEMPTS:
            _fail(
                session,
                job,
                f"abandoned {job.attempts} times without finishing "
                f"(worker lost, or the job kills the worker that takes it)",
            )
        else:
            log.warning(
                "Job %s#%s: lease expired, back in the queue (attempt %d)",
                job.kind.value, job.id, job.attempts,
            )
            _requeue(session, job, "requeued: the worker stopped answering")
        reaped += 1
    if reaped:
        session.commit()
    return reaped


def release(session: Session, worker_id: int) -> int:
    """Give back everything a worker holds, because it is shutting down cleanly.

    Turns a 60 second wait for the lease to lapse into an immediate requeue, and
    does not spend an attempt: being switched off is not the job's fault.
    """
    held = session.exec(
        select(Job).where(Job.state == JobState.RUNNING, Job.worker_id == worker_id)
    ).all()
    for job in held:
        job.attempts = max(0, job.attempts - 1)
        _requeue(session, job, "requeued: the worker was shut down")
        log.info("Job %s#%s released", job.kind.value, job.id)
    if held:
        session.commit()
    return len(held)


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #

def drop_stale_jobs(session: Session) -> tuple[int, int]:
    """Clean the queue of jobs that cannot produce anything useful.

    Two cases. A sequence that was deleted or rebuilt leaves behind jobs pointing
    at nothing; keeping them would only fail them one by one, flagging errors that
    are not errors. And several jobs of the same kind on the same sequence would
    redo the same work, the later ones on top of the earlier one's output.
    """
    known = {seq.id for seq in session.exec(select(Sequence)).all()}
    queued = session.exec(
        select(Job).where(Job.state == JobState.QUEUED).order_by(Job.id)  # type: ignore[arg-type]
    ).all()

    orphans, duplicates = 0, 0
    seen: set[tuple[int, JobKind]] = set()
    for job in queued:
        if job.sequence_id is None:
            continue
        if job.sequence_id not in known:
            session.delete(job)
            orphans += 1
            continue
        key = (job.sequence_id, job.kind)
        if key in seen:
            session.delete(job)
            duplicates += 1
            continue
        seen.add(key)

    if orphans:
        log.warning("%d queued job(s) with no sequence: dropped", orphans)
    if duplicates:
        log.warning("%d duplicate queued job(s): dropped", duplicates)
    if orphans or duplicates:
        session.commit()
    return orphans, duplicates

