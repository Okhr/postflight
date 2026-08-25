from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import unicodedata
from collections import Counter
from collections.abc import Iterable
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
    Folder,
    Grade,
    GradeState,
    Job,
    JobKind,
    JobState,
    Look,
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
    enqueue_merge,
    enqueue_proxy,
    ingest_and_group,
    mark_upload_complete,
    upload_finished,
    upload_started,
    unique_destination,
)
from ..services import grading as grading_service
from ..services import gyro as gyro_service
from ..services import gyroflow as gyroflow_service
from ..services.probe import fingerprint_lengths, fingerprint_parts
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


def _cuts_out(session: Session, seq: Sequence, cuts: Iterable[Cut]) -> list[schemas.CutOut]:
    """The cuts of one rush, each carrying whether it has been stabilized and graded.

    Both are computed here rather than left to the caller to cross-reference: the
    rush tree draws one icon per state, and it only ever asks for the cuts.
    """
    done = session.exec(
        select(Render.id, Render.cut_id).where(
            Render.sequence_id == seq.id, Render.state == RenderState.DONE
        )
    ).all()
    rendered = {cut_id for _, cut_id in done if cut_id is not None}
    graded: set[int] = set()
    files: Counter[int] = Counter()
    by_render = {render_id: cut_id for render_id, cut_id in done if cut_id is not None}
    files.update(by_render.values())
    if by_render:
        for render_id in session.exec(
            select(Grade.render_id).where(
                Grade.render_id.in_(by_render),  # type: ignore[attr-defined]
                Grade.state == GradeState.DONE,
            )
        ).all():
            graded.add(by_render[render_id])
            files[by_render[render_id]] += 1

    out: list[schemas.CutOut] = []
    for cut in cuts:
        frames = cut.end_frame - cut.start_frame + 1
        out.append(
            schemas.CutOut(
                id=cut.id or 0,
                order_index=cut.order_index,
                label=cut.label,
                start_frame=cut.start_frame,
                end_frame=cut.end_frame,
                frames=frames,
                duration_ms=frame_to_ms(frames, seq.fps_num, seq.fps_den) if seq.fps_num else 0.0,
                start_tc=format_timecode(cut.start_frame, seq.fps_num, seq.fps_den) if seq.fps_num else "",
                end_tc=format_timecode(cut.end_frame, seq.fps_num, seq.fps_den) if seq.fps_num else "",
                rendered=cut.id in rendered,
                graded=cut.id in graded,
                files=files[cut.id or 0],
            )
        )
    return out


def _render_out(
    render: Render, seq: Sequence | None = None, cut_label: str = ""
) -> schemas.RenderOut:
    out_path = to_absolute(render.out_path)
    frames = render.end_frame - render.start_frame + 1
    return schemas.RenderOut(
        id=render.id or 0,
        sequence_id=render.sequence_id,
        sequence_key=seq.key if seq else "",
        sequence_label=seq.label if seq else "",
        # Where its rush sits, so the colour page can group its clips the way every
        # other list here is grouped.
        folder_id=seq.folder_id if seq else None,
        cut_label=cut_label,
        # From the rush's own fps. The colour page derived it from the frame count at
        # a hardcoded 60, which is wrong on every 60000/1001 rush there is.
        duration_ms=frame_to_ms(frames, seq.fps_num, seq.fps_den) if seq and seq.fps_num else 0.0,
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


# The palette a new folder draws from. Tokens, not colours: what each one looks like
# is decided in `frontend/src/lib/colors.ts`, which is the same list.
FOLDER_COLORS = ["red", "amber", "emerald", "sky", "violet", "pink"]


def _sequence_out(session: Session, seq: Sequence) -> schemas.SequenceOut:
    clips = session.exec(
        select(Clip).where(Clip.sequence_id == seq.id).order_by(Clip.part_index)  # type: ignore[arg-type]
    ).all()
    merged = to_absolute(seq.merged_path)
    return schemas.SequenceOut(
        id=seq.id or 0,
        key=seq.key,
        label=seq.label or seq.key,
        folder_id=seq.folder_id,
        state=seq.state.value,
        derushed=seq.derushed,
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
    analysis = (render.analysis if render else None) or {}
    return schemas.GradeOut(
        id=grade.id or 0,
        render_id=grade.render_id,
        label=grade.label,
        sequence_id=render.sequence_id if render else 0,
        sequence_key=seq.key if seq else "",
        render_name=render_path.name if render_path else None,
        state=grade.state.value,
        progress=grade.progress,
        params=grading_service.merge_params(grade.params),
        analysis=analysis,
        # Off the clip, not off this grade: where the two points would go if measured.
        suggested=grading_service.suggest_levels(analysis),
        out_name=out_path.name if out_path else None,
        size_bytes=out_path.stat().st_size if out_path and out_path.exists() else None,
        error=grade.error,
        created_at=grade.created_at,
        started_at=grade.started_at,
        finished_at=grade.finished_at,
    )


def _purge_render(session: Session, render: Render) -> list[str]:
    """Delete a render and everything hanging off it, files included.

    Deleting a parent deletes its children: a render is the parent of its grades, so
    their files go with it. They used to stay, which left `grade` rows pointing at a
    render that no longer existed (`render_id` is not a foreign key, so nothing
    complained) and files in `graded/` nothing could name.

    Does not commit: the callers delete more than one thing at a time.
    """
    removed: list[str] = []
    rendered = to_absolute(render.out_path)
    if rendered and rendered.exists():
        rendered.unlink(missing_ok=True)
        removed.append(rendered.name)
    for grade in session.exec(select(Grade).where(Grade.render_id == render.id)).all():
        graded = to_absolute(grade.out_path)
        if graded and graded.exists():
            graded.unlink(missing_ok=True)
            removed.append(graded.name)
        for job in session.exec(select(Job).where(Job.grade_id == grade.id)).all():
            session.delete(job)
        session.delete(grade)
    for job in session.exec(select(Job).where(Job.render_id == render.id)).all():
        session.delete(job)
    session.delete(render)
    return removed


def _get_render(session: Session, render_id: int) -> Render:
    render = session.get(Render, render_id)
    if render is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown render")
    return render


def _analyse_render(session: Session, render: Render) -> dict:
    """What signalstats measured on the stabilized clip, measured once.

    It lives on the render because it describes the clip and not the look: the grades
    of one clip all read the same numbers, and this is a decode pass of a few seconds
    that must not run once per grade.
    """
    if render.analysis:
        return render.analysis
    source = to_absolute(render.out_path)
    if source is None or not source.exists():
        return {}
    try:
        render.analysis = grading_service.analyse(source).to_dict()
    except (grading_service.GradeError, OSError) as exc:
        log.warning("Analysis failed for render %s: %s", render.id, exc)
        return {}
    session.add(render)
    session.commit()
    return render.analysis


def _get_grade(session: Session, grade_id: int) -> Grade:
    grade = session.get(Grade, grade_id)
    if grade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown grade")
    return grade


def _free_grade_label(session: Session, render: Render) -> str:
    """The first "Grade N" this clip does not already carry."""
    taken = {
        g.label
        for g in session.exec(select(Grade).where(Grade.render_id == render.id)).all()
    }
    number = 1
    while f"Grade {number}" in taken:
        number += 1
    return f"Grade {number}"


def _write_params(session: Session, grade: Grade, params: dict) -> Grade:
    """Put a look on a grade, and drop whatever the old one had produced.

    A look that moved makes whatever exists, or is being written, the wrong file. An
    encode in flight is cancelled the way a render of a deleted cut is: its job goes
    and the worker holding it stops on its next heartbeat. Guarded on the look having
    actually moved, because the page writes on every slider release and a write that
    changes nothing must not kill an encode.
    """
    params = grading_service.merge_params(params)
    digest = grading_service.params_hash(params)
    changed = digest != grade.params_hash
    grade.params = params
    grade.params_hash = digest
    if changed and grade.state is not GradeState.DRAFT:
        for job in session.exec(select(Job).where(Job.grade_id == grade.id)).all():
            session.delete(job)
        grade.state = GradeState.DRAFT
        grade.progress = 0.0
        grade.error = None
    session.add(grade)
    session.commit()
    session.refresh(grade)
    return grade


def _job_names(session: Session, jobs: Iterable[Job]) -> dict[int, tuple[str, str, str]]:
    """Per job: the rush key, the rush name, and the sequence name when there is one.

    Resolved in bulk, four selects whatever the number of jobs: this feeds the bar at
    the top of every page, which asks for it on a stream.
    """
    jobs = list(jobs)
    rushes = {s.id: s for s in session.exec(select(Sequence)).all()}
    render_ids = {j.render_id for j in jobs if j.render_id}
    grade_ids = {j.grade_id for j in jobs if j.grade_id}
    if grade_ids:
        for grade in session.exec(select(Grade).where(Grade.id.in_(grade_ids))).all():  # type: ignore[union-attr]
            render_ids.add(grade.render_id)
    cut_of: dict[int, int | None] = {}
    grade_render: dict[int, int] = {}
    if grade_ids:
        grade_render = {
            g.id or 0: g.render_id
            for g in session.exec(select(Grade).where(Grade.id.in_(grade_ids))).all()  # type: ignore[union-attr]
        }
    if render_ids:
        cut_of = {
            r.id or 0: r.cut_id
            for r in session.exec(select(Render).where(Render.id.in_(render_ids))).all()  # type: ignore[union-attr]
        }
    cut_labels = {
        c.id: c.label
        for c in session.exec(select(Cut).where(Cut.id.in_([v for v in cut_of.values() if v])))  # type: ignore[union-attr]
    } if any(cut_of.values()) else {}

    out: dict[int, tuple[str, str, str]] = {}
    for job in jobs:
        rush = rushes.get(job.sequence_id) if job.sequence_id else None
        render_id = job.render_id or (grade_render.get(job.grade_id or 0) if job.grade_id else None)
        cut_id = cut_of.get(render_id or 0)
        out[job.id or 0] = (
            rush.key if rush else "",
            rush.label if rush else "",
            cut_labels.get(cut_id or 0, ""),
        )
    return out


def _job_out(job: Job, names: tuple[str, str, str] | None = None) -> schemas.JobOut:
    key, label, cut = names or ("", "", "")
    return schemas.JobOut(
        id=job.id or 0,
        kind=job.kind.value,
        state=job.state.value,
        progress=job.progress,
        message=job.message,
        error=job.error,
        sequence_id=job.sequence_id,
        sequence_key=key or None,
        sequence_label=label or None,
        cut_label=cut or None,
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
            rates=w.rates or {},
            observed=w.observed or {},
            shares_data=bool(w.shares_data),
            concurrency=w.concurrency,
            last_seen_at=w.last_seen_at,
            online=dispatch.is_online(w),
            running=_count(session, Job, state=JobState.RUNNING, worker_id=w.id),
        )
        for w in session.exec(select(Worker).order_by(Worker.name)).all()  # type: ignore[arg-type]
    ]
    return schemas.StatusOut(
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

    The body carries only the probe bytes the fingerprint needs: head chunk
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
    `.partial` suffix the scanner ignores, and rename at the end, so the file only
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

    # Announced before the first byte and cleared whatever happens: while this is
    # counted, the scheduled scan holds off, so the parts of one flight are ingested
    # together instead of the first one being merged alone.
    upload_started()
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
    finally:
        upload_finished()

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


@router.get("/templates/defaults", response_model=schemas.TemplateDefaults)
def get_template_defaults() -> schemas.TemplateDefaults:
    """Gyroflow's own starting values, so "back to defaults" is not a number the
    front invented."""
    return schemas.TemplateDefaults(
        codecs=gyroflow_service.CODECS, **gyroflow_service.GYROFLOW_DEFAULTS
    )


@router.post("/templates", response_model=schemas.TemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(payload: schemas.TemplateIn) -> schemas.TemplateOut:
    try:
        created = gyroflow_service.create_template(payload.label, payload.copy_of)
    except gyroflow_service.GyroflowError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return schemas.TemplateOut(**created.to_dict())


@router.patch("/templates/{template_id}", response_model=schemas.TemplateOut)
def update_template(template_id: str, payload: schemas.TemplatePatch) -> schemas.TemplateOut:
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing to update")
    if (codec := values.get("codec")) and codec not in gyroflow_service.CODECS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown codec: {codec}")
    try:
        saved = gyroflow_service.save_template(template_id, values)
    except gyroflow_service.GyroflowError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return schemas.TemplateOut(**saved.to_dict())


@router.delete("/templates/{template_id}")
def delete_template(template_id: str) -> dict:
    """Deletes a template made here, resets one that ships with the image."""
    try:
        return {"template": template_id, "outcome": gyroflow_service.delete_template(template_id)}
    except gyroflow_service.GyroflowError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# --------------------------------------------------------------------------- #
# Sequences
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Folders
# --------------------------------------------------------------------------- #

def _color(token: str | None) -> str:
    """A token from the palette, or one drawn from it.

    Refusing an unknown token rather than storing it: the front decides what each one
    looks like, and a word it has no entry for would come out as no colour at all.
    """
    if token is None:
        return secrets.choice(FOLDER_COLORS)
    if token not in FOLDER_COLORS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown colour {token}; expected one of {', '.join(FOLDER_COLORS)}",
        )
    return token


def _folder_out(session: Session, folder: Folder) -> schemas.FolderOut:
    return schemas.FolderOut(
        id=folder.id or 0,
        name=folder.name,
        color=folder.color,
        parent_id=folder.parent_id,
        position=folder.position,
        sequence_count=_count(session, Sequence, folder_id=folder.id),
    )


def _siblings(session: Session, parent_id: int | None) -> list[Folder]:
    """The folders sharing a parent, in the order they are shown."""
    return list(
        session.exec(
            select(Folder)
            .where(Folder.parent_id == parent_id)
            .order_by(Folder.position, Folder.id)  # type: ignore[arg-type]
        ).all()
    )


def _renumber(session: Session, parent_id: int | None) -> None:
    """Close the gaps in a sibling group, keeping the order it is in."""
    for rank, sibling in enumerate(_siblings(session, parent_id)):
        sibling.position = rank
        session.add(sibling)


def _place(session: Session, folder: Folder, index: int) -> None:
    """Put the folder at `index` among its siblings and renumber them all densely.

    Rebuilding the whole list rather than shifting the neighbours: it is a handful of
    rows, and it is the version that cannot leave two folders sharing a rank.
    """
    others = [f for f in _siblings(session, folder.parent_id) if f.id != folder.id]
    # An index past the end means last, which is also how "no index given" arrives.
    others.insert(max(0, min(index, len(others))), folder)
    for rank, sibling in enumerate(others):
        sibling.position = rank
        session.add(sibling)


def _get_folder(session: Session, folder_id: int) -> Folder:
    folder = session.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown folder")
    return folder


def _check_nesting(session: Session, folder: Folder | None, parent_id: int | None) -> None:
    """Two levels, and a folder is never its own ancestor."""
    if parent_id is None:
        return
    parent = _get_folder(session, parent_id)
    if folder is not None and parent.id == folder.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a folder cannot hold itself")
    if parent.parent_id is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{parent.name} is already inside a folder, and two levels is the limit",
        )
    if folder is not None and session.exec(
        select(Folder).where(Folder.parent_id == folder.id)
    ).first() is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{folder.name} holds folders of its own, so it cannot move into one",
        )


@router.get("/folders", response_model=list[schemas.FolderOut])
def list_folders(session: Session = Depends(get_session)) -> list[schemas.FolderOut]:
    folders = session.exec(
        select(Folder).order_by(Folder.position, Folder.id)  # type: ignore[arg-type]
    ).all()
    return [_folder_out(session, f) for f in folders]


@router.post("/folders", response_model=schemas.FolderOut, status_code=status.HTTP_201_CREATED)
def create_folder(
    payload: schemas.FolderIn, session: Session = Depends(get_session)
) -> schemas.FolderOut:
    _check_nesting(session, None, payload.parent_id)
    folder = Folder(
        name=payload.name.strip(),
        color=_color(payload.color),
        parent_id=payload.parent_id,
    )
    session.add(folder)
    session.flush()
    # Last among its siblings: a new folder appearing in the middle of an order someone
    # arranged by hand would be the surprising choice. Through `_place` rather than by
    # counting the siblings, so creating and moving share one way of assigning a rank,
    # and the group comes out dense whatever state it was in.
    _place(session, folder, 10**6)
    session.commit()
    session.refresh(folder)
    return _folder_out(session, folder)


@router.patch("/folders/{folder_id}", response_model=schemas.FolderOut)
def update_folder(
    folder_id: int, payload: schemas.FolderPatch, session: Session = Depends(get_session)
) -> schemas.FolderOut:
    folder = _get_folder(session, folder_id)
    if payload.name is not None:
        folder.name = payload.name.strip()
    if payload.color is not None:
        folder.color = _color(payload.color)
    left_behind: int | None = None
    moved = False
    if "parent_id" in payload.model_fields_set:
        _check_nesting(session, folder, payload.parent_id)
        moved = folder.parent_id != payload.parent_id
        left_behind = folder.parent_id
        folder.parent_id = payload.parent_id

    if moved or payload.position is not None:
        # Always through `_place`, and always renumbering both groups it touched. The
        # alternative, writing one rank and trusting the rest, is how two siblings end
        # up sharing one: seen on 2026-08-20 with two root folders both at rank 1. The
        # list it left has a hole in it too, which the next insertion would land on.
        _place(session, folder, payload.position if payload.position is not None else 10**6)
        if moved:
            _renumber(session, left_behind)

    session.add(folder)
    session.commit()
    session.refresh(folder)
    return _folder_out(session, folder)


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: int, session: Session = Depends(get_session)) -> dict:
    """Remove the drawer, keep everything that was in it.

    Rushes and child folders come back to the root. A folder holds no footage of its
    own, so there is nothing here that could be lost, and making this destructive
    would only invite a dialog that has nothing true to warn about.
    """
    folder = _get_folder(session, folder_id)
    freed = 0
    for sequence in session.exec(select(Sequence).where(Sequence.folder_id == folder.id)).all():
        sequence.folder_id = None
        session.add(sequence)
        freed += 1
    # The children come back to the root, after whatever is already there, and the
    # whole root is renumbered: the rank the deleted folder held has to close up, or
    # two folders end up sharing one. Their parent is cleared before the delete, since
    # foreign keys are on and a row still pointing at it would refuse to go.
    children = _siblings(session, folder.id)
    orphans = len(children)
    roots = [f for f in _siblings(session, None) if f.id != folder.id]
    for child in children:
        child.parent_id = None
    for rank, sibling in enumerate([*roots, *children]):
        sibling.position = rank
        session.add(sibling)
    session.delete(folder)
    session.commit()
    return {"deleted": folder.name, "rushes_freed": freed, "folders_freed": orphans}


@router.get("/stabilize/queue", response_model=list[schemas.QueueRush])
def stabilize_queue(session: Session = Depends(get_session)) -> list[schemas.QueueRush]:
    """Everything that can be stabilized, in one answer.

    One request rather than one per rush, because the page exists to say what is left
    to do and cannot say it a rush at a time. A rush needs the merged file (`ready`)
    and at least one marked sequence: stabilizing a whole rush is not a thing here.

    Oldest first, like the derush list, which is the order a session is walked in.
    """
    rushes = session.exec(
        select(Sequence).where(Sequence.state == SequenceState.READY)
    ).all()
    if not rushes:
        return []

    ids = [r.id for r in rushes]
    cuts = session.exec(
        select(Cut).where(Cut.sequence_id.in_(ids)).order_by(Cut.order_index)  # type: ignore[union-attr]
    ).all()
    renders = session.exec(select(Render).where(Render.sequence_id.in_(ids))).all()  # type: ignore[union-attr]
    # Which renders have a graded file, so the row says so without a second request.
    # A clip holds several grades, so this is a set: any one of them having produced
    # something is what the droplet reports.
    graded = {
        grade.render_id
        for grade in session.exec(
            select(Grade).where(
                Grade.render_id.in_([r.id for r in renders]),  # type: ignore[attr-defined]
                Grade.state == GradeState.DONE,
            )
        ).all()
    }

    done: dict[int, list[schemas.QueueRender]] = {}
    busy: dict[int, list[schemas.QueueRender]] = {}
    for render in sorted(renders, key=lambda r: r.id or 0):
        if render.cut_id is None:
            continue
        entry = schemas.QueueRender(
            id=render.id or 0,
            template=render.template,
            graded=(render.id or 0) in graded,
        )
        if render.state == RenderState.DONE:
            done.setdefault(render.cut_id, []).append(entry)
        elif render.state in (RenderState.QUEUED, RenderState.RUNNING):
            busy.setdefault(render.cut_id, []).append(entry)

    by_rush: dict[int, list[Cut]] = {}
    for cut in cuts:
        by_rush.setdefault(cut.sequence_id, []).append(cut)

    out: list[schemas.QueueRush] = []
    for rush in sorted(rushes, key=lambda r: (r.recorded_at is None, r.recorded_at, r.id)):
        mine = by_rush.get(rush.id or 0, [])
        if not mine:
            continue
        out.append(
            schemas.QueueRush(
                id=rush.id or 0,
                label=rush.label,
                folder_id=rush.folder_id,
                recorded_at=rush.recorded_at,
                cuts=[
                    schemas.QueueCut(
                        id=cut.id or 0,
                        label=cut.label,
                        frames=cut.end_frame - cut.start_frame + 1,
                        # A rush whose probe failed has no fps. It must not take the
                        # whole queue down with a division by zero.
                        duration_ms=frame_to_ms(
                            cut.end_frame - cut.start_frame + 1, rush.fps_num, rush.fps_den
                        )
                        if rush.fps_num
                        else 0.0,
                        start_tc=format_timecode(cut.start_frame, rush.fps_num, rush.fps_den)
                        if rush.fps_num
                        else "",
                        end_tc=format_timecode(cut.end_frame, rush.fps_num, rush.fps_den)
                        if rush.fps_num
                        else "",
                        done=done.get(cut.id or 0, []),
                        busy=busy.get(cut.id or 0, []),
                    )
                    for cut in mine
                ],
            )
        )
    return out


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
    cut_names = {c.id or 0: c.label for c in cuts}
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
        cuts=_cuts_out(session, seq, cuts),
        renders=[
            _render_out(r, seq, cut_names.get(r.cut_id or 0, "")) for r in renders
        ],
    )


@router.patch("/sequences/{sequence_id}", response_model=schemas.SequenceOut)
def update_sequence(
    sequence_id: int,
    label: str | None = Query(None, min_length=1, max_length=200),
    folder_id: int | None = Query(None, ge=0),
    derushed: bool | None = Query(None),
    session: Session = Depends(get_session),
) -> schemas.SequenceOut:
    """Rename a rush, file it in a folder, mark it derushed. Absent is unchanged.

    `folder_id=0` takes it out of its folder: a query parameter cannot carry null in a
    way that differs from being left out, and both meanings are wanted here.
    """
    if label is None and folder_id is None and derushed is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing to update")
    seq = _get_sequence(session, sequence_id)
    if label is not None:
        seq.label = label
    if derushed is not None:
        seq.derushed = derushed
    if folder_id is not None:
        seq.folder_id = _get_folder(session, folder_id).id if folder_id else None
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
    purge. Renders in `out/` always go, graded files with them: they belong to this
    sequence's cuts, which are being deleted.
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
        removed.extend(_purge_render(session, render))
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
        cleaned.append(schemas.CutIn(id=cut.id, label=cut.label, start_frame=start, end_frame=end))
    cleaned.sort(key=lambda c: c.start_frame)

    # A cut keeps its id across an edit. Deleting the lot and inserting it back was
    # simpler and wrong: a render points at the cut it came from (`render.cut_id`,
    # which `dispatch.prepare` reads to build the trim range), and the rush tree
    # lights an icon per cut that has been stabilized. Both broke on every save, and
    # dragging an edge now saves by itself, so it would break constantly.
    existing = {
        c.id: c
        for c in session.exec(select(Cut).where(Cut.sequence_id == seq.id)).all()
    }
    kept: list[Cut] = []
    for index, cut in enumerate(cleaned):
        row = existing.pop(cut.id, None) if cut.id else None
        if row is None:
            row = Cut(sequence_id=seq.id)  # type: ignore[arg-type]
        row.order_index = index
        row.label = cut.label or f"cut {index + 1}"
        row.start_frame = cut.start_frame
        row.end_frame = cut.end_frame
        session.add(row)
        kept.append(row)
    # What the caller left out is gone, and it takes its renders with it. They have
    # to go first anyway: `render.cut_id` is a real foreign key, so deleting a cut a
    # render points at fails outright.
    for orphan in existing.values():
        _purge_cut(session, orphan)

    seq.updated_at = utcnow()
    session.add(seq)
    session.commit()
    for row in kept:
        session.refresh(row)
    return _cuts_out(session, seq, kept)


def _purge_cut(session: Session, cut: Cut) -> list[str]:
    """Delete a cut and every file made from it. Does not commit.

    Deleting a parent deletes its children, whatever state they are in. A finished
    render used to keep its file and merely lose the link, which left a clip in
    `out/` that no tree could name any more: the only place it still showed up was
    the flat list on the colour page. A render still queued or running goes too, and
    its job in flight stops on the next heartbeat, the way a deleted rush stops one.
    """
    removed: list[str] = []
    for render in session.exec(select(Render).where(Render.cut_id == cut.id)).all():
        removed.extend(_purge_render(session, render))
    # Flushed before the cut goes: `render.cut_id` is a foreign key and no ORM
    # relationship declares the dependency, so nothing would order the two deletes.
    session.flush()
    session.delete(cut)
    return removed


@router.delete("/cuts/{cut_id}")
def delete_cut(cut_id: int, session: Session = Depends(get_session)) -> dict:
    cut = session.get(Cut, cut_id)
    if cut is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown cut")
    seq = session.get(Sequence, cut.sequence_id)
    removed = _purge_cut(session, cut)
    if seq is not None:
        seq.updated_at = utcnow()
        session.add(seq)
    session.commit()
    return {"deleted": cut_id, "files_removed": removed}


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

    named = {c.id: c.label for c in session.exec(
        select(Cut).where(Cut.sequence_id == seq.id)
    ).all()}
    return [_render_out(r, seq, named.get(r.cut_id or 0, "")) for r in created]


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
    # The sequence's name too: the pages that list renders name them the way the tree
    # does, rush then sequence, and a render only knows its cut by id.
    wanted = [r.cut_id for r in renders if r.cut_id]
    cuts = (
        {c.id: c.label for c in session.exec(select(Cut).where(Cut.id.in_(wanted))).all()}  # type: ignore[union-attr]
        if wanted
        else {}
    )
    return [
        _render_out(r, sequences.get(r.sequence_id), cuts.get(r.cut_id or 0, ""))
        for r in renders
    ]


@router.delete("/renders/{render_id}")
def delete_render(render_id: int, session: Session = Depends(get_session)) -> dict:
    render = session.get(Render, render_id)
    if render is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown render")
    removed = _purge_render(session, render)
    session.commit()
    return {"deleted": render_id, "files_removed": removed}


# --------------------------------------------------------------------------- #
# Colour grading
# --------------------------------------------------------------------------- #

@router.get("/grades", response_model=list[schemas.GradeOut])
def list_grades(session: Session = Depends(get_session)) -> list[schemas.GradeOut]:
    grades = session.exec(select(Grade).order_by(Grade.id.desc())).all()  # type: ignore[union-attr]
    return [_grade_out(session, g) for g in grades]


@router.get("/renders/{render_id}/grades", response_model=list[schemas.GradeOut])
async def list_render_grades(
    render_id: int, session: Session = Depends(get_session)
) -> list[schemas.GradeOut]:
    """Every grade on one clip, oldest first, and nothing created.

    A grade exists because someone made it. The clip's own measurement is taken here,
    on the way in, since this is what opening a clip asks for.
    """
    render = _get_render(session, render_id)
    await run_in_threadpool(_analyse_render, session, render)
    grades = session.exec(
        select(Grade).where(Grade.render_id == render.id).order_by(Grade.id)  # type: ignore[arg-type]
    ).all()
    return [_grade_out(session, g) for g in grades]


@router.post("/renders/{render_id}/grades", response_model=schemas.GradeOut)
def put_grade(
    render_id: int,
    payload: schemas.GradeIn,
    session: Session = Depends(get_session),
) -> schemas.GradeOut:
    """Make sure this clip carries a grade of that name, holding those numbers.

    Addressed by name rather than by id, which is what makes copying a look onto
    twenty clips idempotent: each target ends up with one grade of that name, whatever
    else it already holds. Without a name it is a new "Grade N", which is the "+" on
    the profile row.

    No analysis here: copying a look creates grades on clips nobody has opened, and
    what needs the measurement is opening one, which asks for the list.
    """
    render = _get_render(session, render_id)
    label = payload.label.strip() or _free_grade_label(session, render)
    params = grading_service.merge_params(payload.params or {})

    grade = session.exec(
        select(Grade).where(Grade.render_id == render.id, Grade.label == label)
    ).first()
    if grade is None:
        grade = Grade(
            render_id=render.id,  # type: ignore[arg-type]
            label=label,
            params=params,
            params_hash=grading_service.params_hash(params),
            state=GradeState.DRAFT,
        )
        session.add(grade)
        session.commit()
        session.refresh(grade)
    else:
        grade = _write_params(session, grade, params)
    return _grade_out(session, grade)


@router.get("/grades/{grade_id}", response_model=schemas.GradeOut)
def get_grade(grade_id: int, session: Session = Depends(get_session)) -> schemas.GradeOut:
    return _grade_out(session, _get_grade(session, grade_id))


@router.put("/grades/{grade_id}", response_model=schemas.GradeOut)
def save_grade(
    grade_id: int,
    payload: schemas.GradeIn,
    session: Session = Depends(get_session),
) -> schemas.GradeOut:
    """The look, the name, or both. What is not sent is not touched."""
    grade = _get_grade(session, grade_id)
    label = payload.label.strip()
    if label and label != grade.label:
        clash = session.exec(
            select(Grade).where(Grade.render_id == grade.render_id, Grade.label == label)
        ).first()
        if clash is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "this clip already has a grade with that name"
            )
        grade.label = label
        session.add(grade)
        session.commit()
    if payload.params is not None:
        grade = _write_params(session, grade, payload.params)
    return _grade_out(session, grade)


@router.post("/grades/{grade_id}/apply", response_model=schemas.GradeOut)
def apply_grade(grade_id: int, session: Session = Depends(get_session)) -> schemas.GradeOut:
    grade = _get_grade(session, grade_id)
    render = session.get(Render, grade.render_id)
    if render is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown render")
    if render.state != RenderState.DONE:
        raise HTTPException(status.HTTP_409_CONFLICT, "the clip is not stabilized yet")
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


# --------------------------------------------------------------------------- #
# Looks
# --------------------------------------------------------------------------- #

def _look_out(look: Look) -> schemas.LookOut:
    return schemas.LookOut(
        id=look.id or 0,
        label=look.label,
        params=grading_service.travelling(look.params),
        created_at=look.created_at,
    )


@router.get("/looks", response_model=list[schemas.LookOut])
def list_looks(session: Session = Depends(get_session)) -> list[schemas.LookOut]:
    looks = session.exec(select(Look).order_by(Look.label)).all()  # type: ignore[arg-type]
    return [_look_out(look) for look in looks]


@router.post("/looks", response_model=schemas.LookOut, status_code=status.HTTP_201_CREATED)
def create_look(payload: schemas.LookIn, session: Session = Depends(get_session)) -> schemas.LookOut:
    label = payload.label.strip()
    if not label:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a look needs a name")
    # Only the settings that travel are stored, whatever the caller sent: a look is
    # applied to clips whose own range it knows nothing about.
    look = Look(label=label, params=grading_service.travelling(payload.params))
    session.add(look)
    session.commit()
    session.refresh(look)
    return _look_out(look)


@router.patch("/looks/{look_id}", response_model=schemas.LookOut)
def update_look(
    look_id: int, payload: schemas.LookPatch, session: Session = Depends(get_session)
) -> schemas.LookOut:
    look = session.get(Look, look_id)
    if look is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown look")
    if payload.label is not None:
        if not payload.label.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a look needs a name")
        look.label = payload.label.strip()
    if payload.params is not None:
        look.params = grading_service.travelling(payload.params)
    session.add(look)
    session.commit()
    session.refresh(look)
    return _look_out(look)


@router.delete("/looks/{look_id}")
def delete_look(look_id: int, session: Session = Depends(get_session)) -> dict:
    look = session.get(Look, look_id)
    if look is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown look")
    # Nothing hangs off a look: applying one copies its numbers into a grade, so the
    # clips that wear it keep wearing it.
    session.delete(look)
    session.commit()
    return {"deleted": look_id}


@router.delete("/grades/{grade_id}/file")
def delete_graded_file(grade_id: int, session: Session = Depends(get_session)) -> dict:
    """Throw away what a grade produced, and keep the grade.

    Two different gestures now that a grade is a named node: this one frees a hundred
    megabytes and leaves the look ready to be encoded again, while deleting the grade
    itself takes the look with it.
    """
    grade = _get_grade(session, grade_id)
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


@router.delete("/grades/{grade_id}")
def delete_grade(grade_id: int, session: Session = Depends(get_session)) -> dict:
    """The grade, its file and its job. A level of the tree deletes like every other."""
    grade = _get_grade(session, grade_id)
    graded = to_absolute(grade.out_path)
    if graded and graded.exists():
        graded.unlink(missing_ok=True)
    for job in session.exec(select(Job).where(Job.grade_id == grade.id)).all():
        session.delete(job)
    session.delete(grade)
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
    if path is None or grade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "graded file not found")
    render = session.get(Render, grade.render_id)
    name = _render_name(session, render, path, graded=True) if render else path.name
    return media.serve_file(path, request, download_name=name)


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
    names = _job_names(session, jobs)
    return [_job_out(j, names.get(j.id or 0)) for j in jobs]


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
                    names = _job_names(session, jobs)
                    return [
                        _job_out(j, names.get(j.id or 0)).model_dump(mode="json") for j in jobs
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
    sequences that predate the feature, a dozen seconds, once. A chart written by
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


_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(part: str) -> str:
    """A label as a filename fragment: ASCII, lowercase, nothing to quote.

    Capped because three labels and an extension have to fit in a filename, and a
    label is free text.
    """
    folded = unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode().lower()
    return _NOT_SLUG.sub("_", folded).strip("_")[:60]


def _pretty_name(parts: Iterable[str], suffix: str) -> str:
    """The name a downloaded file takes: what the interface calls it, slugified.

    On disk a render is `DJI_20260711191722_0025_D__h_1080__c00.mp4`, which is right
    where it lives (the worker cache addresses files by path, and the parts have to be
    unambiguous) and wrong in a downloads folder. Here it is the rush, the sequence and
    the profile, the same three words the row above the file says, in the same shape
    the volume already uses: `__` between the fields, `_` inside one.
    """
    return "__".join(slug for slug in (_slug(part) for part in parts) if slug) + suffix.lower()


def _render_name(session: Session, render: Render, path: Path, graded: bool = False) -> str:
    seq = session.get(Sequence, render.sequence_id)
    cut = session.get(Cut, render.cut_id) if render.cut_id else None
    try:
        profile = gyroflow_service.get_template(render.template).label
    except gyroflow_service.GyroflowError:
        profile = render.template  # a profile deleted since: its id is all that is left
    return _pretty_name(
        [
            seq.label if seq else "",
            cut.label if cut else "",
            profile,
            "graded" if graded else "",
        ],
        path.suffix,
    )


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
    if path is None or render is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "render file not found")
    return media.serve_file(path, request, download_name=_render_name(session, render, path))
