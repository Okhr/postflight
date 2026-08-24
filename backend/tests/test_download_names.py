"""A downloaded file is named the way the interface names things, slugified.

On disk a render is `DJI_20260711191722_0025_D__h_1080__c00.mp4`: unambiguous, which
is what the worker cache needs, and unreadable in a downloads folder. The name served
is the rush, the sequence and the profile, the same three words on the row the file
came from, in the same shape the volume already uses: `__` between fields, `_` inside.
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

    assert _named(session, _render(session, sequence, cut)) == "rush_1__dive__wide_1080p.mp4"


def test_a_profile_deleted_since_leaves_its_id(session: Session, sequence: Sequence):
    """All that is left of it. Better than dropping the only thing that tells two
    files of the same sequence apart."""
    sequence.label = "Rush 1"
    session.add(sequence)
    session.commit()
    cut = _cut(session, sequence, "dive")

    name = _named(session, _render(session, sequence, cut, template="gone_forever"))

    assert name == "rush_1__dive__gone_forever.mp4"


def test_a_whole_rush_render_names_no_sequence(session: Session, sequence: Sequence):
    """No cut, so no empty ` -  - ` in the middle."""
    sequence.label = "Rush 1"
    session.add(sequence)
    session.commit()

    assert _named(session, _render(session, sequence, None)) == "rush_1__wide_1080p.mp4"


def test_the_graded_file_says_so(session: Session, sequence: Sequence):
    sequence.label = "Rush 1"
    session.add(sequence)
    session.commit()
    cut = _cut(session, sequence, "dive")

    name = _named(session, _render(session, sequence, cut), graded=True)

    assert name == "rush_1__dive__wide_1080p__graded.mp4"


def test_a_label_is_slugified_however_it_was_typed(session: Session, sequence: Sequence):
    """Labels are free text: a slash would read as a path, an accent as mojibake in a
    latin-1 header, and a space is merely unpleasant to hand to a shell."""
    sequence.label = "16/9 Été"
    session.add(sequence)
    session.commit()
    cut = _cut(session, sequence, 'a "quoted" bit')

    name = _named(session, _render(session, sequence, cut))

    assert name == "16_9_ete__a_quoted_bit__wide_1080p.mp4"
    assert name.isascii() and " " not in name


def test_a_label_of_nothing_usable_drops_out(session: Session, sequence: Sequence):
    """An emoji-only sequence name leaves no fragment, and an empty one must not
    leave `__` behind either."""
    sequence.label = "Rush 1"
    session.add(sequence)
    session.commit()
    cut = _cut(session, sequence, "🚀")

    assert _named(session, _render(session, sequence, cut)) == "rush_1__wide_1080p.mp4"


def test_a_very_long_label_is_capped(session: Session, sequence: Sequence):
    """Three labels and an extension have to fit in one filename."""
    sequence.label = "x" * 200
    session.add(sequence)
    session.commit()

    name = _named(session, _render(session, sequence, None))

    assert name.startswith("x" * 60 + "__")
    assert len(name) < 128


def test_the_download_header_carries_the_slug():
    header = _disposition("rush_1__dive__wide_1080p.mp4")

    assert header == 'attachment; filename="rush_1__dive__wide_1080p.mp4"'



def test_a_download_of_nothing_is_a_404(session: Session):
    with pytest.raises(HTTPException) as raised:
        routes.download_render(4242, request=None, session=session)  # type: ignore[arg-type]
    assert raised.value.status_code == 404
