from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from sqlmodel import Session, func, select

from .. import dispatch
from ..config import settings
from ..db import get_session
from ..framing import format_timecode, frame_to_ms
from ..models import (
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
from ..paths import exists as path_exists, to_absolute
from ..pipeline import (
    PRIORITY_MANUAL,
    adopt_existing_artifacts,
    enqueue_merge,
    enqueue_pending,
    enqueue_proxy,
    ingest_and_group,
    mark_upload_complete,
    unique_destination,
)
from ..services.grouping import sequence_hash
from ..services import grading as grading_service
from ..services import gyro as gyro_service
from ..services import gyroflow as gyroflow_service
from ..services.capabilities import detect
from ..services.probe import fingerprint_lengths, fingerprint_parts
from ..timeutil import as_utc
from . import media, schemas

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #

def _count(session: Session, model, **filters) -> int:
    statement = select(func.count()).select_from(model)
    for key, value in filters.items():
        statement = statement.where(getattr(model, key) == value)
    return session.exec(statement).one()


def _cut_out(cut: Cut, seq: Sequence) -> schemas.CutOut:
    frames = cut.end_frame - cut.start_frame + 1
    return schemas.CutOut(
        id=cut.id or 0,
        order_index=cut.order_index,
        label=cut.label,
        start_frame=cut.start_frame,
        end_frame=cut.end_frame,
        frames=frames,
        duration_ms=frame_to_ms(frames, seq.fps_num, seq.fps_den) if seq.fps_num else 0.0,
        start_tc=format_timecode(cut.start_frame, seq.fps_num, seq.fps_den) if seq.fps_num else "",
        end_tc=format_timecode(cut.end_frame, seq.fps_num, seq.fps_den) if seq.fps_num else "",
    )


def _render_out(render: Render, seq: Sequence | None = None) -> schemas.RenderOut:
    out_path = to_absolute(render.out_path)
    return schemas.RenderOut(
        id=render.id or 0,
        sequence_id=render.sequence_id,
        sequence_key=seq.key if seq else "",
        cut_id=render.cut_id,
        template=render.template,
        state=render.state.value,
        progress=render.progress,
        start_frame=render.start_frame,
        end_frame=render.end_frame,
        out_name=out_path.name if out_path else None,
        size_bytes=out_path.stat().st_size if out_path and out_path.exists() else None,
        error=render.error,
        processing_device=render.processing_device,
        created_at=render.created_at,
        started_at=render.started_at,
        finished_at=render.finished_at,
    )


def _sequence_out(session: Session, seq: Sequence) -> schemas.SequenceOut:
    clips = session.exec(
        select(Clip).where(Clip.sequence_id == seq.id).order_by(Clip.part_index)  # type: ignore[arg-type]
    ).all()
    merged = to_absolute(seq.merged_path)
    return schemas.SequenceOut(
        id=seq.id or 0,
        key=seq.key,
        label=seq.label or seq.key,
        color=seq.color,
        state=seq.state.value,
        part_count=seq.part_count,
        width=seq.width,
        height=seq.height,
        fps=(seq.fps_num / seq.fps_den) if seq.fps_den else 0.0,
        fps_num=seq.fps_num,
        fps_den=seq.fps_den,
        duration_ms=seq.duration_ms,
        frame_count=seq.frame_count,
        size_bytes=seq.size_bytes,
        recorded_at=seq.recorded_at,
        has_proxy=path_exists(seq.proxy_path),
        has_filmstrip=path_exists(seq.filmstrip_path),
        proxy_width=seq.proxy_width,
        proxy_height=seq.proxy_height,
        cut_count=_count(session, Cut, sequence_id=seq.id),
        render_count=_count(session, Render, sequence_id=seq.id),
        # One part without gyro makes the whole sequence unstabilizable: better to
        # see it in the list than after derushing for nothing.
        has_gyro=bool(clips) and all(c.has_gyro for c in clips),
        part_names=[c.filename for c in clips],
        merged_name=merged.name if merged is not None else None,
        error=seq.error,
    )


def _grade_out(session: Session, grade: Grade) -> schemas.GradeOut:
    render = session.get(Render, grade.render_id)
    seq = session.get(Sequence, render.sequence_id) if render else None
    render_path = to_absolute(render.out_path) if render else None
    out_path = to_absolute(grade.out_path)
    return schemas.GradeOut(
        id=grade.id or 0,
        render_id=grade.render_id,
        sequence_id=render.sequence_id if render else 0,
        sequence_key=seq.key if seq else "",
        render_name=render_path.name if render_path else None,
        state=grade.state.value,
        progress=grade.progress,
        params=grading_service.merge_params(grade.params),
        analysis=grade.analysis or {},
        out_name=out_path.name if out_path else None,
        size_bytes=out_path.stat().st_size if out_path and out_path.exists() else None,
        error=grade.error,
        created_at=grade.created_at,
        started_at=grade.started_at,
        finished_at=grade.finished_at,
    )


def _get_render(session: Session, render_id: int) -> Render:
    render = session.get(Render, render_id)
    if render is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown render")
    return render


def _grade_for(session: Session, render: Render, analyse: bool = True) -> Grade:
    """The grade row of a clip, created with a neutral look on first sight.

    The analysis runs once, here: it is a decode-only pass, but on a 30 s clip it
    still costs a few seconds, and nothing about it changes afterwards.
    """
    grade = session.exec(select(Grade).where(Grade.render_id == render.id)).first()
    if grade is None:
        grade = Grade(
            render_id=render.id,  # type: ignore[arg-type]
            params=dict(grading_service.DEFAULTS),
            state=GradeState.DRAFT,
        )
        session.add(grade)
        session.commit()
        session.refresh(grade)

    if analyse and not grade.analysis:
        source = to_absolute(render.out_path)
        if source is not None and source.exists():
            try:
                grade.analysis = grading_service.analyse(source).to_dict()
                session.add(grade)
                session.commit()
            except (grading_service.GradeError, OSError) as exc:
                log.warning("Analysis failed for render %s: %s", render.id, exc)
    return grade


def _job_out(job: Job, key: str | None = None) -> schemas.JobOut:
    return schemas.JobOut(
        id=job.id or 0,
        kind=job.kind.value,
        state=job.state.value,
        progress=job.progress,
        message=job.message,
        error=job.error,
        sequence_id=job.sequence_id,
        sequence_key=key,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _get_sequence(session: Session, sequence_id: int) -> Sequence:
    seq = session.get(Sequence, sequence_id)
    if seq is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown sequence")
    return seq


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #

@router.get("/status", response_model=schemas.StatusOut)
def get_status(session: Session = Depends(get_session)) -> schemas.StatusOut:
    inbox_pending = 0
    if settings.inbox_dir.is_dir():
        inbox_pending = sum(
            1
            for p in settings.inbox_dir.rglob("*")
            if p.is_file()
            and p.suffix.lower() in settings.extensions
            and ".duplicates" not in p.parts
        )
    counts = {
        "clips": _count(session, Clip),
        "sequences": _count(session, Sequence),
        "sequences_ready": _count(session, Sequence, state=SequenceState.READY),
        "sequences_failed": _count(session, Sequence, state=SequenceState.FAILED),
        "cuts": _count(session, Cut),
        "renders": _count(session, Render),
        "renders_done": _count(session, Render, state=RenderState.DONE),
        "grades_done": _count(session, Grade, state=GradeState.DONE),
        "jobs_queued": _count(session, Job, state=JobState.QUEUED),
        "jobs_running": _count(session, Job, state=JobState.RUNNING),
        "jobs_failed": _count(session, Job, state=JobState.FAILED),
    }
    workers = [
        schemas.WorkerOut(
            id=w.id or 0,
            name=w.name,
            capabilities=w.capabilities or {},
            concurrency=w.concurrency,
            last_seen_at=w.last_seen_at,
            online=dispatch.is_online(w),
            running=_count(session, Job, state=JobState.RUNNING, worker_id=w.id),
        )
        for w in session.exec(select(Worker).order_by(Worker.name)).all()  # type: ignore[arg-type]
    ]
    return schemas.StatusOut(
        capabilities=detect().to_dict(),
        workers=workers,
        counts=counts,
        inbox_pending=inbox_pending,
        settings={
            "data_dir": str(settings.data_dir),
            "scan_interval_s": settings.scan_interval_s,
            "split_gap_tolerance_s": settings.split_gap_tolerance_s,
            "proxy_height": settings.proxy_height,
            "purge_parts_after_merge": settings.purge_parts_after_merge,
        },
    )


@router.post("/scan", response_model=schemas.ScanOut)
async def trigger_scan(session: Session = Depends(get_session)) -> schemas.ScanOut:
    """Force an immediate scan, without waiting for the worker's turn."""
    result = await run_in_threadpool(ingest_and_group, session, True)
    return schemas.ScanOut(
        ingested=[c.filename for c in result.ingested],
        duplicates=result.duplicates,
        rejected=result.rejected,
        failed=result.failed,
        sequences=[s.key for s in result.sequences],
    )


@router.post("/upload/check", response_model=schemas.UploadCheckOut)
async def upload_check(
    request: Request,
    size: int = Query(..., ge=1, description="Size of the whole file, in bytes"),
    session: Session = Depends(get_session),
) -> schemas.UploadCheckOut:
    """Tell whether a file is already imported, without sending it.

    The body carries only the probe bytes the fingerprint needs — head chunk
    followed by tail chunk, both derivable from `size`, 2 MiB at most. Sending
    4 GB only to have the scanner drop it in `.duplicates/` wastes the upload and
    twice the disk.
    """
    head_len, tail_len = fingerprint_lengths(size)
    body = await request.body()
    if len(body) != head_len + tail_len:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"expected {head_len + tail_len} probe bytes for a {size} byte file, got {len(body)}",
        )

    fp = fingerprint_parts(size, body[:head_len], body[head_len:])
    clip = session.exec(select(Clip).where(Clip.fingerprint == fp)).first()
    return schemas.UploadCheckOut(
        fingerprint=fp,
        known=clip is not None,
        filename=clip.filename if clip else None,
        sequence_id=clip.sequence_id if clip else None,
    )


@router.put("/upload/{filename}", response_model=schemas.UploadOut)
async def upload(filename: str, request: Request) -> schemas.UploadOut:
    """Stream a file into `inbox/`.

    Deliberately not multipart: FastAPI parses the whole body before calling the
    view, so a 4 GB rush would first be written in full to a temporary file and
    then copied over. Here we write straight to the destination, under a
    `.partial` suffix the scanner ignores, and rename at the end — the file only
    looks complete once it is.
    """
    safe_name = Path(filename).name
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid file name")
    if Path(safe_name).suffix.lower() not in settings.extensions:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"unsupported extension; expected {', '.join(settings.extensions)}",
        )

    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_destination(settings.inbox_dir, safe_name)
    partial = destination.with_name(destination.name + ".partial")

    written = 0
    try:
        handle = await run_in_threadpool(partial.open, "wb")
        try:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(handle.write, chunk)
                    written += len(chunk)
        finally:
            await run_in_threadpool(handle.close)
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    if written == 0:
        partial.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty request body")

    partial.rename(destination)
    mark_upload_complete(destination)
    log.info("Received %s (%.1f MB)", destination.name, written / (1 << 20))
    return schemas.UploadOut(filename=destination.name, size_bytes=written)


@router.get("/templates", response_model=list[schemas.TemplateOut])
def get_templates() -> list[schemas.TemplateOut]:
    return [schemas.TemplateOut(**t.to_dict()) for t in gyroflow_service.list_templates()]


# --------------------------------------------------------------------------- #
# Sequences
# --------------------------------------------------------------------------- #

@router.get("/sequences", response_model=list[schemas.SequenceOut])
def list_sequences(
    state: str | None = None,
    session: Session = Depends(get_session),
) -> list[schemas.SequenceOut]:
    statement = select(Sequence).order_by(Sequence.recorded_at.desc(), Sequence.id.desc())  # type: ignore[union-attr]
    if state:
        statement = statement.where(Sequence.state == state)
    return [_sequence_out(session, s) for s in session.exec(statement).all()]


@router.get("/sequences/{sequence_id}", response_model=schemas.SequenceDetail)
def get_sequence(sequence_id: int, session: Session = Depends(get_session)) -> schemas.SequenceDetail:
    seq = _get_sequence(session, sequence_id)
    clips = session.exec(
        select(Clip).where(Clip.sequence_id == seq.id).order_by(Clip.part_index)  # type: ignore[arg-type]
    ).all()
    cuts = session.exec(
        select(Cut).where(Cut.sequence_id == seq.id).order_by(Cut.order_index)  # type: ignore[arg-type]
    ).all()
    renders = session.exec(
        select(Render).where(Render.sequence_id == seq.id).order_by(Render.id.desc())  # type: ignore[union-attr]
    ).all()

    base = _sequence_out(session, seq)
    return schemas.SequenceDetail(
        **base.model_dump(),
        clips=[
            schemas.ClipOut(
                id=c.id or 0, filename=c.filename, part_index=c.part_index,
                size_bytes=c.size_bytes, duration_ms=c.duration_ms, width=c.width,
                height=c.height, codec=c.codec, has_gyro=c.has_gyro,
                recorded_at=c.recorded_at, camera_index=c.camera_index, state=c.state.value,
            )
            for c in clips
        ],
        cuts=[_cut_out(c, seq) for c in cuts],
        renders=[_render_out(r, seq) for r in renders],
    )


@router.patch("/sequences/{sequence_id}", response_model=schemas.SequenceOut)
def update_sequence(
    sequence_id: int,
    label: str | None = Query(None, min_length=1, max_length=200),
    color: str | None = Query(None, max_length=20),
    session: Session = Depends(get_session),
) -> schemas.SequenceOut:
    """Rename a rush, tag it with a colour, or both. Absent means unchanged."""
    if label is None and color is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing to update")
    seq = _get_sequence(session, sequence_id)
    if label is not None:
        seq.label = label
    if color is not None:
        seq.color = color
    seq.updated_at = utcnow()
    session.add(seq)
    session.commit()
    return _sequence_out(session, seq)


@router.post("/sequences/{sequence_id}/retry", response_model=schemas.SequenceOut)
def retry_sequence(sequence_id: int, session: Session = Depends(get_session)) -> schemas.SequenceOut:
    """Re-run whichever step is missing: the merge if absent, otherwise the proxy."""
    seq = _get_sequence(session, sequence_id)
    seq.error = None
    if path_exists(seq.merged_path):
        seq.state = SequenceState.MERGED
        session.add(seq)
        session.commit()
        enqueue_proxy(session, seq, priority=PRIORITY_MANUAL)
    else:
        seq.state = SequenceState.NEW
        session.add(seq)
        session.commit()
        enqueue_merge(session, seq, priority=PRIORITY_MANUAL)
    return _sequence_out(session, seq)


@router.delete("/sequences/{sequence_id}")
def delete_sequence(
    sequence_id: int,
    keep_raw: bool = True,
    keep_derived: bool = True,
    session: Session = Depends(get_session),
) -> dict:
    """Delete the sequence row. What is kept on disk is up to the caller.

    By default both the masters in `raw/` and the derived files (merge, proxy,
    filmstrip) stay: the derived ones carry the content hash in their name, so
    regrouping the same parts finds them again and skips the reprocessing
    entirely. `keep_derived=false` is the real cleanup, `keep_raw=false` the full
    purge. Renders in `out/` always go — they belong to this sequence's cuts,
    which are being deleted.
    """
    seq = _get_sequence(session, sequence_id)
    removed: list[str] = []

    if not keep_derived:
        for attribute in ("merged_path", "proxy_path", "filmstrip_path"):
            target = to_absolute(getattr(seq, attribute))
            if target and target.exists():
                target.unlink(missing_ok=True)
                removed.append(target.name)
        poster = settings.proxies_dir / f"{seq.artifact_stem}.poster.jpg"
        poster.unlink(missing_ok=True)
        gyro_service.chart_path(seq.artifact_stem).unlink(missing_ok=True)

    for render in session.exec(select(Render).where(Render.sequence_id == seq.id)).all():
        rendered = to_absolute(render.out_path)
        if rendered and rendered.exists():
            rendered.unlink(missing_ok=True)
            removed.append(rendered.name)
        session.delete(render)
    for cut in session.exec(select(Cut).where(Cut.sequence_id == seq.id)).all():
        session.delete(cut)
    for job in session.exec(select(Job).where(Job.sequence_id == seq.id)).all():
        session.delete(job)

    for clip in session.exec(select(Clip).where(Clip.sequence_id == seq.id)).all():
        if keep_raw:
            clip.sequence_id = None
            clip.part_index = 0
            clip.state = ClipState.INGESTED
            session.add(clip)
        else:
            master = to_absolute(clip.raw_path)
            if master and master.exists():
                master.unlink(missing_ok=True)
                removed.append(master.name)
            session.delete(clip)

    session.delete(seq)
    session.commit()
    return {"deleted": seq.key, "files_removed": removed}


@router.post("/sequences/regroup", response_model=schemas.SequenceOut)
def regroup(payload: schemas.RegroupRequest, session: Session = Depends(get_session)) -> schemas.SequenceOut:
    """Join clips into a single sequence by hand.

    Useful when a part showed up after its sequence had already been merged:
    automatic detection never touches a sequence that has been produced.

    The caller names either clips or sequences whose parts should be joined — the
    UI only knows about sequences.
    """
    clips = [session.get(Clip, cid) for cid in payload.clip_ids]
    if any(c is None for c in clips):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown clip in the list")
    clips = [c for c in clips if c is not None]

    known = {c.id for c in clips}
    for sid in payload.sequence_ids:
        if session.get(Sequence, sid) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown sequence {sid}")
        for clip in session.exec(select(Clip).where(Clip.sequence_id == sid)).all():
            if clip.id not in known:
                known.add(clip.id)
                clips.append(clip)

    if len(clips) < 2:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "at least two parts are needed to join"
        )

    clips.sort(key=lambda c: (as_utc(c.recorded_at) or utcnow(), c.camera_index or 0, c.filename))

    touched_ids = {c.sequence_id for c in clips if c.sequence_id}
    for sid in touched_ids:
        old = session.get(Sequence, sid)
        if old is None:
            continue
        if old.state == SequenceState.READY and not payload.force:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"sequence {old.key} is already ready; pass force=true to redo it",
            )
        delete_sequence(sid, keep_raw=True, session=session)

    first = clips[0]
    key = f"{Path(first.filename).stem}"
    if session.exec(select(Sequence).where(Sequence.key == key)).first() is not None:
        key = f"{key}__r{first.id}"

    seq = Sequence(
        key=key,
        label=payload.label or key,
        content_hash=sequence_hash([c.fingerprint for c in clips]),
        state=SequenceState.NEW,
        part_count=len(clips),
        width=first.width,
        height=first.height,
        fps_num=first.fps_num,
        fps_den=first.fps_den,
        duration_ms=sum(c.duration_ms for c in clips),
        size_bytes=sum(c.size_bytes for c in clips),
        recorded_at=first.recorded_at,
    )
    session.add(seq)
    session.commit()
    session.refresh(seq)
    for index, clip in enumerate(clips):
        clip.sequence_id = seq.id
        clip.part_index = index
        session.add(clip)
    session.commit()

    # Same parts, same order, same merge: if that file is still on disk from a
    # previous run, the whole heavy part is already done.
    adopt_existing_artifacts(session, seq)
    if seq.state != SequenceState.READY:
        enqueue_pending(session, seq)
    return _sequence_out(session, seq)


# --------------------------------------------------------------------------- #
# Cuts (derush)
# --------------------------------------------------------------------------- #

@router.put("/sequences/{sequence_id}/cuts", response_model=list[schemas.CutOut])
def replace_cuts(
    sequence_id: int,
    payload: schemas.CutsReplaceIn,
    session: Session = Depends(get_session),
) -> list[schemas.CutOut]:
    seq = _get_sequence(session, sequence_id)
    last_frame = max(seq.frame_count - 1, 0)

    cleaned: list[schemas.CutIn] = []
    for cut in payload.cuts:
        start = max(0, min(cut.start_frame, last_frame))
        end = max(0, min(cut.end_frame, last_frame))
        if end < start:
            start, end = end, start
        if end == start:
            continue  # a single-frame cut makes no sense
        cleaned.append(schemas.CutIn(label=cut.label, start_frame=start, end_frame=end))
    cleaned.sort(key=lambda c: c.start_frame)

    for existing in session.exec(select(Cut).where(Cut.sequence_id == seq.id)).all():
        session.delete(existing)
    session.commit()

    created: list[Cut] = []
    for index, cut in enumerate(cleaned):
        row = Cut(
            sequence_id=seq.id,  # type: ignore[arg-type]
            order_index=index,
            label=cut.label or f"cut {index + 1}",
            start_frame=cut.start_frame,
            end_frame=cut.end_frame,
        )
        session.add(row)
        created.append(row)
    seq.updated_at = utcnow()
    session.add(seq)
    session.commit()
    for row in created:
        session.refresh(row)
    return [_cut_out(c, seq) for c in created]


# --------------------------------------------------------------------------- #
# Renders
# --------------------------------------------------------------------------- #

@router.post("/sequences/{sequence_id}/renders", response_model=list[schemas.RenderOut])
def create_renders(
    sequence_id: int,
    payload: schemas.RenderRequest,
    session: Session = Depends(get_session),
) -> list[schemas.RenderOut]:
    seq = _get_sequence(session, sequence_id)
    if not path_exists(seq.merged_path):
        raise HTTPException(status.HTTP_409_CONFLICT, "the sequence has no merged file")
    try:
        gyroflow_service.get_template(payload.template)
    except gyroflow_service.GyroflowError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    targets: list[int | None]
    if payload.whole_sequence:
        targets = [None]
    else:
        cuts = session.exec(
            select(Cut).where(Cut.sequence_id == seq.id).order_by(Cut.order_index)  # type: ignore[arg-type]
        ).all()
        if payload.cut_ids:
            wanted = set(payload.cut_ids)
            cuts = [c for c in cuts if c.id in wanted]
        if not cuts:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "no cut to render; mark zones or ask for whole_sequence",
            )
        targets = [c.id for c in cuts]

    created: list[Render] = []
    for cut_id in targets:
        render = Render(
            sequence_id=seq.id,  # type: ignore[arg-type]
            cut_id=cut_id,
            template=payload.template,
            state=RenderState.QUEUED,
            overrides=payload.overrides or {},
        )
        session.add(render)
        session.commit()
        session.refresh(render)
        job = Job(
            kind=JobKind.RENDER,
            state=JobState.QUEUED,
            priority=50,
            sequence_id=seq.id,
            render_id=render.id,
            payload={"render_id": render.id, "sequence_id": seq.id},
        )
        session.add(job)
        session.commit()
        created.append(render)

    return [_render_out(r, seq) for r in created]


@router.get("/renders", response_model=list[schemas.RenderOut])
def list_renders(
    state: str | None = None,
    session: Session = Depends(get_session),
) -> list[schemas.RenderOut]:
    statement = select(Render).order_by(Render.id.desc())  # type: ignore[union-attr]
    if state:
        statement = statement.where(Render.state == state)
    renders = session.exec(statement).all()
    sequences = {s.id: s for s in session.exec(select(Sequence)).all()}
    return [_render_out(r, sequences.get(r.sequence_id)) for r in renders]


@router.delete("/renders/{render_id}")
def delete_render(render_id: int, session: Session = Depends(get_session)) -> dict:
    render = session.get(Render, render_id)
    if render is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown render")
    rendered = to_absolute(render.out_path)
    if rendered and rendered.exists():
        rendered.unlink(missing_ok=True)
    for job in session.exec(select(Job).where(Job.render_id == render.id)).all():
        session.delete(job)
    session.delete(render)
    session.commit()
    return {"deleted": render_id}


# --------------------------------------------------------------------------- #
# Colour grading
# --------------------------------------------------------------------------- #

@router.get("/grades", response_model=list[schemas.GradeOut])
def list_grades(session: Session = Depends(get_session)) -> list[schemas.GradeOut]:
    grades = session.exec(select(Grade).order_by(Grade.id.desc())).all()  # type: ignore[union-attr]
    return [_grade_out(session, g) for g in grades]


@router.get("/renders/{render_id}/grade", response_model=schemas.GradeOut)
async def get_grade(render_id: int, session: Session = Depends(get_session)) -> schemas.GradeOut:
    render = _get_render(session, render_id)
    grade = await run_in_threadpool(_grade_for, session, render)
    return _grade_out(session, grade)


@router.put("/renders/{render_id}/grade", response_model=schemas.GradeOut)
def save_grade(
    render_id: int,
    payload: schemas.GradeParamsIn,
    session: Session = Depends(get_session),
) -> schemas.GradeOut:
    render = _get_render(session, render_id)
    grade = _grade_for(session, render, analyse=False)
    grade.params = grading_service.merge_params(payload.params)
    grade.params_hash = grading_service.params_hash(grade.params)
    if grade.state in {GradeState.DONE, GradeState.FAILED}:
        grade.state = GradeState.DRAFT  # the look changed, the file no longer matches
    session.add(grade)
    session.commit()
    return _grade_out(session, grade)


@router.get("/renders/{render_id}/grade/preview")
async def grade_preview(
    render_id: int,
    request: Request,
    at_ms: float = Query(0.0, ge=0),
    exposure: float = Query(0.0),
    contrast: float = Query(1.0),
    saturation: float = Query(1.0),
    temperature: int = Query(6500),
    shadows: float = Query(0.0),
    highlights: float = Query(0.0),
    auto_levels: bool = Query(False),
    session: Session = Depends(get_session),
) -> Response:
    """One graded frame, straight from the clip.

    Parameters travel in the query string rather than being read from the saved
    row: the point is to see a look *before* committing to it. Measured at 0.32 s,
    which is what makes a slider usable.
    """
    render = _get_render(session, render_id)
    source = to_absolute(render.out_path)
    if source is None or not source.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "stabilized clip not found")
    grade = _grade_for(session, render, analyse=False)

    params = {
        "exposure": exposure, "contrast": contrast, "saturation": saturation,
        "temperature": temperature, "shadows": shadows, "highlights": highlights,
        "auto_levels": auto_levels,
    }
    dest = settings.tmp_dir / f"preview_{render_id}_{grading_service.params_hash(params)}_{int(at_ms)}.jpg"
    if not dest.exists():
        try:
            await run_in_threadpool(
                grading_service.preview_frame, source, dest, at_ms, params, grade.analysis
            )
        except (grading_service.GradeError, OSError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return media.serve_file(dest, request)


@router.post("/renders/{render_id}/grade/apply", response_model=schemas.GradeOut)
def apply_grade(
    render_id: int,
    session: Session = Depends(get_session),
) -> schemas.GradeOut:
    render = _get_render(session, render_id)
    if render.state != RenderState.DONE:
        raise HTTPException(status.HTTP_409_CONFLICT, "the clip is not stabilized yet")
    grade = _grade_for(session, render, analyse=False)
    if grading_service.is_neutral(grade.params):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the look is neutral; encoding it again would only cost quality",
        )

    grade.state = GradeState.QUEUED
    grade.progress = 0.0
    grade.error = None
    session.add(grade)
    session.commit()

    job = Job(
        kind=JobKind.GRADE,
        state=JobState.QUEUED,
        priority=60,
        sequence_id=render.sequence_id,
        render_id=render.id,
        grade_id=grade.id,
        payload={"grade_id": grade.id, "render_id": render.id},
    )
    session.add(job)
    session.commit()
    return _grade_out(session, grade)


@router.delete("/grades/{grade_id}")
def delete_grade(grade_id: int, session: Session = Depends(get_session)) -> dict:
    grade = session.get(Grade, grade_id)
    if grade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown grade")
    graded = to_absolute(grade.out_path)
    if graded and graded.exists():
        graded.unlink(missing_ok=True)
    grade.out_path = None
    grade.state = GradeState.DRAFT
    grade.progress = 0.0
    session.add(grade)
    for job in session.exec(select(Job).where(Job.grade_id == grade.id)).all():
        session.delete(job)
    session.commit()
    return {"deleted": grade_id}


@router.get("/media/graded/{grade_id}")
def get_graded_file(grade_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    grade = session.get(Grade, grade_id)
    path = to_absolute(grade.out_path) if grade else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "graded file not found")
    return media.serve_file(path, request)


@router.get("/media/graded/{grade_id}/download")
def download_graded_file(grade_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    grade = session.get(Grade, grade_id)
    path = to_absolute(grade.out_path) if grade else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "graded file not found")
    return media.serve_file(path, request, download_name=path.name)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

@router.get("/jobs", response_model=list[schemas.JobOut])
def list_jobs(
    state: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[schemas.JobOut]:
    statement = select(Job).order_by(Job.id.desc()).limit(limit)  # type: ignore[union-attr]
    if state:
        statement = statement.where(Job.state == state)
    jobs = session.exec(statement).all()
    keys = {s.id: s.key for s in session.exec(select(Sequence)).all()}
    return [_job_out(j, keys.get(j.sequence_id) if j.sequence_id else None) for j in jobs]


@router.post("/jobs/{job_id}/retry", response_model=schemas.JobOut)
def retry_job(job_id: int, session: Session = Depends(get_session)) -> schemas.JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job")
    job.state = JobState.QUEUED
    job.progress = 0.0
    job.error = None
    job.message = "retried manually"
    job.started_at = None
    job.finished_at = None
    session.add(job)
    if job.kind == JobKind.RENDER and job.render_id:
        render = session.get(Render, job.render_id)
        if render is not None:
            render.state = RenderState.QUEUED
            render.progress = 0.0
            render.error = None
            session.add(render)
    session.commit()
    return _job_out(job)


@router.get("/jobs/stream")
async def stream_jobs(request: Request) -> StreamingResponse:
    """SSE: the API reads back the `job` table the worker keeps updating."""
    from ..db import session_scope

    async def event_source():
        last_payload = None
        while True:
            if await request.is_disconnected():
                break
            def snapshot() -> list[dict]:
                with session_scope() as session:
                    jobs = session.exec(
                        select(Job)
                        .where(Job.state.in_([JobState.QUEUED, JobState.RUNNING]))  # type: ignore[union-attr]
                        .order_by(Job.priority, Job.id)  # type: ignore[arg-type]
                    ).all()
                    keys = {s.id: s.key for s in session.exec(select(Sequence)).all()}
                    return [
                        _job_out(j, keys.get(j.sequence_id) if j.sequence_id else None).model_dump(mode="json")
                        for j in jobs
                    ]

            payload = await run_in_threadpool(snapshot)
            body = json.dumps(payload)
            if body != last_payload:
                last_payload = body
                yield f"data: {body}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #

@router.get("/media/proxy/{sequence_id}")
def get_proxy(sequence_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    seq = _get_sequence(session, sequence_id)
    proxy = to_absolute(seq.proxy_path)
    if proxy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proxy not generated yet")
    return media.serve_file(proxy, request)


@router.get("/media/filmstrip/{sequence_id}")
def get_filmstrip(sequence_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    seq = _get_sequence(session, sequence_id)
    filmstrip = to_absolute(seq.filmstrip_path)
    if filmstrip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "filmstrip not generated yet")
    return media.serve_file(filmstrip, request)


@router.get("/media/poster/{sequence_id}")
def get_poster(sequence_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    seq = _get_sequence(session, sequence_id)
    poster = settings.proxies_dir / f"{seq.artifact_stem}.poster.jpg"
    return media.serve_file(poster, request)


@router.get("/media/gyro/{sequence_id}")
async def get_gyro_chart(
    sequence_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """Downsampled telemetry for the chart under the derush timeline.

    Built during the proxy step, but generated here on first request for
    sequences that predate the feature — a dozen seconds, once. A chart written by
    an older version is rebuilt the same way rather than served in a shape the
    front no longer reads.
    """
    seq = _get_sequence(session, sequence_id)
    chart = gyro_service.chart_path(seq.artifact_stem)
    if chart.exists() and not gyro_service.is_current(chart):
        log.info("Rebuilding %s: older payload format", chart.name)
        chart.unlink(missing_ok=True)
    if not chart.exists():
        merged = to_absolute(seq.merged_path)
        if merged is None or not merged.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no merged file to read telemetry from")
        try:
            await run_in_threadpool(
                gyro_service.build_chart, merged, chart, seq.duration_ms
            )
        except (gyro_service.GyroError, OSError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return media.serve_file(chart, request)


@router.get("/media/render/{render_id}")
def get_render_file(render_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    render = session.get(Render, render_id)
    path = to_absolute(render.out_path) if render else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "render file not found")
    return media.serve_file(path, request)


@router.get("/media/render/{render_id}/download")
def download_render(render_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    render = session.get(Render, render_id)
    path = to_absolute(render.out_path) if render else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "render file not found")
    return media.serve_file(path, request, download_name=path.name)
