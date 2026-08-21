"""Drawers for rushes: two levels, and nothing in them is ever at risk.

These call the route functions with a session rather than going through HTTP: the
rules worth pinning down are about the shape of the tree and about what survives a
deletion, and neither of those is about serialization.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.api import routes, schemas
from app.models import Folder, Sequence


def _folder(session: Session, name: str, parent_id: int | None = None) -> schemas.FolderOut:
    return routes.create_folder(schemas.FolderIn(name=name, parent_id=parent_id), session)


# --------------------------------------------------------------------------- #
# The shape of the tree
# --------------------------------------------------------------------------- #

def test_a_new_folder_draws_a_colour_when_none_is_given(session: Session):
    """So a folder made in a hurry still comes out told apart from its neighbours."""
    assert _folder(session, "Pierrevert").color in routes.FOLDER_COLORS


def test_a_chosen_colour_is_kept(session: Session):
    created = routes.create_folder(schemas.FolderIn(name="Pierrevert", color="violet"), session)
    assert created.color == "violet"


def test_a_colour_outside_the_palette_is_refused(session: Session):
    """The front decides what each token looks like, so one it has no entry for would
    come out as no colour at all. Better a 400 than a folder with an invisible dot."""
    with pytest.raises(HTTPException) as raised:
        routes.create_folder(schemas.FolderIn(name="Pierrevert", color="chartreuse"), session)
    assert raised.value.status_code == 400

    folder = _folder(session, "Manosque")
    with pytest.raises(HTTPException):
        routes.update_folder(folder.id, schemas.FolderPatch(color="#ff0000"), session)


def test_the_colour_can_be_changed(session: Session):
    folder = _folder(session, "Pierrevert")
    recoloured = routes.update_folder(folder.id, schemas.FolderPatch(color="sky"), session)
    assert (recoloured.name, recoloured.color) == ("Pierrevert", "sky")


def test_two_levels_is_the_limit(session: Session):
    site = _folder(session, "Pierrevert")
    outing = _folder(session, "August", site.id)
    with pytest.raises(HTTPException) as raised:
        _folder(session, "deeper still", outing.id)
    assert raised.value.status_code == 400


def test_a_folder_cannot_hold_itself(session: Session):
    site = _folder(session, "Pierrevert")
    with pytest.raises(HTTPException):
        routes.update_folder(site.id, schemas.FolderPatch(parent_id=site.id), session)


def test_a_folder_with_children_cannot_move_into_one(session: Session):
    """It would be three levels deep counting from the new parent."""
    site, other = _folder(session, "Pierrevert"), _folder(session, "Manosque")
    _folder(session, "August", site.id)
    with pytest.raises(HTTPException):
        routes.update_folder(site.id, schemas.FolderPatch(parent_id=other.id), session)


def test_a_child_can_be_moved_back_to_the_root(session: Session):
    """Null and absent look alike in a patch, so this reads `model_fields_set`."""
    site = _folder(session, "Pierrevert")
    child = _folder(session, "August", site.id)
    moved = routes.update_folder(child.id, schemas.FolderPatch(parent_id=None), session)
    assert moved.parent_id is None


def test_renaming_leaves_the_colour_alone(session: Session):
    folder = _folder(session, "Pierrevert")
    renamed = routes.update_folder(folder.id, schemas.FolderPatch(name="Manosque"), session)
    assert (renamed.name, renamed.color) == ("Manosque", folder.color)


# --------------------------------------------------------------------------- #
# The order they sit in
# --------------------------------------------------------------------------- #

def _names(session: Session, parent_id: int | None = None) -> list[str]:
    return [f.name for f in routes.list_folders(session) if f.parent_id == parent_id]


def test_a_new_folder_lands_last(session: Session):
    """Appearing in the middle of an order someone arranged by hand would be the
    surprising choice."""
    for name in ("Quissac", "Pierrevert", "Manosque"):
        _folder(session, name)
    assert _names(session) == ["Quissac", "Pierrevert", "Manosque"]


def test_a_folder_moves_between_two_others(session: Session):
    a, b, c = (_folder(session, n) for n in ("A", "B", "C"))
    routes.update_folder(c.id, schemas.FolderPatch(position=1), session)
    assert _names(session) == ["A", "C", "B"]


def test_moving_to_the_front_and_to_the_back(session: Session):
    for name in ("A", "B", "C"):
        _folder(session, name)
    last = [f for f in routes.list_folders(session) if f.name == "C"][0]
    routes.update_folder(last.id, schemas.FolderPatch(position=0), session)
    assert _names(session) == ["C", "A", "B"]

    routes.update_folder(last.id, schemas.FolderPatch(position=99), session)
    assert _names(session) == ["A", "B", "C"], "past the last row means last, not refused"


def test_the_ranks_stay_dense_after_a_move(session: Session):
    """Two folders sharing a rank is what a shift-the-neighbours version leaves
    behind, and the order then depends on the id tiebreak instead of on the move."""
    for name in ("A", "B", "C", "D"):
        _folder(session, name)
    moved = [f for f in routes.list_folders(session) if f.name == "D"][0]
    routes.update_folder(moved.id, schemas.FolderPatch(position=1), session)
    assert [f.position for f in routes.list_folders(session)] == [0, 1, 2, 3]


def test_a_folder_given_a_new_parent_lands_last_in_it(session: Session):
    site = _folder(session, "Site")
    first = _folder(session, "One", site.id)
    second = _folder(session, "Two")
    routes.update_folder(second.id, schemas.FolderPatch(parent_id=site.id), session)
    assert _names(session, site.id) == [first.name, "Two"]


def test_a_child_promoted_to_the_root_can_be_placed(session: Session):
    """Dropping a subfolder in a gap between two root folders is one gesture, so it
    carries both the new parent and the rank."""
    a = _folder(session, "A")
    _folder(session, "B")
    child = _folder(session, "Child", a.id)
    routes.update_folder(
        child.id, schemas.FolderPatch(parent_id=None, position=1), session
    )
    assert _names(session) == ["A", "Child", "B"]


def test_the_list_a_folder_left_closes_up(session: Session):
    """The hole is not cosmetic: the next folder placed in that list lands on the rank
    the departed one held, and two siblings then share it."""
    site = _folder(session, "Site")
    for name in ("One", "Two", "Three"):
        _folder(session, name, site.id)
    two = [f for f in routes.list_folders(session) if f.name == "Two"][0]

    routes.update_folder(two.id, schemas.FolderPatch(parent_id=None), session)

    kept = [f for f in routes.list_folders(session) if f.parent_id == site.id]
    assert [(f.name, f.position) for f in kept] == [("One", 0), ("Three", 1)]


def test_nesting_then_promoting_leaves_the_root_dense(session: Session):
    """A folder pushed into another and pulled back out: both lists it passed through
    have to come out without a gap and without a tie."""
    quissac = _folder(session, "Quissac")
    b = _folder(session, "B")

    routes.update_folder(b.id, schemas.FolderPatch(parent_id=quissac.id), session)
    routes.update_folder(b.id, schemas.FolderPatch(parent_id=None, position=1), session)

    roots = [f for f in routes.list_folders(session) if f.parent_id is None]
    assert [(f.name, f.position) for f in roots] == [("Quissac", 0), ("B", 1)]


def test_a_group_already_holding_a_tie_comes_out_dense(session: Session):
    """Ranks are recomputed on every write rather than incremented, so a group that
    starts out with two folders on the same rank comes back dense instead of carrying
    the tie forward. A rank only has to be unique among siblings, which is why reading
    a dump without `parent_id` is no way to judge one."""
    first, second = _folder(session, "A"), _folder(session, "B")
    for stale in (session.get(Folder, first.id), session.get(Folder, second.id)):
        assert stale is not None
        stale.position = 1
        session.add(stale)
    session.commit()

    _folder(session, "C")

    assert [(f.name, f.position) for f in routes.list_folders(session)] == [
        ("A", 0), ("B", 1), ("C", 2)
    ]


def test_deleting_a_parent_ranks_its_children_at_the_root(session: Session):
    a = _folder(session, "A")
    site = _folder(session, "Site")
    _folder(session, "Child", site.id)
    routes.delete_folder(site.id, session)
    assert _names(session) == [a.name, "Child"]
    assert [f.position for f in routes.list_folders(session)] == [0, 1]


# --------------------------------------------------------------------------- #
# What is in them
# --------------------------------------------------------------------------- #

def test_a_rush_is_filed_and_taken_back_out(session: Session, sequence: Sequence):
    folder = _folder(session, "Pierrevert")
    filed = routes.update_sequence(sequence.id, label=None, derushed=None, folder_id=folder.id, session=session)
    assert filed.folder_id == folder.id

    # 0 rather than null: a query parameter has no way to say "set this to nothing"
    # that differs from leaving it out.
    freed = routes.update_sequence(sequence.id, label=None, derushed=None, folder_id=0, session=session)
    assert freed.folder_id is None


def test_the_count_is_what_sits_directly_in_the_folder(session: Session, sequence: Sequence):
    """A parent showing the total would count what the child already shows."""
    site = _folder(session, "Pierrevert")
    child = _folder(session, "August", site.id)
    routes.update_sequence(sequence.id, label=None, derushed=None, folder_id=child.id, session=session)

    listed = {f.name: f.sequence_count for f in routes.list_folders(session)}
    assert listed == {"August": 1, "Pierrevert": 0}


def test_deleting_a_folder_keeps_the_rushes(session: Session, sequence: Sequence):
    """A folder holds no footage, so there is nothing here that can be lost."""
    site = _folder(session, "Pierrevert")
    child = _folder(session, "August", site.id)
    routes.update_sequence(sequence.id, label=None, derushed=None, folder_id=site.id, session=session)

    result = routes.delete_folder(site.id, session)

    assert result == {"deleted": "Pierrevert", "rushes_freed": 1, "folders_freed": 1}
    assert session.get(Sequence, sequence.id) is not None
    assert session.get(Sequence, sequence.id).folder_id is None
    assert session.get(Folder, child.id).parent_id is None


def test_filing_into_a_folder_that_does_not_exist_is_refused(
    session: Session, sequence: Sequence
):
    with pytest.raises(HTTPException) as raised:
        routes.update_sequence(sequence.id, label=None, derushed=None, folder_id=4242, session=session)
    assert raised.value.status_code == 404
