from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

log = logging.getLogger(__name__)

_engine: Engine | None = None


def get_engine() -> Engine:
    """SQLite engine in WAL mode: the API and the worker are two processes
    writing to the same database, and WAL + busy_timeout is enough at this scale."""
    global _engine
    if _engine is None:
        settings.ensure_dirs()
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


def init_db() -> None:
    from . import models  # noqa: F401  (registers the tables)

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)


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
