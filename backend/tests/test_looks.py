"""Named looks, the way the Gyroflow profiles are named.

The one rule that shapes them: a look is six numbers and a name, and the black and
white points are not among them. They are measured on one clip's own range, so a look
that carried them would take one shot's shadows to another.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.api import routes, schemas


def _make(session: Session, label: str, **params) -> schemas.LookOut:
    return routes.create_look(schemas.LookIn(label=label, params=params), session=session)


def test_a_look_holds_the_settings_that_travel(session: Session):
    look = _make(session, "Golden", exposure=0.3, temperature=4200)

    assert look.params["exposure"] == 0.3
    assert look.params["temperature"] == 4200
    assert look.params["contrast"] == 1.0  # filled in from the defaults


def test_the_points_never_get_stored(session: Session):
    """Sent anyway by a caller that copied a whole look, and dropped here rather than
    trusted: a look applied to ten clips would otherwise carry one clip's range."""
    look = _make(session, "Golden", exposure=0.3, black_point=0.25, white_point=0.8)

    assert "black_point" not in look.params
    assert "white_point" not in look.params


def test_a_look_needs_a_name(session: Session):
    with pytest.raises(HTTPException) as raised:
        _make(session, "   ")
    assert raised.value.status_code == 422


def test_looks_come_back_by_name(session: Session):
    _make(session, "Zenith")
    _make(session, "Autumn")

    assert [look.label for look in routes.list_looks(session=session)] == ["Autumn", "Zenith"]


def test_renaming_keeps_the_settings(session: Session):
    look = _make(session, "Draft", exposure=0.4)

    renamed = routes.update_look(look.id, schemas.LookPatch(label="Golden"), session=session)

    assert renamed.label == "Golden"
    assert renamed.params["exposure"] == 0.4


def test_a_look_can_be_written_over(session: Session):
    """Tuning a clip further and pointing the same name at it, which is what a look
    being a working tool rather than an archive means."""
    look = _make(session, "Golden", exposure=0.4)

    updated = routes.update_look(
        look.id, schemas.LookPatch(params={"exposure": 0.9, "black_point": 0.3}), session=session
    )

    assert updated.params["exposure"] == 0.9
    assert "black_point" not in updated.params


def test_deleting_one_leaves_the_clips_wearing_it(session: Session):
    """Applying a look copies its numbers into a grade, so nothing hangs off the row."""
    look = _make(session, "Golden", exposure=0.4)

    assert routes.delete_look(look.id, session=session) == {"deleted": look.id}
    assert routes.list_looks(session=session) == []


def test_deleting_an_unknown_look_is_a_404(session: Session):
    with pytest.raises(HTTPException) as raised:
        routes.delete_look(4242, session=session)
    assert raised.value.status_code == 404
