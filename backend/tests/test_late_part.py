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


def test_a_rush_carrying_cuts_is_left_alone(session: Session, monkeypatch):
    """The frame numbers of a cut only mean something against the merged file, so
    rebuilding would move them under it."""
    _ingest(session, monkeypatch, FIRST)
    first = sequences(session)[0]
    _merge_it(session, first)
    session.add(Cut(sequence_id=first.id, label="dive", start_frame=100, end_frame=200))
    session.commit()

    _ingest(session, monkeypatch, SECOND)

    rushes = sequences(session)
    assert len(rushes) == 2, "the late part must form its own rush instead"
    assert rushes[0].part_count == 1


def test_a_rush_carrying_a_render_is_left_alone(session: Session, monkeypatch):
    _ingest(session, monkeypatch, FIRST)
    first = sequences(session)[0]
    _merge_it(session, first)
    session.add(Render(sequence_id=first.id, template="h_1080", state=RenderState.DONE))
    session.commit()

    _ingest(session, monkeypatch, SECOND)

    assert len(sequences(session)) == 2


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
