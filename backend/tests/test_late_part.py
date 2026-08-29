"""A part that arrives after its flight was already merged.

Before this, such a part became a rush of its own and stayed one: the docstring
said "to be joined by hand", and joining by hand had been removed on 2026-08-20.
Measured on the real collection: two halves of one flight 0.39 s apart, sitting in
two sequences, with no way to put them back together.

What must not regress is the other half of the rule. Rebuilding renumbers frames,
so a rush somebody has already derushed or rendered is left alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app import pipeline
from app.config import settings
from app.models import Clip, Cut, Render, RenderState, Sequence, SequenceState
from app.framing import duration_to_frames
from app.services.naming import parse_filename
from app.services.probe import ProbeResult

BASE = datetime(2026, 7, 11, 19, 17, 22, tzinfo=timezone.utc)
DURATION_MS = 222_639.0
FIRST = "DJI_20260711191722_0025_D.MP4"
SECOND = "DJI_20260711192105_0026_D.MP4"  # starts 0.39 s after the first ends


def _probe(**over) -> ProbeResult:
    fields = dict(
        duration_ms=DURATION_MS, width=3840, height=2880, fps_num=60000, fps_den=1001,
        codec="hevc", size_bytes=1024, has_gyro=True, recorded_at=BASE,
    )
    fields.update(over)
    return ProbeResult(**fields)


def _drop(name: str) -> None:
    path = settings.inbox_dir / name
    path.write_bytes(name.encode())  # distinct bytes: distinct fingerprints
    pipeline.mark_upload_complete(path)


def _ingest(session: Session, monkeypatch, name: str) -> None:
    """One scan. The probe answers with the time the DJI name carries, which is what
    the real one does when the container has none."""
    def probe(path):
        return _probe(recorded_at=parse_filename(path.name).recorded_at)
    monkeypatch.setattr(pipeline, "probe", probe)
    _drop(name)
    pipeline.ingest_and_group(session)


def _merge_it(session: Session, seq: Sequence) -> None:
    """Pretend the worker did the merge, so the sequence leaves NEW."""
    seq.state = SequenceState.READY
    seq.merged_path = f"merged/{seq.artifact_stem}.mp4"
    session.add(seq)
    session.commit()


def sequences(session: Session) -> list[Sequence]:
    return list(session.exec(select(Sequence).order_by(Sequence.id)).all())  # type: ignore[arg-type]


def test_a_late_part_joins_the_rush_it_continues(session: Session, monkeypatch):
    _ingest(session, monkeypatch, FIRST)
    first = sequences(session)[0]
    _merge_it(session, first)
    assert first.part_count == 1

    _ingest(session, monkeypatch, SECOND)

    rushes = sequences(session)
    assert len(rushes) == 1, [s.key for s in rushes]
    assert rushes[0].part_count == 2
    parts = session.exec(
        select(Clip).where(Clip.sequence_id == rushes[0].id).order_by(Clip.part_index)  # type: ignore[arg-type]
    ).all()
    assert [c.filename for c in parts] == [FIRST, SECOND]


def test_the_rebuilt_rush_is_queued_to_merge_again(session: Session, monkeypatch):
    """It leaves READY for NEW: the file on disk is one part and the rush is two."""
    _ingest(session, monkeypatch, FIRST)
    _merge_it(session, sequences(session)[0])

    _ingest(session, monkeypatch, SECOND)

    assert sequences(session)[0].state == SequenceState.NEW


def test_a_rush_carrying_cuts_takes_the_late_part_too(session: Session, monkeypatch):
    """The guard used to be "leave it alone", which meant a derushed rush could never
    be repaired. Now the marks are adapted instead."""
    _ingest(session, monkeypatch, FIRST)
    first = sequences(session)[0]
    _merge_it(session, first)
    session.add(Cut(sequence_id=first.id, label="dive", start_frame=100, end_frame=200))
    session.commit()

    _ingest(session, monkeypatch, SECOND)

    rushes = sequences(session)
    assert len(rushes) == 1 and rushes[0].part_count == 2
    cuts = session.exec(select(Cut).where(Cut.sequence_id == rushes[0].id)).all()
    assert len(cuts) == 1, "the cut followed its rush instead of dying with the row"
    # Appended at the end, so the frames already there keep their numbers.
    assert (cuts[0].start_frame, cuts[0].end_frame) == (100, 200)


def test_a_part_landing_in_front_shifts_the_marks(session: Session, monkeypatch):
    """The case that makes the rule worth having: everything already there moves back
    by the length of what was inserted, and a mark is a frame number."""
    _ingest(session, monkeypatch, SECOND)
    second = sequences(session)[0]
    _merge_it(session, second)
    session.add(Cut(sequence_id=second.id, label="dive", start_frame=100, end_frame=200))
    session.commit()

    _ingest(session, monkeypatch, FIRST)  # the part that comes before it

    rushes = sequences(session)
    assert len(rushes) == 1 and rushes[0].part_count == 2
    frames_ahead = duration_to_frames(DURATION_MS, 60000, 1001)
    cuts = session.exec(select(Cut).where(Cut.sequence_id == rushes[0].id)).all()
    assert (cuts[0].start_frame, cuts[0].end_frame) == (100 + frames_ahead, 200 + frames_ahead)


def test_a_rush_keeps_its_id_when_it_is_reopened(session: Session, monkeypatch):
    """What everything hanging off it depends on: a cut, a render, a URL somebody
    left open."""
    _ingest(session, monkeypatch, FIRST)
    first = sequences(session)[0]
    before = first.id
    _merge_it(session, first)
    session.add(Render(sequence_id=first.id, template="h_1080", state=RenderState.DONE))
    session.commit()

    _ingest(session, monkeypatch, SECOND)

    rushes = sequences(session)
    assert [s.id for s in rushes] == [before]
    renders = session.exec(select(Render).where(Render.sequence_id == before)).all()
    assert len(renders) == 1, "the render survived, and still points at the same footage"


def test_an_unrelated_rush_is_not_reopened(session: Session, monkeypatch):
    """Only a sequence a free clip actually touches: pulling in every one of them
    would renumber and re-merge the whole library on every scan."""
    _ingest(session, monkeypatch, FIRST)
    first = sequences(session)[0]
    _merge_it(session, first)
    before = (first.id, first.key, first.part_count)

    # Another flight entirely: consecutive index, but hours later.
    far = "DJI_20260711221722_0026_D.MP4"
    _ingest(session, monkeypatch, far)

    rushes = sequences(session)
    assert len(rushes) == 2
    kept = session.get(Sequence, before[0])
    assert kept is not None and (kept.key, kept.part_count) == (before[1], before[2])
    assert kept.state == SequenceState.READY, "it was never torn down"


def _artifacts(seq: Sequence) -> list:
    """Every file derived from a rush, whatever the step that wrote it."""
    stem = seq.artifact_stem
    return sorted(
        list(settings.merged_dir.glob(f"{stem}.*")) + list(settings.proxies_dir.glob(f"{stem}.*"))
    )


def test_the_old_merge_and_proxy_go_when_the_content_changes(session: Session, monkeypatch):
    """A rebuilt rush is named after its new content hash, so the files of the old
    one are addressed by nobody. Left behind they are pure loss: a four minute proxy
    is ~90 MB, and the merged file of a multi-part rush is gigabytes."""
    _ingest(session, monkeypatch, FIRST)
    first = sequences(session)[0]
    _merge_it(session, first)
    # The string, not the object: the row is updated in place, so reading the stem
    # off it afterwards gives the new hash and compares it to itself.
    stem = first.artifact_stem
    old = []
    for path in (
        settings.merged_dir / f"{stem}.mp4",
        settings.proxies_dir / f"{stem}.mp4",
        settings.proxies_dir / f"{stem}.poster.jpg",
        settings.proxies_dir / f"{stem}.gyro.json",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"produced from one part")
        old.append(path)

    _ingest(session, monkeypatch, SECOND)

    rebuilt = sequences(session)[0]
    assert rebuilt.artifact_stem != stem, "the content changed, so the name does"
    assert [p for p in old if p.exists()] == [], "the files of the old content are orphans"


def test_files_stay_when_the_regrouping_changes_nothing(session: Session, monkeypatch):
    """The other half: naming files after the hash is what lets a rush torn down and
    rebuilt pick its own merge back up instead of redoing it."""
    _ingest(session, monkeypatch, FIRST)
    first = sequences(session)[0]
    _merge_it(session, first)
    kept = settings.proxies_dir / f"{first.artifact_stem}.mp4"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"still the same content")

    pipeline.group_clips_into_sequences(session)  # nothing new to place

    assert kept.exists()


def test_a_part_landing_in_front_renames_the_rush(session: Session, monkeypatch):
    """Seen in production on 2026-08-29: the row is updated in place, so it kept the
    name of what used to be its first part. Every produced file is named after that
    key, so a rush made of 0025 and 0026 was writing files called 0026."""
    _ingest(session, monkeypatch, SECOND)
    second = sequences(session)[0]
    _merge_it(session, second)
    assert second.key == "DJI_20260711192105_0026_D"

    _ingest(session, monkeypatch, FIRST)

    rush = sequences(session)[0]
    assert rush.key == "DJI_20260711191722_0025_D"
    assert rush.label == "DJI_20260711191722_0025_D"


def test_a_name_somebody_typed_survives_the_rebuild(session: Session, monkeypatch):
    """The key is ours, the label is theirs."""
    _ingest(session, monkeypatch, SECOND)
    second = sequences(session)[0]
    second.label = "Sunset dive"
    _merge_it(session, second)

    _ingest(session, monkeypatch, FIRST)

    rush = sequences(session)[0]
    assert rush.key == "DJI_20260711191722_0025_D"
    assert rush.label == "Sunset dive"
