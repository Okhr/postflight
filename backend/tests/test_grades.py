"""Several looks on one clip, side by side.

A grade became a level of the hierarchy on 2026-08-25: rush, sequence, profile, grade.
What that changes is not the tuning, it is the bookkeeping around it. A clip holds as
many grades as one wants, each named, each with its own file; they are addressed by
name so copying a look twenty times is idempotent; and the clip's measurement is taken
once for all of them.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app import dispatch
from app.api import routes, schemas
from app.models import Grade, GradeState, Job, JobKind, JobState, Render, RenderState, Sequence
from app.services import grading as grading_service


def _clip(session: Session, seq: Sequence, template: str = "h_1080") -> Render:
    render = Render(
        sequence_id=seq.id,  # type: ignore[arg-type]
        template=template,
        state=RenderState.DONE,
        out_path="out/clip.mp4",
    )
    session.add(render)
    session.commit()
    session.refresh(render)
    return render


def _put(session: Session, render: Render, label: str = "", **params) -> schemas.GradeOut:
    return routes.put_grade(
        render.id,
        schemas.GradeIn(label=label, params={**params} or None),
        session=session,
    )


def test_a_clip_holds_several_grades(session: Session, sequence: Sequence):
    """The point of the whole change: two looks on one clip, neither replacing the
    other. `grade.render_id` was unique until this, which is what made it impossible."""
    render = _clip(session, sequence)

    warm = _put(session, render, "Golden hour", temperature=7400)
    cold = _put(session, render, "Blue hour", temperature=5200)

    assert warm.id != cold.id
    assert {g.label for g in routes.list_grades(session=session)} == {
        "Golden hour",
        "Blue hour",
    }
    assert warm.params["temperature"] == 7400
    assert cold.params["temperature"] == 5200


def test_a_grade_without_a_name_takes_the_first_free_number(
    session: Session, sequence: Sequence
):
    """What the "+" on a profile row produces. The first free number, not a count: a
    grade deleted in the middle leaves its number available."""
    render = _clip(session, sequence)

    first = _put(session, render)
    second = _put(session, render)
    assert (first.label, second.label) == ("Grade 1", "Grade 2")

    routes.delete_grade(first.id, session=session)
    assert _put(session, render).label == "Grade 1"


def test_writing_by_name_twice_writes_one_grade(session: Session, sequence: Sequence):
    """Copying a look onto a clip that already has it must not pile up duplicates. This
    is the rule that makes "Copy to" safe to press again: one name, one grade."""
    render = _clip(session, sequence)

    _put(session, render, "Golden hour", temperature=7400)
    again = _put(session, render, "Golden hour", temperature=6900)

    grades = session.exec(select(Grade).where(Grade.render_id == render.id)).all()
    assert len(grades) == 1
    assert again.params["temperature"] == 6900


def test_a_look_copied_over_a_done_grade_drops_its_file(
    session: Session, sequence: Sequence
):
    """Writing by name goes through the same guard as a slider release: the file that
    exists is of the old look, so the grade goes back to draft and its job is gone."""
    render = _clip(session, sequence)
    grade = _put(session, render, "Golden hour", temperature=7400)
    row = session.get(Grade, grade.id)
    assert row is not None
    row.state = GradeState.DONE
    row.out_path = "graded/x.mp4"
    session.add(row)
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

    after = _put(session, render, "Golden hour", temperature=6900)

    assert after.state == "draft"
    assert session.exec(select(Job)).all() == []


def test_two_grades_can_be_queued_at_once(session: Session, sequence: Sequence):
    """"Several in parallel" has to mean it: two independent jobs on the same clip,
    writing two different files."""
    render = _clip(session, sequence)
    warm = _put(session, render, "Golden hour", temperature=7400)
    cold = _put(session, render, "Blue hour", temperature=5200)

    routes.apply_grade(warm.id, session=session)
    routes.apply_grade(cold.id, session=session)

    jobs = session.exec(select(Job).where(Job.kind == JobKind.GRADE)).all()
    assert {job.grade_id for job in jobs} == {warm.id, cold.id}

    specs = [dispatch._prepare_grade(session, job) for job in jobs]
    assert len({spec["dest"] for spec in specs}) == 2


def test_the_graded_file_is_named_after_its_grade(session: Session, sequence: Sequence):
    """Two grades set exactly the same would otherwise share one file, and deleting
    either would take it from the other. The hash keeps a look already produced free,
    the id keeps the two apart."""
    render = _clip(session, sequence)
    one = _put(session, render, "One", temperature=7400)
    two = _put(session, render, "Two", temperature=7400)
    assert one.params == two.params

    dests = []
    for grade in (one, two):
        routes.apply_grade(grade.id, session=session)
        job = session.exec(
            select(Job).where(Job.grade_id == grade.id)
        ).one()
        dests.append(dispatch._prepare_grade(session, job)["dest"])

    assert f"__g{one.id}__" in dests[0]
    assert f"__g{two.id}__" in dests[1]
    # Same look, so the same hash: only the id tells the two files apart.
    assert dests[0].split("__g")[0] == dests[1].split("__g")[0]
    assert dests[0].split("__")[-1] == dests[1].split("__")[-1]


def test_a_name_already_taken_on_the_clip_is_refused(session: Session, sequence: Sequence):
    """Renaming is what makes two rows tell each other apart, so a collision cannot be
    allowed to happen quietly."""
    render = _clip(session, sequence)
    _put(session, render, "Golden hour")
    other = _put(session, render, "Blue hour")

    with pytest.raises(HTTPException) as raised:
        routes.save_grade(other.id, schemas.GradeIn(label="Golden hour"), session=session)
    assert raised.value.status_code == 409

    # The same name on another clip is not a collision: names live per clip.
    elsewhere = _clip(session, sequence, template="v_1080")
    assert _put(session, elsewhere, "Golden hour").label == "Golden hour"


def test_deleting_a_grade_leaves_its_siblings_alone(
    session: Session, sequence: Sequence, tmp_path
):
    """Deleting a parent deletes its children, and a sibling is not a child."""
    render = _clip(session, sequence)
    warm = _put(session, render, "Golden hour", temperature=7400)
    cold = _put(session, render, "Blue hour", temperature=5200)

    routes.delete_grade(warm.id, session=session)

    left = session.exec(select(Grade).where(Grade.render_id == render.id)).all()
    assert [g.id for g in left] == [cold.id]


def test_deleting_the_file_keeps_the_look(session: Session, sequence: Sequence):
    """The two gestures a named grade needs, and they are not the same one: throwing
    away a hundred megabytes must not throw away the look that made them."""
    render = _clip(session, sequence)
    grade = _put(session, render, "Golden hour", temperature=7400)
    row = session.get(Grade, grade.id)
    assert row is not None
    row.state = GradeState.DONE
    row.out_path = "graded/x.mp4"
    session.add(row)
    session.commit()

    routes.delete_graded_file(grade.id, session=session)

    kept = session.get(Grade, grade.id)
    assert kept is not None
    assert kept.state is GradeState.DRAFT
    assert kept.out_path is None
    assert kept.params["temperature"] == 7400


def test_the_clip_is_measured_once_for_all_its_grades(
    session: Session, sequence: Sequence, monkeypatch
):
    """The analysis moved from the grade to the render, and this is why: it measures the
    clip, not the look. It is a decode pass of a few seconds, and it used to be stored
    per grade, so five looks on one clip would have run it five times."""
    render = _clip(session, sequence)
    runs = []

    def fake_analyse(source):  # noqa: ANN001, ANN202
        runs.append(source)
        return grading_service.Analysis(
            frames=10, y_low=64, y_high=940, y_avg=500, sat_avg=40,
            clipped_black=0.0, clipped_white=0.0, looks_log=False,
            darkest_ms=0.0, median_ms=500.0, brightest_ms=900.0,
        )

    monkeypatch.setattr(grading_service, "analyse", fake_analyse)
    monkeypatch.setattr(routes, "to_absolute", lambda path: __import__("pathlib").Path(__file__))

    routes._analyse_render(session, render)
    routes._analyse_render(session, render)

    assert len(runs) == 1
    assert (session.get(Render, render.id) or render).analysis["frames"] == 10
