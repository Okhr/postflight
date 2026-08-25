"""Saving a look, when there is no save button.

The colour page writes on every slider release, so the write is the frequent event
here and it has to be exact: a look that moved invalidates the file being encoded,
and a write that changes nothing must not touch anything.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app import dispatch
from app.api import routes, schemas
from app.config import settings
from app.models import Grade, GradeState, Job, JobKind, JobState, Render, RenderState, Sequence
from app.paths import to_relative


def _clip(session: Session, seq: Sequence) -> Render:
    render = Render(
        sequence_id=seq.id,  # type: ignore[arg-type]
        template="h_1080",
        state=RenderState.DONE,
        out_path="out/clip.mp4",
    )
    session.add(render)
    session.commit()
    session.refresh(render)
    return render


def _save(session: Session, render: Render, **params) -> schemas.GradeOut:
    """Write a look on this clip's grade, creating it on first call.

    Addressed by name, which is the route the page uses to put a grade on a clip: one
    name, one grade, however many times it is called.
    """
    return routes.put_grade(
        render.id,
        schemas.GradeIn(label="Look", params={"exposure": 0.0, "contrast": 1.0, **params}),
        session=session,
    )


def _encoding(session: Session, render: Render, state: GradeState) -> Grade:
    grade = session.exec(select(Grade).where(Grade.render_id == render.id)).one()
    grade.state = state
    session.add(grade)
    session.add(
        Job(
            kind=JobKind.GRADE,
            state=JobState.RUNNING,
            sequence_id=render.sequence_id,
            render_id=render.id,
            grade_id=grade.id,
            payload={},
        )
    )
    session.commit()
    return grade


def test_a_look_that_moves_cancels_the_encode_in_flight(session: Session, sequence: Sequence):
    """The file being written is of the look nobody is looking at any more."""
    render = _clip(session, sequence)
    _save(session, render, exposure=0.5)
    _encoding(session, render, GradeState.RUNNING)

    saved = _save(session, render, exposure=0.9)

    assert saved.state == "draft"
    assert session.exec(select(Job)).all() == []


def test_a_write_that_changes_nothing_leaves_the_encode_alone(
    session: Session, sequence: Sequence
):
    """Releasing a slider on the value it already had is a write like any other, and
    it must not kill a job."""
    render = _clip(session, sequence)
    _save(session, render, exposure=0.5)
    _encoding(session, render, GradeState.RUNNING)

    saved = _save(session, render, exposure=0.5)

    assert saved.state == "running"
    assert len(session.exec(select(Job)).all()) == 1


def test_a_look_that_moves_after_a_file_exists_goes_back_to_draft(
    session: Session, sequence: Sequence
):
    render = _clip(session, sequence)
    _save(session, render, exposure=0.5)
    _encoding(session, render, GradeState.DONE)

    assert _save(session, render, exposure=0.1).state == "draft"


def test_a_new_graded_file_replaces_the_one_it_supersedes(
    session: Session, sequence: Sequence, tmp_path
):
    """A graded file is named after its look, so going back to a look already made is
    free. The price was a hundred megabytes per look ever tried, unreachable from
    anywhere once the row pointed elsewhere: measured at 181 MB on the real volume.
    """
    render = _clip(session, sequence)
    _save(session, render, exposure=0.5)
    grade = session.exec(select(Grade).where(Grade.render_id == render.id)).one()
    old = settings.graded_dir / "clip__oldlook.mp4"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"the look before")
    grade.out_path = to_relative(old)
    grade.state = GradeState.RUNNING
    session.add(grade)
    session.commit()

    new = settings.graded_dir / "clip__newlook.mp4"
    new.write_bytes(b"the look now")
    job = Job(
        kind=JobKind.GRADE,
        state=JobState.RUNNING,
        sequence_id=render.sequence_id,
        render_id=render.id,
        grade_id=grade.id,
        payload={"grade_id": grade.id},
    )
    session.add(job)
    session.commit()

    dispatch._apply_grade(session, job, {"out_path": to_relative(new)})

    assert not old.exists()
    assert new.exists()
    session.refresh(grade)
    assert grade.out_path == to_relative(new)


def test_re_rendering_the_same_look_keeps_its_file(session: Session, sequence: Sequence):
    """The worker reuses the file when the hash matches, so the applier is handed the
    path it already holds. Deleting it there would delete the answer."""
    render = _clip(session, sequence)
    _save(session, render, exposure=0.5)
    grade = session.exec(select(Grade).where(Grade.render_id == render.id)).one()
    same = settings.graded_dir / "clip__samelook.mp4"
    same.parent.mkdir(parents=True, exist_ok=True)
    same.write_bytes(b"unchanged")
    grade.out_path = to_relative(same)
    session.add(grade)
    session.commit()
    job = Job(
        kind=JobKind.GRADE, state=JobState.RUNNING, sequence_id=render.sequence_id,
        render_id=render.id, grade_id=grade.id, payload={"grade_id": grade.id},
    )
    session.add(job)
    session.commit()

    dispatch._apply_grade(session, job, {"out_path": to_relative(same), "reused": True})

    assert same.exists()
