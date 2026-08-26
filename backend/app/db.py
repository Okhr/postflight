from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import settings
from .services import backup

log = logging.getLogger(__name__)

_engine: Engine | None = None


def get_engine() -> Engine:
    """SQLite engine in WAL mode.

    One process ever writes here: the dispatcher owns the database and a worker
    never opens it, so the concurrency a server database buys is concurrency
    nobody needs. WAL and a busy_timeout cover the API's own threads.

    That single owner is also what lets the whole volume sit on a network share.
    WAL needs a `-shm` file mapped by every process that opens the database, and
    SQLite's docs say plainly that this does not work over a network filesystem.
    Measured on NFSv4 it does, for one client: the mapping comes off the share
    and a commit costs 5.6 ms against 0.01 ms locally. The docs are about two
    client machines, and there the second one is refused outright with a disk I/O
    error rather than corrupting anything.
    """
    global _engine
    if _engine is None:
        settings.ensure_dirs()
        _adopt_legacy_db()
        _apply_pending_restore()
        _engine = create_engine(
            f"sqlite:///{settings.db_path}",
            connect_args={"timeout": 30.0, "check_same_thread": False},
            pool_pre_ping=True,
        )

        @event.listens_for(_engine, "connect")
        def _set_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return _engine


def _apply_pending_restore() -> None:
    """Put a staged snapshot in place of the database, before anything opens it.

    The `-wal` and `-shm` of the outgoing database have to go, and that is the whole
    reason this is not a two-line copy: SQLite would replay a stale WAL into the
    restored file, which is corruption rather than a restore. They belong to the file
    being replaced and mean nothing next to its successor.

    Nothing is kept aside here. `backup.stage_restore` takes a snapshot of the current
    state before staging, so the copy that matters already exists, whole, in the
    backups directory. A half file with no WAL beside it would be a worse safety net
    than none, because it would look like one.
    """
    pending = backup.pending_path()
    if not pending.is_file():
        return
    db = settings.db_path
    for suffix in ("", "-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    pending.rename(db)
    log.warning("Database restored from a staged snapshot")


def _adopt_legacy_db() -> None:
    """Take over the database this project wrote under its former name.

    The three files move together or not at all: the WAL holds committed
    transactions the main file does not have yet, so carrying it over alone
    would silently roll the database back. Nothing has opened it at this point,
    which is the only moment a rename is safe.
    """
    legacy = settings.db_path.with_name("video-stab.sqlite3")
    if settings.db_path.exists() or not legacy.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        source = legacy.with_name(legacy.name + suffix)
        if source.exists():
            source.rename(settings.db_path.with_name(settings.db_path.name + suffix))
    log.info("Database adopted from %s", legacy.name)


def _scalar_default(column) -> Any | None:  # noqa: ANN001
    """The constant a new column should be filled with, when there is one.

    A callable default (`created_at`) cannot be written into DDL, so it comes back
    None and the column is left null on the rows that predate it.
    """
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    return default.arg


def _add_missing_columns(engine: Engine) -> None:
    """Add the columns that appeared since the database was created, and fill them.

    `create_all` does nothing to an existing table. Rather than pulling in
    Alembic for a homelab app, we diff against the declared schema and add what
    is missing. SQLite takes `ALTER TABLE ADD COLUMN` without rewriting the table.

    The `DEFAULT` clause is the part that is easy to leave out and expensive to
    leave out: without it SQLite fills the existing rows with null, and a column the
    model declares as an int then reads back as None, which fails validation on the
    way out rather than on the way in. Measured on 2026-08-20 with `folder.position`.
    So every non-nullable column with a constant default also gets a backfill pass,
    which repairs the rows an earlier run of this function left null.
    """
    from sqlalchemy import inspect, literal, text

    def render(value: Any, type_: Any) -> str:
        """The value as SQL, bound to the column's own type.

        Without the type, an enum member has no renderer at all ("No literal value
        renderer is available for literal value <GradeState.DRAFT: 'draft'>"), and a
        string would go in unquoted.
        """
        return str(
            literal(value, type_=type_).compile(
                dialect=engine.dialect, compile_kwargs={"literal_binds": True}
            )
        )

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.primary_key:
                    continue
                filler = _scalar_default(column)

                if column.name not in present:
                    if not column.nullable and filler is None:
                        log.warning(
                            "column %s.%s is not nullable and has no constant default: "
                            "manual migration needed",
                            table.name, column.name,
                        )
                        continue
                    ddl = column.type.compile(engine.dialect)
                    clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}'
                    if filler is not None:
                        clause += f" DEFAULT {render(filler, column.type)}"
                    connection.execute(text(clause))
                    log.info("Column added: %s.%s", table.name, column.name)

                if not column.nullable and filler is not None:
                    filled = connection.execute(
                        text(
                            f'UPDATE "{table.name}" SET "{column.name}" = {render(filler, column.type)} '
                            f'WHERE "{column.name}" IS NULL'
                        )
                    ).rowcount
                    if filled:
                        log.info(
                            "Backfilled %d row(s) of %s.%s", filled, table.name, column.name
                        )


def _relax_grade_render_index(engine: Engine) -> None:
    """`grade.render_id` was unique while a clip had exactly one grade.

    A clip now holds several, side by side. SQLite cannot alter an index in place, and
    `create_all` never touches one that already exists, so the old unique index has to
    be dropped here and recreated plain. Named rather than general on purpose: this is
    one schema change that happened once, not a rule about indexes.
    """
    from sqlalchemy import text

    with engine.begin() as connection:
        found = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_grade_render_id'")
        ).scalar()
        if not found or "UNIQUE" not in found.upper():
            return
        connection.execute(text("DROP INDEX ix_grade_render_id"))
        connection.execute(text("CREATE INDEX ix_grade_render_id ON grade (render_id)"))
    log.info("Index relaxed: grade.render_id is no longer unique")


# Columns whose meaning died, with the day it did. The rest of this file only ever
# adds, so without this they would sit there for ever; SQLite can drop a column since
# 3.35 (the image ships 3.46). Listed one by one on purpose: a general "drop what the
# model no longer declares" would delete a column the day someone forgets to declare it.
DEAD_COLUMNS = (
    # A rush carried a colour pill until 2026-08-20, when only folders kept one.
    ("sequence", "color"),
    # The clip's measurement moved onto `render` on 2026-08-25: it describes the clip
    # and not the look, and a clip now holds several grades.
    ("grade", "analysis"),
    # The derush timeline stopped drawing a filmstrip when the gyro curve took the
    # room, and on 2026-08-26 the whole thing went: an ffmpeg pass and 190 to 290 kB
    # per proxy for a picture no page asked for.
    ("sequence", "filmstrip_path"),
    # Per-render deviations from a profile. Wired end to end and never populated: two
    # variants of a look are two profiles, which is what makes a render reproducible.
    ("render", "overrides"),
)


def _drop_dead_columns(engine: Engine) -> None:
    """Drop the columns nothing reads any more, once each."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, column in DEAD_COLUMNS:
        if table not in tables:
            continue
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        try:
            with engine.begin() as connection:
                connection.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"'))
        except Exception as exc:  # noqa: BLE001 (a schema tidy-up must never stop a boot)
            log.warning("Column %s.%s could not be dropped: %s", table, column, exc)
            continue
        log.info("Column dropped: %s.%s", table, column)


def init_db() -> None:
    from . import models  # noqa: F401  (registers the tables)

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _relax_grade_render_index(engine)
    _drop_dead_columns(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(get_engine()) as session:
        yield session
