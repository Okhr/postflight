"""Saving a look, when there is no save button.

The colour page writes on every slider release, so the write is the frequent event
here and it has to be exact: a look that moved invalidates the file being encoded,
and a write that changes nothing must not touch anything.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.api import routes, schemas
from app.models import Grade, GradeState, Job, JobKind, JobState, Render, RenderState, Sequence


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
    return routes.save_grade(
        render.id,
        schemas.GradeParamsIn(params={"exposure": 0.0, "contrast": 1.0, **params}),
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
