"""A downloaded file is named the way the interface names things.

On disk a render is `DJI_20260711191722_0025_D__h_1080__c00.mp4`: unambiguous, which
is what the worker cache needs, and unreadable in a downloads folder. The name served
is the rush, the sequence and the profile, the same three words on the row the file
came from.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.api import routes
from app.api.media import _disposition
from app.services import gyroflow as gyroflow_service
from app.models import Cut, Grade, GradeState, Render, RenderState, Sequence
from app.paths import to_absolute


def _profile(label: str = "Wide 1080p") -> str:
    """A profile of this test's own, so the name asserted is not a shipped label."""
    return gyroflow_service.create_template(label).id


def _render(session: Session, seq: Sequence, cut: Cut | None, template: str = "") -> Render:
    template = template or _profile()
    render = Render(
        sequence_id=seq.id,  # type: ignore[arg-type]
        cut_id=cut.id if cut else None,
        template=template,
        state=RenderState.DONE,
        out_path=f"out/{seq.key}__{template}__c00.mp4",
    )
    session.add(render)
    session.commit()
    session.refresh(render)
    return render


def _cut(session: Session, seq: Sequence, label: str) -> Cut:
    cut = Cut(sequence_id=seq.id, label=label, start_frame=10, end_frame=100, order_index=0)
    session.add(cut)
    session.commit()
    session.refresh(cut)
    return cut


def _named(session: Session, render: Render, graded: bool = False) -> str:
    path = to_absolute(render.out_path)
    assert path is not None
    return routes._render_name(session, render, path, graded=graded)


def test_a_render_is_named_rush_sequence_profile(session: Session, sequence: Sequence):
    sequence.label = "Rush 1"
    session.add(sequence)
    session.commit()
    cut = _cut(session, sequence, "dive")

    assert _named(session, _render(session, sequence, cut)) == "Rush 1 - dive - Wide 1080p.mp4"


def test_a_profile_deleted_since_leaves_its_id(session: Session, sequence: Sequence):
    """All that is left of it. Better than dropping the only thing that tells two
    files of the same sequence apart."""
    sequence.label = "Rush 1"
    session.add(sequence)
    session.commit()
    cut = _cut(session, sequence, "dive")

    name = _named(session, _render(session, sequence, cut, template="gone_forever"))

    assert name == "Rush 1 - dive - gone_forever.mp4"


def test_a_whole_rush_render_names_no_sequence(session: Session, sequence: Sequence):
    """No cut, so no empty ` -  - ` in the middle."""
    sequence.label = "Rush 1"
    session.add(sequence)
    session.commit()

    assert _named(session, _render(session, sequence, None)) == "Rush 1 - Wide 1080p.mp4"


def test_the_graded_file_says_so(session: Session, sequence: Sequence):
    sequence.label = "Rush 1"
    session.add(sequence)
    session.commit()
    cut = _cut(session, sequence, "dive")

    name = _named(session, _render(session, sequence, cut), graded=True)

    assert name == "Rush 1 - dive - Wide 1080p - graded.mp4"


def test_a_label_cannot_carry_a_path_separator(session: Session, sequence: Sequence):
    """Labels are free text, and a slash in one would be a path, not a name."""
    sequence.label = "16/9 tests"
    session.add(sequence)
    session.commit()
    cut = _cut(session, sequence, 'a "quoted" bit')

    name = _named(session, _render(session, sequence, cut))

    assert "/" not in name and '"' not in name
    assert name == "16 9 tests - a quoted bit - Wide 1080p.mp4"


def test_the_download_header_carries_the_name_twice():
    """A header is ASCII and a label is not, so both forms go out (RFC 5987)."""
    header = _disposition("Quissac été - séquence 2 - Vertical.mp4")

    assert 'filename="Quissac ete - sequence 2 - Vertical.mp4"' in header
    assert "filename*=UTF-8''Quissac%20%C3%A9t%C3%A9" in header


def test_a_download_of_nothing_is_a_404(session: Session):
    with pytest.raises(HTTPException) as raised:
        routes.download_render(4242, request=None, session=session)  # type: ignore[arg-type]
    assert raised.value.status_code == 404
