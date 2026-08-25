"""Adding a column to a database that already has rows in it.

`create_all` never touches an existing table, so `_add_missing_columns` is the whole
migration story here. What it has to get right is not the ALTER, it is what lands in
the rows that predate the column: SQLite fills them with null, and a column the model
declares as an int then reads back as None and fails on the way out.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, inspect, text
from sqlmodel import SQLModel, create_engine

from app.db import _add_missing_columns, _relax_grade_render_index


def _engine(tmp_path):  # type: ignore[no-untyped-def]
    return create_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")


def _table(name: str, *columns: Column) -> Table:  # type: ignore[type-arg]
    """A standalone table in its own metadata, so tests never touch the real schema."""
    return Table(name, MetaData(), Column("id", Integer, primary_key=True), *columns)


def _declare(monkeypatch, table: Table) -> None:
    """Make `_add_missing_columns` see this table and nothing else.

    `sorted_tables` is read-only, so the whole `metadata` is swapped instead.
    """
    monkeypatch.setattr(SQLModel, "metadata", table.metadata)


def test_a_new_column_with_a_default_fills_the_existing_rows(tmp_path, monkeypatch):
    """The bug of 2026-08-20: `folder.position` was added without a DEFAULT clause, so
    the folder already in the database read back with position None and the folder list
    answered 500."""
    engine = _engine(tmp_path)
    before = _table("thing")
    before.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text('INSERT INTO thing (id) VALUES (1)'))

    after = _table("thing", Column("rank", Integer, nullable=False, default=0))
    _declare(monkeypatch, after)
    _add_missing_columns(engine)

    with engine.begin() as connection:
        assert connection.execute(text("SELECT rank FROM thing WHERE id = 1")).scalar() == 0


def test_a_null_left_by_an_earlier_run_is_repaired(tmp_path, monkeypatch):
    """The column is already there and already null, which is the state a database
    upgraded by the broken version is in. Restarting has to be enough to fix it."""
    engine = _engine(tmp_path)
    broken = _table("thing", Column("rank", Integer))
    broken.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO thing (id, rank) VALUES (1, NULL)"))

    after = _table("thing", Column("rank", Integer, nullable=False, default=0))
    _declare(monkeypatch, after)
    _add_missing_columns(engine)

    with engine.begin() as connection:
        assert connection.execute(text("SELECT rank FROM thing WHERE id = 1")).scalar() == 0


def test_a_string_default_is_quoted(tmp_path, monkeypatch):
    """Rendered through SQLAlchemy rather than by hand, so a quote in the value cannot
    end the statement early."""
    engine = _engine(tmp_path)
    _table("thing").metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO thing (id) VALUES (1)"))

    after = _table("thing", Column("tag", String, nullable=False, default="it's fine"))
    _declare(monkeypatch, after)
    _add_missing_columns(engine)

    with engine.begin() as connection:
        assert connection.execute(text("SELECT tag FROM thing")).scalar() == "it's fine"


def test_an_enum_default_is_rendered(tmp_path, monkeypatch):
    """Found by it crashing the API on startup: `literal()` with no type has no
    renderer for an enum member, and `grade.state` has one as its default."""
    import enum

    from sqlalchemy import Enum

    class State(str, enum.Enum):
        DRAFT = "draft"
        DONE = "done"

    engine = _engine(tmp_path)
    _table("thing").metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO thing (id) VALUES (1)"))

    after = _table(
        "thing", Column("state", Enum(State), nullable=False, default=State.DRAFT)
    )
    _declare(monkeypatch, after)
    _add_missing_columns(engine)

    with engine.begin() as connection:
        assert connection.execute(text("SELECT state FROM thing")).scalar() == "DRAFT"


def test_a_nullable_column_is_added_and_left_null(tmp_path, monkeypatch):
    """Null is a value there, not damage: `folder_id` on a rush means Global."""
    engine = _engine(tmp_path)
    _table("thing").metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO thing (id) VALUES (1)"))

    after = _table("thing", Column("note", String, nullable=True))
    _declare(monkeypatch, after)
    _add_missing_columns(engine)

    with engine.begin() as connection:
        assert connection.execute(text("SELECT note FROM thing")).scalar() is None
    assert "note" in {c["name"] for c in inspect(engine).get_columns("thing")}


def test_the_unique_index_on_a_grade_s_clip_is_relaxed(tmp_path):
    """A clip held exactly one grade until 2026-08-25, and the unique index said so.

    SQLite cannot alter an index in place and `create_all` never touches one that
    exists, so the migration has to drop it and put a plain one back. The rows must
    survive: this runs on a database with grades already in it.
    """
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE grade (id INTEGER PRIMARY KEY, render_id INTEGER)"))
        connection.execute(text("CREATE UNIQUE INDEX ix_grade_render_id ON grade (render_id)"))
        connection.execute(text("INSERT INTO grade (id, render_id) VALUES (1, 7)"))

    _relax_grade_render_index(engine)

    with engine.begin() as connection:
        sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE name='ix_grade_render_id'")
        ).scalar()
        assert sql is not None and "UNIQUE" not in sql.upper()
        # Two grades on one clip, which is the whole point.
        connection.execute(text("INSERT INTO grade (id, render_id) VALUES (2, 7)"))
        assert connection.execute(text("SELECT count(*) FROM grade")).scalar() == 2


def test_relaxing_the_index_twice_is_harmless(tmp_path):
    """It runs on every start, so the second time has to be a no-op."""
    engine = _engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE grade (id INTEGER PRIMARY KEY, render_id INTEGER)"))
        connection.execute(text("CREATE INDEX ix_grade_render_id ON grade (render_id)"))

    _relax_grade_render_index(engine)
    _relax_grade_render_index(engine)

    with engine.begin() as connection:
        indexes = connection.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name='ix_grade_render_id'")
        ).scalar()
    assert indexes == 1
