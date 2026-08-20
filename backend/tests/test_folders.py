"""Drawers for rushes: two levels, and nothing in them is ever at risk.

These call the route functions with a session rather than going through HTTP: the
rules worth pinning down are about the shape of the tree and about what survives a
deletion, and neither of those is about serialization.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.api import routes, schemas
from app.models import Clip, Folder, Sequence, SequenceState
from app.pipeline import group_clips_into_sequences


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
# What is in them
# --------------------------------------------------------------------------- #

def test_a_rush_is_filed_and_taken_back_out(session: Session, sequence: Sequence):
    folder = _folder(session, "Pierrevert")
    filed = routes.update_sequence(sequence.id, label=None, color=None, folder_id=folder.id, session=session)
    assert filed.folder_id == folder.id

    # 0 rather than null: a query parameter has no way to say "set this to nothing"
    # that differs from leaving it out.
    freed = routes.update_sequence(sequence.id, label=None, color=None, folder_id=0, session=session)
    assert freed.folder_id is None


def test_the_count_is_what_sits_directly_in_the_folder(session: Session, sequence: Sequence):
    """A parent showing the total would count what the child already shows."""
    site = _folder(session, "Pierrevert")
    child = _folder(session, "August", site.id)
    routes.update_sequence(sequence.id, label=None, color=None, folder_id=child.id, session=session)

    listed = {f.name: f.sequence_count for f in routes.list_folders(session)}
    assert listed == {"August": 1, "Pierrevert": 0}


def test_deleting_a_folder_keeps_the_rushes(session: Session, sequence: Sequence):
    """A folder holds no footage, so there is nothing here that can be lost."""
    site = _folder(session, "Pierrevert")
    child = _folder(session, "August", site.id)
    routes.update_sequence(sequence.id, label=None, color=None, folder_id=site.id, session=session)

    result = routes.delete_folder(site.id, session)

    assert result == {"deleted": "Pierrevert", "rushes_freed": 1, "folders_freed": 1}
    assert session.get(Sequence, sequence.id) is not None
    assert session.get(Sequence, sequence.id).folder_id is None
    assert session.get(Folder, child.id).parent_id is None


def test_filing_into_a_folder_that_does_not_exist_is_refused(
    session: Session, sequence: Sequence
):
    with pytest.raises(HTTPException) as raised:
        routes.update_sequence(sequence.id, label=None, color=None, folder_id=4242, session=session)
    assert raised.value.status_code == 404


# --------------------------------------------------------------------------- #
# Taking a merge apart
# --------------------------------------------------------------------------- #

def test_splitting_gives_one_sequence_per_part(session: Session, sequence: Sequence):
    parts = routes.split_sequence(sequence.id, session=session)

    assert len(parts) == 2
    assert all(p.part_count == 1 for p in parts)
    # The joined row is gone. Not asserted by id: SQLite hands the freed rowid to the
    # first part, so the old id still resolves, to a different sequence.
    remaining = session.exec(select(Sequence)).all()
    assert [s.part_count for s in remaining] == [1, 1]


def test_the_parts_are_never_left_loose(session: Session, sequence: Sequence):
    """The reason this endpoint exists rather than a plain delete: loose contiguous
    clips are exactly what the scan groups, so letting go of them would put the merge
    straight back on the next tick."""
    routes.split_sequence(sequence.id, session=session)

    assert session.exec(select(Clip).where(Clip.sequence_id.is_(None))).all() == []  # type: ignore[union-attr]
    assert group_clips_into_sequences(session) == []


def test_a_split_keeps_the_folder_the_rush_was_in(session: Session, sequence: Sequence):
    folder = _folder(session, "Pierrevert")
    routes.update_sequence(sequence.id, label=None, color=None, folder_id=folder.id, session=session)

    parts = routes.split_sequence(sequence.id, session=session)

    assert [p.folder_id for p in parts] == [folder.id, folder.id]


def test_joining_them_back_puts_the_rush_where_it_was(session: Session, sequence: Sequence):
    """A join is the inverse of a split, and the split keeps the folder. Measured on
    2026-08-20: it did not, so taking a group apart and putting it back emptied the
    drawer without saying so."""
    folder = _folder(session, "Pierrevert")
    routes.update_sequence(sequence.id, label=None, color=None, folder_id=folder.id, session=session)
    parts = routes.split_sequence(sequence.id, session=session)

    joined = routes.regroup(
        schemas.RegroupRequest(sequence_ids=[p.id for p in parts], force=True), session
    )

    assert joined.part_count == 2
    assert joined.folder_id == folder.id


def test_a_single_part_rush_has_nothing_to_split(session: Session, sequence: Sequence):
    parts = routes.split_sequence(sequence.id, session=session)
    with pytest.raises(HTTPException) as raised:
        routes.split_sequence(parts[0].id, session=session)
    assert raised.value.status_code == 400


def test_a_ready_rush_is_not_taken_apart_without_being_told_twice(
    session: Session, sequence: Sequence
):
    """Same guard as regrouping: work has been produced from it."""
    sequence.state = SequenceState.READY
    session.add(sequence)
    session.commit()

    with pytest.raises(HTTPException) as raised:
        routes.split_sequence(sequence.id, session=session)
    assert raised.value.status_code == 409

    assert len(routes.split_sequence(sequence.id, force=True, session=session)) == 2
