"""Worker: inbox scanning plus job queue execution.

A separate process from the API. The two share nothing but the SQLite database
(in WAL mode): the API posts jobs, the worker consumes them and writes its
progress there, the API reads it back for its SSE stream. No broker to maintain.

Concurrency is deliberately 1 on heavy jobs: ffmpeg, mp4_merge and Gyroflow
already saturate every core, so running two at once only makes them slower.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path

from sqlmodel import Session, select

from .config import settings
from .db import init_db, session_scope
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
    utcnow,
)
from .paths import to_absolute, to_relative
from .pipeline import enqueue_proxy, ingest_and_group
from .services import gyroflow as gyroflow_service
from .services import grading as grading_service
from .services import gyro as gyro_service
from .services import merge as merge_service
from .services import proxy as proxy_service
from .services.capabilities import detect
from .services import procs
from .services.procs import ProcessError

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
PROGRESS_MIN_DELTA = 0.005
PROGRESS_MIN_INTERVAL_S = 1.0


class ProgressWriter:
    """Write progress into the `job` table without hammering SQLite."""

    def __init__(self, session: Session, job: Job, render: Render | None = None) -> None:
        self._session = session
        self._job = job
        self._render = render
        self._last_value = -1.0
        self._last_write = 0.0

    def __call__(self, value: float, message: str = "") -> None:
        now = time.monotonic()
        if (
            value < 1.0
            and value - self._last_value < PROGRESS_MIN_DELTA
            and now - self._last_write < PROGRESS_MIN_INTERVAL_S
        ):
            return
        self._last_value = value
        self._last_write = now
        self._job.progress = value
        if message:
            self._job.message = message[:300]
        self._session.add(self._job)
        if self._render is not None:
            self._render.progress = value
            self._session.add(self._render)
        self._session.commit()


def _claim_job(session: Session) -> Job | None:
    job = session.exec(
        select(Job)
        .where(Job.state == JobState.QUEUED)
        .order_by(Job.priority, Job.id)  # type: ignore[arg-type]
        .limit(1)
    ).first()
    if job is None:
        return None
    job.state = JobState.RUNNING
    job.started_at = utcnow()
    job.attempts += 1
    job.progress = 0.0
    job.error = None
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _finish(session: Session, job: Job, state: JobState, error: str | None = None) -> None:
    job.state = state
    job.finished_at = utcnow()
    job.progress = 1.0 if state == JobState.DONE else job.progress
    if error:
        job.error = error[:2000]
    session.add(job)
    session.commit()


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def _handle_merge(session: Session, job: Job) -> None:
    sequence = session.get(Sequence, job.payload.get("sequence_id"))
    if sequence is None:
        raise RuntimeError(f"sequence {job.payload.get('sequence_id')} not found")

    clips = session.exec(
        select(Clip).where(Clip.sequence_id == sequence.id).order_by(Clip.part_index)  # type: ignore[arg-type]
    ).all()
    if not clips:
        raise RuntimeError(f"sequence {sequence.key} has no part")

    sequence.state = SequenceState.MERGING
    sequence.error = None
    session.add(sequence)
    session.commit()

    parts = [to_absolute(c.raw_path) for c in clips if c.raw_path]
    parts = [p for p in parts if p is not None]
    dest = settings.merged_dir / f"{sequence.artifact_stem}.mp4"
    result = merge_service.merge_parts(parts, dest, ProgressWriter(session, job))

    # The values measured on the merged file are authoritative, not the estimates.
    sequence.merged_path = to_relative(result.path)
    sequence.width = result.probe.width
    sequence.height = result.probe.height
    sequence.fps_num = result.probe.fps_num
    sequence.fps_den = result.probe.fps_den
    sequence.duration_ms = result.probe.duration_ms
    sequence.frame_count = duration_to_frames(
        result.probe.duration_ms, result.probe.fps_num, result.probe.fps_den
    )
    sequence.size_bytes = result.probe.size_bytes
    sequence.state = SequenceState.MERGED
    sequence.updated_at = utcnow()
    session.add(sequence)

    for clip in clips:
        clip.state = ClipState.MERGED
        session.add(clip)
    session.commit()

    if settings.purge_parts_after_merge and result.method == "mp4_merge":
        for part in parts:
            part.unlink(missing_ok=True)
        log.info("Raw parts deleted for %s (merge verified)", sequence.key)

    enqueue_proxy(session, sequence)


def _handle_proxy(session: Session, job: Job) -> None:
    sequence = session.get(Sequence, job.payload.get("sequence_id"))
    if sequence is None:
        raise RuntimeError(f"sequence {job.payload.get('sequence_id')} not found")
    source = to_absolute(sequence.merged_path)
    if source is None or not source.exists():
        raise RuntimeError(f"merged file missing for {sequence.key}")

    sequence.state = SequenceState.PROXYING
    sequence.error = None
    session.add(sequence)
    session.commit()

    caps = detect()
    proxy_path = settings.proxies_dir / f"{sequence.artifact_stem}.mp4"

    result = proxy_service.build_proxy(
        source,
        proxy_path,
        caps,
        frame_count=sequence.frame_count,
        fps_num=sequence.fps_num,
        fps_den=sequence.fps_den,
        progress_cb=ProgressWriter(session, job),
    )

    filmstrip = settings.proxies_dir / f"{sequence.artifact_stem}.filmstrip.jpg"
    poster = settings.proxies_dir / f"{sequence.artifact_stem}.poster.jpg"
    try:
        proxy_service.build_filmstrip(result.path, filmstrip, sequence.duration_ms)
        proxy_service.build_poster(result.path, poster, sequence.duration_ms)
    except (ProcessError, RuntimeError) as exc:
        # A missing filmstrip does not prevent derushing.
        log.warning("Filmstrip/poster not generated for %s: %s", sequence.key, exc)

    # Read from the merged master, not the proxy: the proxy has no gyro track.
    # A few seconds, against a proxy measured in minutes.
    try:
        gyro_service.build_chart(
            source,
            gyro_service.chart_path(sequence.artifact_stem),
            sequence.duration_ms,
        )
    except (gyro_service.GyroError, OSError) as exc:
        log.warning("Gyro chart not generated for %s: %s", sequence.key, exc)

    sequence.proxy_path = to_relative(result.path)
    sequence.proxy_width = result.width
    sequence.proxy_height = result.height
    sequence.filmstrip_path = to_relative(filmstrip) if filmstrip.exists() else None
    sequence.state = SequenceState.READY
    sequence.updated_at = utcnow()
    session.add(sequence)
    session.commit()


def _handle_render(session: Session, job: Job) -> None:
    render = session.get(Render, job.payload.get("render_id"))
    if render is None:
        raise RuntimeError(f"render {job.payload.get('render_id')} not found")
    sequence = session.get(Sequence, render.sequence_id)
    merged = to_absolute(sequence.merged_path) if sequence else None
    if sequence is None or merged is None or not merged.exists():
        raise RuntimeError("sequence or merged file not found")

    template = gyroflow_service.get_template(render.template)

    if render.cut_id is not None:
        cut = session.get(Cut, render.cut_id)
        if cut is None:
            raise RuntimeError(f"cut {render.cut_id} not found")
        trim = [cut_to_trim_range_ms(cut.start_frame, cut.end_frame, sequence.fps_num, sequence.fps_den)]
        suffix = f"c{cut.order_index:02d}"
        render.start_frame, render.end_frame = cut.start_frame, cut.end_frame
    else:
        trim = []  # whole sequence
        suffix = "full"
        render.start_frame, render.end_frame = 0, max(0, sequence.frame_count - 1)

    out_filename = f"{sequence.key}__{template.id}__{suffix}.mp4"
    project_path = settings.projects_dir / f"{sequence.key}__{template.id}__{suffix}.gyroflow.json"

    render.state = RenderState.RUNNING
    render.started_at = utcnow()
    render.error = None
    session.add(render)
    session.commit()

    result = gyroflow_service.render(
        source=merged,
        template=template,
        trim_ranges_ms=trim,
        out_dir=settings.out_dir,
        out_filename=out_filename,
        project_path=project_path,
        overrides=render.overrides or {},
        progress_cb=ProgressWriter(session, job, render),
    )

    render.out_path = to_relative(result.out_path)
    render.project_path = to_relative(result.project_path)
    render.state = RenderState.DONE
    render.progress = 1.0
    render.finished_at = utcnow()
    render.processing_device = result.processing_device
    render.log_tail = result.log_tail[-4000:]
    session.add(render)
    session.commit()


def _handle_grade(session: Session, job: Job) -> None:
    grade = session.get(Grade, job.payload.get("grade_id"))
    if grade is None:
        raise RuntimeError(f"grade {job.payload.get('grade_id')} not found")
    render = session.get(Render, grade.render_id)
    source = to_absolute(render.out_path) if render else None
    if render is None or source is None or not source.exists():
        raise RuntimeError("stabilized clip not found")

    grade.state = GradeState.RUNNING
    grade.started_at = utcnow()
    grade.error = None
    session.add(grade)
    session.commit()

    # The graded file is named after the clip *and* the look, so two looks live
    # side by side and a look already produced is never produced again.
    grade.params_hash = grading_service.params_hash(grade.params)
    dest = settings.graded_dir / f"{source.stem}__{grade.params_hash}.mp4"
    frames = max(render.end_frame - render.start_frame + 1, 0)

    if dest.exists():
        log.info("Grade %s already produced, reusing %s", grade.id, dest.name)
    else:
        grading_service.render(
            source,
            dest,
            grade.params,
            analysis=grade.analysis,
            frame_count=frames,
            progress_cb=ProgressWriter(session, job),
        )

    grade.out_path = to_relative(dest)
    grade.state = GradeState.DONE
    grade.progress = 1.0
    grade.finished_at = utcnow()
    session.add(grade)
    session.commit()


_HANDLERS = {
    JobKind.MERGE: _handle_merge,
    JobKind.PROXY: _handle_proxy,
    JobKind.RENDER: _handle_render,
    JobKind.GRADE: _handle_grade,
}


def _mark_failure(session: Session, job: Job, exc: Exception) -> None:
    message = str(exc)
    if isinstance(exc, ProcessError) and exc.log_tail:
        message = f"{message}\n{exc.log_tail}"
    log.error("Job %s#%s failed: %s", job.kind.value, job.id, message)

    if job.sequence_id:
        sequence = session.get(Sequence, job.sequence_id)
        if sequence is not None:
            sequence.state = SequenceState.FAILED
            sequence.error = message[:2000]
            sequence.updated_at = utcnow()
            session.add(sequence)
    if job.kind == JobKind.RENDER:
        render = session.get(Render, job.payload.get("render_id"))
        if render is not None:
            render.state = RenderState.FAILED
            render.error = message[:2000]
            render.finished_at = utcnow()
            session.add(render)
    if job.kind == JobKind.GRADE:
        grade = session.get(Grade, job.payload.get("grade_id"))
        if grade is not None:
            grade.state = GradeState.FAILED
            grade.error = message[:2000]
            grade.finished_at = utcnow()
            session.add(grade)
    session.commit()
    _finish(session, job, JobState.FAILED, message)


def process_next_job() -> bool:
    """Process one job. Returns False when the queue is empty."""
    with session_scope() as session:
        job = _claim_job(session)
        if job is None:
            return False
        handler = _HANDLERS.get(job.kind)
        if handler is None:
            _finish(session, job, JobState.FAILED, f"unknown job kind: {job.kind}")
            return True
        try:
            handler(session, job)
        except Exception as exc:  # noqa: BLE001 — a crashed job must not kill the worker
            session.rollback()
            _mark_failure(session, job, exc)
        else:
            _finish(session, job, JobState.DONE)
        return True


def _scanner_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            with session_scope() as session:
                ingest_and_group(session)
        except Exception:
            log.exception("inbox scan failed")
        stop.wait(settings.scan_interval_s)


def _requeue_orphans() -> None:
    """After an abrupt restart, jobs left `running` go back into the queue."""
    with session_scope() as session:
        stuck = session.exec(select(Job).where(Job.state == JobState.RUNNING)).all()
        for job in stuck:
            job.state = JobState.QUEUED
            job.progress = 0.0
            job.message = "requeued after a worker restart"
            session.add(job)
        if stuck:
            log.warning("%d job(s) requeued after restart", len(stuck))


def _drop_stale_jobs() -> None:
    """Clean the queue of jobs that cannot produce anything useful.

    Two cases. A sequence that was deleted or rebuilt leaves behind jobs pointing
    at nothing; keeping them would only fail them one by one, flagging errors that
    are not errors. And several jobs of the same kind on the same sequence would
    redo the same work, the later ones on top of the earlier one's output.
    """
    with session_scope() as session:
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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings.ensure_dirs()
    init_db()
    gyroflow_service.seed_templates()
    detect()
    _requeue_orphans()
    _drop_stale_jobs()

    stop = threading.Event()

    def _shutdown(signum, _frame):  # noqa: ANN001
        log.info("signal %s received, stopping the worker", signum)
        stop.set()
        # Hand the signal down rather than letting the container's grace period run
        # out and SIGKILL everything: an ffmpeg killed mid-VAAPI-decode leaves this
        # machine's amdgpu deadlocked on an orphaned fence. See procs.terminate_all.
        hit = procs.terminate_all()
        if hit:
            log.info("shutdown: %d child process(es) asked to stop", hit)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    scanner = threading.Thread(target=_scanner_loop, args=(stop,), name="scanner", daemon=True)
    scanner.start()
    log.info("Worker started (scanning every %.0fs)", settings.scan_interval_s)

    while not stop.is_set():
        try:
            worked = process_next_job()
        except Exception:
            log.exception("job loop raised")
            worked = False
        if not worked:
            stop.wait(POLL_INTERVAL_S)

    log.info("Worker stopped")


if __name__ == "__main__":
    main()
