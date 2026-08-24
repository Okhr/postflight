"""Cuts survive being edited, and say what has been made from them.

Both properties are about identity. A cut is the subject of a render, so its id has
to outlive an edit of its bounds, and the rush tree asks each cut whether a
stabilized and a graded file exist for it.

The derushed mark of a rush is here too, since it is the other half of the same
question: what is left to do on this rush.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.api import routes, schemas
from app.models import Cut, Grade, GradeState, Render, RenderState, Sequence
from app.paths import to_absolute


def _save(session: Session, seq: Sequence, *cuts: schemas.CutIn) -> list[schemas.CutOut]:
    return routes.replace_cuts(
        seq.id, schemas.CutsReplaceIn(cuts=list(cuts)), session=session
    )


def _framed(session: Session, seq: Sequence) -> Sequence:
    """The fixture rush has no frame count, and cuts are clamped to it."""
    seq.frame_count = 14_400
    session.add(seq)
    session.commit()
    return seq


def _on_disk(relative: str | None) -> Path:
    """A real file where a path says there is one, so unlinking it can be measured."""
    assert relative is not None
    path = to_absolute(relative)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"a clip")
    return path


def _render(session: Session, seq: Sequence, cut_id: int | None, state: RenderState) -> Render:
    render = Render(
        sequence_id=seq.id,  # type: ignore[arg-type]
        cut_id=cut_id,
        template="16x9",
        state=state,
        out_path=f"out/render-{cut_id}.mp4",
    )
    session.add(render)
    session.commit()
    session.refresh(render)
    return render


# --------------------------------------------------------------------------- #
# A cut keeps its id
# --------------------------------------------------------------------------- #

def test_a_cut_keeps_its_id_when_its_bounds_move(session: Session, sequence: Sequence):
    seq = _framed(session, sequence)
    [first] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))

    [moved] = _save(
        session, seq, schemas.CutIn(id=first.id, label="one", start_frame=150, end_frame=260)
    )

    assert moved.id == first.id
    assert (moved.start_frame, moved.end_frame) == (150, 260)


def test_a_render_still_finds_its_cut_after_the_cut_is_resized(
    session: Session, sequence: Sequence
):
    """The reason ids have to be stable: `dispatch.prepare` reads `render.cut_id` to
    build the trim range, and used to fail with "cut N not found" after any save."""
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    render = _render(session, seq, cut.id, RenderState.DONE)

    _save(session, seq, schemas.CutIn(id=cut.id, label="one", start_frame=100, end_frame=300))

    assert session.get(Cut, render.cut_id) is not None


def test_a_cut_with_no_id_is_a_new_one(session: Session, sequence: Sequence):
    seq = _framed(session, sequence)
    [first] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))

    saved = _save(
        session,
        seq,
        schemas.CutIn(id=first.id, label="one", start_frame=100, end_frame=200),
        schemas.CutIn(label="two", start_frame=400, end_frame=500),
    )

    assert [c.label for c in saved] == ["one", "two"]
    assert saved[0].id == first.id
    assert saved[1].id != first.id


def test_a_cut_left_out_of_the_list_is_deleted(session: Session, sequence: Sequence):
    seq = _framed(session, sequence)
    saved = _save(
        session,
        seq,
        schemas.CutIn(label="one", start_frame=100, end_frame=200),
        schemas.CutIn(label="two", start_frame=400, end_frame=500),
    )

    left = _save(session, seq, schemas.CutIn(id=saved[1].id, label="two", start_frame=400, end_frame=500))

    assert [c.label for c in left] == ["two"]
    assert session.get(Cut, saved[0].id) is None


def test_an_id_from_another_rush_is_not_adopted(session: Session, sequence: Sequence):
    """Otherwise a cut could be stolen from one rush by naming its id in another."""
    seq = _framed(session, sequence)
    other = Sequence(key="other", label="other", frame_count=14_400)
    session.add(other)
    session.commit()
    [mine] = _save(session, seq, schemas.CutIn(label="mine", start_frame=100, end_frame=200))

    [theirs] = routes.replace_cuts(
        other.id,
        schemas.CutsReplaceIn(cuts=[schemas.CutIn(id=mine.id, label="theirs", start_frame=10, end_frame=20)]),
        session=session,
    )

    assert theirs.id != mine.id
    assert session.get(Cut, mine.id).sequence_id == seq.id
    assert session.get(Cut, mine.id).label == "mine"


def test_the_order_index_follows_the_bounds(session: Session, sequence: Sequence):
    """Saved out of order, they come back in playing order, renumbered from 0."""
    seq = _framed(session, sequence)
    saved = _save(
        session,
        seq,
        schemas.CutIn(label="late", start_frame=900, end_frame=1000),
        schemas.CutIn(label="early", start_frame=100, end_frame=200),
    )

    assert [(c.label, c.order_index) for c in saved] == [("early", 0), ("late", 1)]


# --------------------------------------------------------------------------- #
# What has been made from a cut
# --------------------------------------------------------------------------- #

def test_a_cut_with_nothing_made_from_it_is_neither_rendered_nor_graded(
    session: Session, sequence: Sequence
):
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))

    assert (cut.rendered, cut.graded) == (False, False)


def test_a_finished_render_makes_its_cut_rendered(session: Session, sequence: Sequence):
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    _render(session, seq, cut.id, RenderState.DONE)

    detail = routes.get_sequence(seq.id, session=session)

    assert [(c.rendered, c.graded) for c in detail.cuts] == [(True, False)]


def test_a_render_still_running_does_not_count(session: Session, sequence: Sequence):
    """The icon says a file exists, not that one was asked for."""
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    _render(session, seq, cut.id, RenderState.RUNNING)

    detail = routes.get_sequence(seq.id, session=session)

    assert detail.cuts[0].rendered is False


def test_a_finished_grade_makes_its_cut_graded(session: Session, sequence: Sequence):
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    render = _render(session, seq, cut.id, RenderState.DONE)
    session.add(Grade(render_id=render.id, state=GradeState.DONE, out_path="out/graded.mp4"))
    session.commit()

    detail = routes.get_sequence(seq.id, session=session)

    assert [(c.rendered, c.graded) for c in detail.cuts] == [(True, True)]


def test_a_grade_still_a_draft_does_not_count(session: Session, sequence: Sequence):
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    render = _render(session, seq, cut.id, RenderState.DONE)
    session.add(Grade(render_id=render.id, state=GradeState.DRAFT))
    session.commit()

    detail = routes.get_sequence(seq.id, session=session)

    assert detail.cuts[0].graded is False


def test_a_render_of_the_whole_rush_lights_no_cut(session: Session, sequence: Sequence):
    """`cut_id` is null for a render of the whole thing, which belongs to no cut."""
    seq = _framed(session, sequence)
    _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    _render(session, seq, None, RenderState.DONE)

    detail = routes.get_sequence(seq.id, session=session)

    assert detail.cuts[0].rendered is False


def test_the_flags_come_back_straight_from_a_save(session: Session, sequence: Sequence):
    """A resize saves by itself, so the answer has to carry the flags: without them
    the tree would put its icons out until the next refetch."""
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    _render(session, seq, cut.id, RenderState.DONE)

    [resized] = _save(
        session, seq, schemas.CutIn(id=cut.id, label="one", start_frame=100, end_frame=300)
    )

    assert resized.rendered is True


def test_dropping_a_cut_takes_its_files_with_it(session: Session, sequence: Sequence):
    """Deleting a parent deletes its children, to the file on disk.

    The detached render was the quiet leak: a clip in `out/` no tree could name any
    more, since every view of it hangs off the cut that is gone.
    """
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    render = _render(session, seq, cut.id, RenderState.DONE)
    clip = _on_disk(render.out_path)

    _save(session, seq)

    assert session.exec(select(Cut)).all() == []
    assert session.get(Render, render.id) is None
    assert not clip.exists()


def test_dropping_a_cut_cancels_a_render_still_waiting(session: Session, sequence: Sequence):
    """A render whose subject is gone has nothing left to render. The job goes with
    it, and the worker holding it stops on its next heartbeat."""
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    render = _render(session, seq, cut.id, RenderState.QUEUED)

    _save(session, seq)

    assert session.get(Render, render.id) is None


def test_dropping_a_cut_takes_the_graded_file_too(session: Session, sequence: Sequence):
    """`grade.render_id` is unique but not a foreign key, so nothing would have
    complained about a grade row pointing at a render that no longer exists."""
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    render = _render(session, seq, cut.id, RenderState.DONE)
    grade = Grade(
        render_id=render.id,  # type: ignore[arg-type]
        state=GradeState.DONE,
        out_path="graded/graded-1.mp4",
    )
    session.add(grade)
    session.commit()
    graded = _on_disk(grade.out_path)

    _save(session, seq)

    assert session.exec(select(Grade)).all() == []
    assert not graded.exists()


def test_deleting_one_cut_leaves_the_others_alone(session: Session, sequence: Sequence):
    """The route the tree and the stabilize queue use: one cut by its id, not a
    rewrite of the whole list."""
    seq = _framed(session, sequence)
    first, second = _save(
        session,
        seq,
        schemas.CutIn(label="one", start_frame=100, end_frame=200),
        schemas.CutIn(label="two", start_frame=300, end_frame=400),
    )
    render = _render(session, seq, first.id, RenderState.DONE)
    clip = _on_disk(render.out_path)

    answer = routes.delete_cut(first.id, session=session)

    assert answer["files_removed"] == [clip.name]
    assert [c.label for c in session.exec(select(Cut)).all()] == ["two"]
    assert session.get(Render, render.id) is None


def test_deleting_an_unknown_cut_is_a_404(session: Session):
    with pytest.raises(HTTPException) as raised:
        routes.delete_cut(9999, session=session)
    assert raised.value.status_code == 404


def test_a_cut_says_how_many_files_it_made(session: Session, sequence: Sequence):
    """What the delete dialog says out loud, so it never has to be counted twice."""
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    assert routes.get_sequence(seq.id, session=session).cuts[0].files == 0

    render = _render(session, seq, cut.id, RenderState.DONE)
    assert routes.get_sequence(seq.id, session=session).cuts[0].files == 1

    session.add(
        Grade(
            render_id=render.id,  # type: ignore[arg-type]
            state=GradeState.DONE,
            out_path="graded/graded-1.mp4",
        )
    )
    session.commit()
    assert routes.get_sequence(seq.id, session=session).cuts[0].files == 2


def test_a_render_still_going_is_not_a_file_yet(session: Session, sequence: Sequence):
    seq = _framed(session, sequence)
    [cut] = _save(session, seq, schemas.CutIn(label="one", start_frame=100, end_frame=200))
    _render(session, seq, cut.id, RenderState.RUNNING)

    assert routes.get_sequence(seq.id, session=session).cuts[0].files == 0


# --------------------------------------------------------------------------- #
# Marking a rush derushed
# --------------------------------------------------------------------------- #

def test_a_rush_starts_out_not_derushed(session: Session, sequence: Sequence):
    assert routes.get_sequence(sequence.id, session=session).derushed is False


def test_the_mark_goes_on_and_comes_back_off(session: Session, sequence: Sequence):
    marked = routes.update_sequence(
        sequence.id, label=None, derushed=True, folder_id=None, session=session
    )
    assert marked.derushed is True

    unmarked = routes.update_sequence(
        sequence.id, label=None, derushed=False, folder_id=None, session=session
    )
    assert unmarked.derushed is False


def test_a_rush_with_no_cuts_at_all_can_be_marked(session: Session, sequence: Sequence):
    """The reason it is a mark and not a count: a rush worth nothing is derushed the
    moment it has been looked at, and it has no cuts to show for it."""
    seq = _framed(session, sequence)
    marked = routes.update_sequence(
        seq.id, label=None, derushed=True, folder_id=None, session=session
    )

    assert (marked.derushed, marked.cut_count) == (True, 0)


def test_renaming_does_not_disturb_the_mark(session: Session, sequence: Sequence):
    routes.update_sequence(sequence.id, label=None, derushed=True, folder_id=None, session=session)

    renamed = routes.update_sequence(
        sequence.id, label="an evening at Pierrevert", derushed=None, folder_id=None, session=session
    )

    assert (renamed.label, renamed.derushed) == ("an evening at Pierrevert", True)
