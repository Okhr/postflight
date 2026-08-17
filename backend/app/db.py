from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

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


def _add_missing_columns(engine: Engine) -> None:
    """Add the columns that appeared since the database was created.

    `create_all` does nothing to an existing table. Rather than pulling in
    Alembic for a homelab app, we diff against the declared schema and add what
    is missing — SQLite takes `ALTER TABLE ADD COLUMN` without rewriting the table.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present or column.primary_key:
                    continue
                if not column.nullable and column.default is None:
                    log.warning(
                        "column %s.%s is not nullable and has no default: manual migration needed",
                        table.name, column.name,
                    )
                    continue
                ddl = column.type.compile(engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')
                )
                log.info("Column added: %s.%s", table.name, column.name)


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
