"""The stabilize queue: what is left to do, and what has already been done with what.

The page it feeds exists to answer one question the old one could not: can I take
everything at once, and what would that mean. So the answer has to carry, per
sequence, the profiles a file already exists for.
"""

from __future__ import annotations

from sqlmodel import Session

from app.api import routes, schemas
from app.models import Cut, Render, RenderState, Sequence, SequenceState


def _cut(session: Session, seq: Sequence, label: str, start: int, end: int) -> Cut:
    cut = Cut(sequence_id=seq.id, label=label, start_frame=start, end_frame=end, order_index=0)
    session.add(cut)
    session.commit()
    session.refresh(cut)
    return cut


def _render(session: Session, seq: Sequence, cut: Cut | None, template: str, state: RenderState):
    render = Render(
        sequence_id=seq.id,  # type: ignore[arg-type]
        cut_id=cut.id if cut else None,
        template=template,
        state=state,
        out_path="out/x.mp4" if state == RenderState.DONE else None,
    )
    session.add(render)
    session.commit()
    return render


def _ready(session: Session, seq: Sequence) -> Sequence:
    seq.state = SequenceState.READY
    seq.frame_count = 14_400
    session.add(seq)
    session.commit()
    return seq


def test_a_rush_with_no_marked_sequence_is_not_in_the_queue(session: Session, sequence: Sequence):
    """Stabilizing a whole rush is not offered, so a rush with nothing marked has
    nothing to offer either."""
    _ready(session, sequence)

    assert routes.stabilize_queue(session=session) == []


def test_a_rush_that_is_not_ready_stays_out(session: Session, sequence: Sequence):
    """A render needs the merged file, and only that state guarantees it."""
    sequence.state = SequenceState.MERGING
    session.add(sequence)
    session.commit()
    _cut(session, sequence, "one", 100, 200)

    assert routes.stabilize_queue(session=session) == []


def test_the_queue_carries_each_sequence_with_its_timecodes(session: Session, sequence: Sequence):
    seq = _ready(session, sequence)
    _cut(session, seq, "dive", 6000, 9000)

    [rush] = routes.stabilize_queue(session=session)

    assert rush.id == seq.id
    assert [c.label for c in rush.cuts] == ["dive"]
    assert rush.cuts[0].frames == 3001
    assert rush.cuts[0].start_tc and rush.cuts[0].end_tc


def test_a_finished_render_is_named_and_addressable(session: Session, sequence: Sequence):
    """The whole point: a sequence says what it has been rendered with, so the page
    can arrive with it unticked and tick it again when the profile changes."""
    seq = _ready(session, sequence)
    cut = _cut(session, seq, "one", 100, 200)
    _render(session, seq, cut, "h_1080", RenderState.DONE)

    [rush] = routes.stabilize_queue(session=session)

    assert [r.template for r in rush.cuts[0].done] == ["h_1080"]
    assert rush.cuts[0].done[0].id > 0
    assert rush.cuts[0].busy == []


def test_a_render_in_flight_is_busy_not_done(session: Session, sequence: Sequence):
    seq = _ready(session, sequence)
    cut = _cut(session, seq, "one", 100, 200)
    _render(session, seq, cut, "v_1080", RenderState.RUNNING)

    [rush] = routes.stabilize_queue(session=session)

    assert rush.cuts[0].done == []
    assert [r.template for r in rush.cuts[0].busy] == ["v_1080"]


def test_two_profiles_on_one_sequence_both_show(session: Session, sequence: Sequence):
    seq = _ready(session, sequence)
    cut = _cut(session, seq, "one", 100, 200)
    _render(session, seq, cut, "h_1080", RenderState.DONE)
    _render(session, seq, cut, "v_1080", RenderState.DONE)

    [rush] = routes.stabilize_queue(session=session)

    assert [r.template for r in rush.cuts[0].done] == ["h_1080", "v_1080"]


def test_a_failed_render_leaves_the_sequence_undone(session: Session, sequence: Sequence):
    """It has to come back ticked: a failure is work still to do."""
    seq = _ready(session, sequence)
    cut = _cut(session, seq, "one", 100, 200)
    _render(session, seq, cut, "h_1080", RenderState.FAILED)

    [rush] = routes.stabilize_queue(session=session)

    assert (rush.cuts[0].done, rush.cuts[0].busy) == ([], [])


def test_a_render_of_the_whole_rush_names_no_sequence(session: Session, sequence: Sequence):
    """Old renders with a null cut_id exist; they must not mark a sequence as done."""
    seq = _ready(session, sequence)
    _cut(session, seq, "one", 100, 200)
    _render(session, seq, None, "h_1080", RenderState.DONE)

    [rush] = routes.stabilize_queue(session=session)

    assert rush.cuts[0].done == []


def test_a_rush_without_fps_does_not_take_the_queue_down(session: Session, sequence: Sequence):
    """A probe that failed leaves no fps, and a division by zero would turn the whole
    page into a 500 over one bad rush."""
    seq = _ready(session, sequence)
    seq.fps_num, seq.fps_den = 0, 0
    session.add(seq)
    session.commit()
    _cut(session, seq, "one", 100, 200)

    [rush] = routes.stabilize_queue(session=session)

    assert rush.cuts[0].duration_ms == 0.0
    assert rush.cuts[0].start_tc == ""


def test_rushes_come_oldest_first(session: Session, sequence: Sequence):
    """The order a session is walked in, same as the derush list."""
    from datetime import datetime, timezone

    old = _ready(session, sequence)
    old.recorded_at = datetime(2026, 7, 3, 17, 28, tzinfo=timezone.utc)
    recent = Sequence(
        key="later", label="later", state=SequenceState.READY, frame_count=1000,
        fps_num=60000, fps_den=1001,
        recorded_at=datetime(2026, 8, 9, 14, 46, tzinfo=timezone.utc),
    )
    session.add_all([old, recent])
    session.commit()
    _cut(session, old, "one", 100, 200)
    _cut(session, recent, "one", 10, 20)

    queue = routes.stabilize_queue(session=session)

    assert [r.label for r in queue] == [old.label, "later"]


# --------------------------------------------------------------------------- #
# A failing render is not a failing rush
# --------------------------------------------------------------------------- #

def test_a_failed_render_does_not_invalidate_the_rush(session: Session, sequence: Sequence):
    """Seen in use: one render killed by a restart marked the rush failed, which took
    it out of this queue entirely and printed gyroflow's stderr on its row in the
    tree. The merged file was never in question."""
    from app import dispatch
    from app.models import Job, JobKind, JobState

    seq = _ready(session, sequence)
    _cut(session, seq, "one", 100, 200)
    render = _render(session, seq, None, "h_1080", RenderState.QUEUED)
    job = Job(kind=JobKind.RENDER, state=JobState.RUNNING, sequence_id=seq.id,
              render_id=render.id, payload={"render_id": render.id})
    session.add(job)
    session.commit()

    dispatch._fail(session, job, "gyroflow exited with code -15")

    session.refresh(seq)
    session.refresh(render)
    assert seq.state == SequenceState.READY
    assert seq.error is None
    assert render.state == RenderState.FAILED
    assert routes.stabilize_queue(session=session)[0].id == seq.id


def test_a_failed_proxy_does_invalidate_the_rush(session: Session, sequence: Sequence):
    """The other half of the rule: without a proxy there is nothing to work on."""
    from app import dispatch
    from app.models import Job, JobKind, JobState

    seq = _ready(session, sequence)
    job = Job(kind=JobKind.PROXY, state=JobState.RUNNING, sequence_id=seq.id, payload={})
    session.add(job)
    session.commit()

    dispatch._fail(session, job, "ffmpeg died")

    session.refresh(seq)
    assert seq.state == SequenceState.FAILED
    assert seq.error == "ffmpeg died"
