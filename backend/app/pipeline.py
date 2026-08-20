"""Ingestion from `inbox/` and grouping into sequences."""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from .config import settings
from .framing import duration_to_frames
from .models import Clip, ClipState, Job, JobKind, JobState, Sequence, SequenceState, utcnow
from .services.grouping import ClipInfo, chain_clips, describe_group, sequence_hash
from .services.naming import parse_filename, sequence_key
from .paths import to_relative
from .services.probe import ProbeError, fingerprint, probe
from .timeutil import as_utc

log = logging.getLogger(__name__)

# Suffixes of files still being written, skipped while scanning.
_IGNORED_SUFFIXES = (".partial", ".tmp", ".part", ".crdownload")
_DUPLICATES_DIRNAME = ".duplicates"
_STABILIZED_DIRNAME = ".stabilized"
# Files set aside rather than deleted, so nothing is ever lost silently. Scanning
# must skip them, or every pass would pick them straight back up.
_SIDELINED_DIRNAMES = (_DUPLICATES_DIRNAME, _STABILIZED_DIRNAME)

# Gyroflow names its output after the source plus `_stabilized`, with the aspect
# sometimes appended: `_stabilized`, `_stabilized_16x9`, `_joined_stabilized`. A
# substring is enough and it is all we look at. Reading the gyro track would be
# stronger, but these files are not supposed to land in the inbox in the first place,
# and a name check does not cost a probe.
_STABILIZED_MARKER = "stabilized"

# Manual scan: minimum file age, and delay before re-checking the size.
QUIESCENT_MIN_AGE_S = 2.0
QUIESCENT_RECHECK_S = 1.0


@dataclass
class ScanResult:
    """What a scan produced, so the UI can report it back."""

    ingested: list["Clip"] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    # Set aside for want of a gyro track: almost always a stabilized output that
    # found its way back into the inbox.
    rejected: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    sequences: list["Sequence"] = field(default_factory=list)


# Sizes seen on the previous scan: a file is only ingested after
# `stability_checks` consecutive scans at an identical size. inotify is not
# reliable on NFS/SMB, and a 4 GB rush takes a while to copy over.
_seen_sizes: dict[str, tuple[int, int]] = {}

# Files dropped by the upload endpoint: complete by construction.
_completed_uploads: set[str] = set()

# Uploads still streaming in. The scheduled scan must not ingest while one is on the
# wire: the file arriving may be the next part of a flight whose first part has already
# landed, and ingesting that first part alone merges it (a lone part is a hardlink, so
# it is done in 0.3 s) and locks the second one out of its sequence. Measured on
# 2026-08-20: two parts 0.36 s apart became two sequences because the 30 s scan fell
# between their uploads, 13:29:15 against an upload that finished at 13:29:20.
_uploads_in_flight = 0
_last_upload_at = 0.0

# How long the inbox is left alone after the last upload finished. An uploader sends
# its files one after another and checks each for duplicates in between, so there is a
# gap of a few hundred milliseconds where nothing is on the wire and the batch is not
# over. Holding off through it costs nothing: the uploader triggers its own scan as
# soon as it is really done.
UPLOAD_SETTLE_S = 10.0


def _candidate_files() -> list[Path]:
    if not settings.inbox_dir.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(settings.inbox_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(name in path.parts for name in _SIDELINED_DIRNAMES):
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in settings.extensions:
            continue
        if any(path.name.lower().endswith(s) for s in _IGNORED_SUFFIXES):
            continue
        files.append(path)
    return files


def mark_upload_complete(path: Path) -> None:
    """Flag a file received by the API, hence complete by construction.

    The stability checks exist for copies made *outside* the application (SMB,
    rsync), where nothing announces that a file has finished arriving. The upload
    endpoint, on the other hand, writes to `.partial` and only renames at the end:
    waiting two more seconds would prove nothing and would only make the scan
    fired right after it come up empty.
    """
    _completed_uploads.add(str(path))


def upload_started() -> None:
    global _uploads_in_flight
    _uploads_in_flight += 1


def upload_finished() -> None:
    """Always call this, including when the upload failed or the client vanished.

    A leaked count would silence the scheduled scan for as long as the process lives.
    """
    global _uploads_in_flight, _last_upload_at
    _uploads_in_flight = max(0, _uploads_in_flight - 1)
    _last_upload_at = time.monotonic()


def uploads_in_flight() -> int:
    return _uploads_in_flight


def uploading() -> bool:
    """Is a batch of uploads still going on, counting the gaps between its files?"""
    if _uploads_in_flight:
        return True
    return bool(_last_upload_at) and time.monotonic() - _last_upload_at < UPLOAD_SETTLE_S


def _is_stable(path: Path) -> bool:
    key = str(path)
    if key in _completed_uploads:
        return True
    try:
        size = path.stat().st_size
    except OSError:
        _seen_sizes.pop(key, None)
        return False
    if size == 0:
        return False
    previous_size, count = _seen_sizes.get(key, (-1, 0))
    if size == previous_size:
        count += 1
    else:
        count = 0
    _seen_sizes[key] = (size, count)
    return count >= settings.stability_checks


def _is_quiescent(path: Path) -> bool:
    """Variant used by a manually triggered scan.

    The `_is_stable` counter only advances from one scan to the next: applied to a
    manual scan, you would have to click several times for anything to happen. So
    we check on the spot, with two reads spaced apart.
    """
    if str(path) in _completed_uploads:
        return True
    try:
        first = path.stat()
    except OSError:
        return False
    if first.st_size == 0:
        return False
    if time.time() - first.st_mtime < QUIESCENT_MIN_AGE_S:
        return False
    time.sleep(QUIESCENT_RECHECK_S)
    try:
        return path.stat().st_size == first.st_size
    except OSError:
        return False


def _forget(path: Path) -> None:
    key = str(path)
    _seen_sizes.pop(key, None)
    _completed_uploads.discard(key)


def unique_destination(directory: Path, filename: str) -> Path:
    dest = directory / filename
    if not dest.exists():
        return dest
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for n in range(1, 1000):
        candidate = directory / f"{stem}__{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free name for {filename} in {directory}")


def _move(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(dest)  # instant when inbox/ and raw/ share a filesystem
    except OSError:
        shutil.move(str(source), str(dest))


def scan_inbox(session: Session, immediate: bool = False) -> ScanResult:
    """Ingest the stable files from `inbox/` into `raw/`.

    `immediate` serves the scan triggered from the UI: nobody wants to wait
    several cycles before seeing anything happen.
    """
    result = ScanResult()
    if not immediate and uploading():
        # Let the batch land whole. A scan asked for by hand is never held back: it is
        # an explicit "now", and it runs after the uploader's own transfers are done.
        log.debug("scan held back: %d upload(s) in flight", _uploads_in_flight)
        return result

    is_ready = _is_quiescent if immediate else _is_stable
    for path in _candidate_files():
        if not is_ready(path):
            log.debug("not stable yet: %s", path.name)
            continue

        if _STABILIZED_MARKER in path.stem.lower():
            # A Gyroflow output back in the inbox. Not a rush: nothing downstream can
            # stabilize it a second time, and ingesting it would cost a full proxy
            # encode before failing. Set aside, never deleted.
            dest = unique_destination(settings.inbox_dir / _STABILIZED_DIRNAME, path.name)
            _move(path, dest)
            _forget(path)
            result.rejected.append(path.name)
            log.info("%s looks stabilized → set aside in %s/", path.name, _STABILIZED_DIRNAME)
            continue

        try:
            fp = fingerprint(path)
        except OSError as exc:
            log.warning("cannot read %s: %s", path.name, exc)
            continue

        existing = session.exec(select(Clip).where(Clip.fingerprint == fp)).first()
        if existing is not None:
            duplicates = settings.inbox_dir / _DUPLICATES_DIRNAME
            dest = unique_destination(duplicates, path.name)
            _move(path, dest)
            _forget(path)
            result.duplicates.append(path.name)
            log.info(
                "Duplicate of %s → moved into %s/", existing.filename, _DUPLICATES_DIRNAME
            )
            continue

        try:
            info = probe(path)
        except (ProbeError, OSError) as exc:
            log.error("probe failed for %s: %s", path.name, exc)
            clip = Clip(
                filename=path.name, fingerprint=fp, state=ClipState.FAILED,
                error=str(exc)[:500],
            )
            session.add(clip)
            session.commit()
            result.failed.append(path.name)
            continue

        if info.recorded_at is None:
            # Neither the name nor the container gives a start time, and there is no
            # third source worth trusting: the mtime used to fill in here and it lied
            # every time the file had been copied. Without a start time the parts of a
            # flight cannot be told apart from two separate flights, so refuse the
            # file and say so rather than group it wrongly and silently.
            reason = (
                "no reliable start time: the name carries no timestamp and the file "
                "has no creation_time"
            )
            log.error("%s refused: %s", path.name, reason)
            session.add(
                Clip(filename=path.name, fingerprint=fp, state=ClipState.FAILED, error=reason)
            )
            session.commit()
            result.failed.append(path.name)
            continue

        raw_dest = unique_destination(settings.raw_dir, path.name)
        _move(path, raw_dest)
        _forget(path)

        parsed = parse_filename(raw_dest)
        clip = Clip(
            filename=raw_dest.name,
            raw_path=to_relative(raw_dest),
            size_bytes=info.size_bytes,
            fingerprint=fp,
            duration_ms=info.duration_ms,
            width=info.width,
            height=info.height,
            fps_num=info.fps_num,
            fps_den=info.fps_den,
            codec=info.codec,
            has_gyro=info.has_gyro,
            recorded_at=info.recorded_at,
            camera_index=parsed.camera_index,
            state=ClipState.INGESTED,
        )
        session.add(clip)
        session.commit()
        session.refresh(clip)
        result.ingested.append(clip)
        log.info(
            "Ingested %s (%.1fs, %dx%d, gyro=%s)",
            clip.filename, clip.duration_ms / 1000, clip.width, clip.height, clip.has_gyro,
        )

    return result


def _clip_info(clip: Clip) -> ClipInfo:
    parsed = parse_filename(clip.filename)
    return ClipInfo(
        id=clip.id or 0,
        filename=clip.filename,
        recorded_at=as_utc(clip.recorded_at),  # type: ignore[arg-type]
        duration_ms=clip.duration_ms,
        width=clip.width,
        height=clip.height,
        fps_num=clip.fps_num,
        fps_den=clip.fps_den,
        codec=clip.codec,
        camera_index=clip.camera_index,
        group_key=parsed.group_key,
    )


def group_clips_into_sequences(session: Session) -> list[Sequence]:
    """Build sequences out of the free clips.

    Sequences still in `NEW` are torn down and rebuilt: nothing has been produced
    from them yet, and a late-arriving part must be able to join its group. A
    sequence already merged is never touched automatically: the late part will
    form its own sequence, to be joined by hand if needed.
    """
    unassigned = session.exec(
        select(Clip).where(Clip.sequence_id.is_(None), Clip.state == ClipState.INGESTED)  # type: ignore[union-attr]
    ).all()
    if not unassigned:
        # Nothing new to place: above all, do not tear down the pending groups.
        # Otherwise, while the worker is busy elsewhere, every scanner tick
        # renumbers them and stacks one more merge, and the queue grows forever.
        return []

    # A merge already running is not pulled out from under the worker.
    busy = {
        job.sequence_id
        for job in session.exec(
            select(Job).where(Job.state == JobState.RUNNING)
        ).all()
        if job.sequence_id
    }
    new_sequences = [
        seq
        for seq in session.exec(select(Sequence).where(Sequence.state == SequenceState.NEW)).all()
        if seq.id not in busy
    ]
    candidates = list(unassigned)
    for seq in new_sequences:
        # The sequence is about to be rebuilt: its queued merge would be orphaned.
        for job in session.exec(
            select(Job).where(Job.sequence_id == seq.id, Job.state == JobState.QUEUED)
        ).all():
            session.delete(job)
        clips = session.exec(select(Clip).where(Clip.sequence_id == seq.id)).all()
        candidates.extend(clips)
        for clip in clips:
            clip.sequence_id = None
            session.add(clip)
        session.delete(seq)
    if new_sequences:
        session.commit()

    candidates = [c for c in candidates if c.recorded_at is not None]
    if not candidates:
        return []

    created: list[Sequence] = []
    for group in chain_clips(
        [_clip_info(c) for c in candidates],
        settings.split_gap_tolerance_s,
    ):
        clips = [session.get(Clip, info.id) for info in group]
        clips = [c for c in clips if c is not None]
        if not clips:
            continue
        first = clips[0]
        key = sequence_key(first.filename)

        if session.exec(select(Sequence).where(Sequence.key == key)).first() is not None:
            key = f"{key}__{first.id}"

        seq = Sequence(
            key=key,
            label=key,
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

        adopt_existing_artifacts(session, seq)
        created.append(seq)
        log.info("Sequence %s: %s", seq.key, describe_group(group))

    return created


def adopt_existing_artifacts(session: Session, sequence: Sequence) -> bool:
    """Pick up what a previous run already produced for this content hash.

    The point of naming files after the hash: a sequence torn down and rebuilt (a
    late part, a manual join, a deletion followed by a rescan) finds its merged
    file and its proxy still on disk and skips straight to the end. Merging 4 GB
    and encoding a proxy at 0.6x realtime is not worth redoing for bytes that are
    already there.

    Returns True if anything was adopted.
    """
    merged = settings.merged_dir / f"{sequence.artifact_stem}.mp4"
    if not merged.exists():
        return False

    try:
        info = probe(merged)
    except (ProbeError, OSError) as exc:
        # A truncated leftover: better to redo it than to trust it.
        log.warning("Existing merge unusable for %s: %s", sequence.key, exc)
        return False

    sequence.merged_path = to_relative(merged)
    sequence.width = info.width
    sequence.height = info.height
    sequence.fps_num = info.fps_num
    sequence.fps_den = info.fps_den
    sequence.duration_ms = info.duration_ms
    sequence.frame_count = duration_to_frames(info.duration_ms, info.fps_num, info.fps_den)
    sequence.size_bytes = info.size_bytes
    sequence.state = SequenceState.MERGED
    for clip in session.exec(select(Clip).where(Clip.sequence_id == sequence.id)).all():
        clip.state = ClipState.MERGED
        session.add(clip)

    reused = "merge"
    proxy = settings.proxies_dir / f"{sequence.artifact_stem}.mp4"
    if proxy.exists():
        try:
            proxy_info = probe(proxy)
        except (ProbeError, OSError):
            proxy_info = None
        if proxy_info is not None:
            filmstrip = settings.proxies_dir / f"{sequence.artifact_stem}.filmstrip.jpg"
            sequence.proxy_path = to_relative(proxy)
            sequence.proxy_width = proxy_info.width
            sequence.proxy_height = proxy_info.height
            sequence.filmstrip_path = to_relative(filmstrip) if filmstrip.exists() else None
            sequence.state = SequenceState.READY
            reused = "merge + proxy"

    sequence.updated_at = utcnow()
    session.add(sequence)
    session.commit()
    log.info("Sequence %s: reused existing %s (hash %s)", sequence.key, reused, sequence.content_hash)
    return True


def enqueue_pending(session: Session, sequence: Sequence) -> Job | None:
    """Queue only the step the sequence is actually missing."""
    if sequence.state == SequenceState.READY:
        return None
    if sequence.state == SequenceState.MERGED:
        return enqueue_proxy(session, sequence)
    return enqueue_merge(session, sequence)


# Lower number runs first. The proxy of an already-merged sequence outranks the
# merge of the next one, on purpose: a rush becomes workable as soon as its *own*
# proxy exists, instead of waiting for the whole batch to be merged first. On
# eight rushes that is the difference between one usable in a few minutes and one
# usable in an hour.
PRIORITY_MANUAL = 1
PRIORITY_PROXY = 5
PRIORITY_MERGE = 10


def _enqueue(session: Session, sequence: Sequence, kind: JobKind, priority: int) -> Job:
    """Queue one step for a sequence, at most once.

    Two jobs of the same kind on the same sequence would do the same work twice,
    the second one on top of the output of the first. It happens easily: a click
    on "run pending" while the scanner has already queued the merge.
    """
    existing = session.exec(
        select(Job).where(
            Job.sequence_id == sequence.id,
            Job.kind == kind,
            Job.state.in_([JobState.QUEUED, JobState.RUNNING]),  # type: ignore[union-attr]
        )
    ).first()
    if existing is not None:
        if existing.state == JobState.QUEUED and priority < existing.priority:
            existing.priority = priority  # a manual retry jumps the line
            session.add(existing)
            session.commit()
        return existing

    job = Job(
        kind=kind,
        state=JobState.QUEUED,
        priority=priority,
        sequence_id=sequence.id,
        payload={"sequence_id": sequence.id},
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def enqueue_merge(session: Session, sequence: Sequence, priority: int = PRIORITY_MERGE) -> Job:
    return _enqueue(session, sequence, JobKind.MERGE, priority)


def enqueue_proxy(session: Session, sequence: Sequence, priority: int = PRIORITY_PROXY) -> Job:
    return _enqueue(session, sequence, JobKind.PROXY, priority)


def ingest_and_group(session: Session, immediate: bool = False) -> ScanResult:
    result = scan_inbox(session, immediate=immediate)
    result.sequences = group_clips_into_sequences(session)
    for seq in result.sequences:
        enqueue_pending(session, seq)
    if result.ingested or result.sequences or result.duplicates:
        log.info(
            "Scan: %d clip(s) ingested, %d duplicate(s), %d sequence(s) created",
            len(result.ingested), len(result.duplicates), len(result.sequences),
        )
    return result


def sequence_marked_updated(session: Session, sequence: Sequence) -> None:
    sequence.updated_at = utcnow()
    session.add(sequence)
    session.commit()
