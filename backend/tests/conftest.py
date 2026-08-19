"""A real SQLite database in a temporary directory, per test.

The application settings are a module-level singleton, and `db.py` caches its
engine in a global. Both are pointed at the test's own `tmp_path` and reset
afterwards, so no test can see another's rows or leave files in the real volume.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from app import db
from app.config import settings
from app.models import Clip, ClipState, Sequence, SequenceState


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:  # type: ignore[misc]
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(db, "_engine", None)
    settings.ensure_dirs()
    db.init_db()
    with Session(db.get_engine()) as opened:
        yield opened
    db._engine = None


@pytest.fixture
def sequence(session: Session) -> Sequence:
    """A two-part sequence whose raw files really exist on disk.

    Real files because several behaviours turn on them: `prepare` refuses a
    sequence with nothing to merge, and purging the parts after a merge is
    supposed to delete them.
    """
    seq = Sequence(
        key="DJI_20260819_120000_0001_D",
        label="test",
        content_hash="abc123",
        state=SequenceState.NEW,
        part_count=2,
        width=3840,
        height=2880,
        fps_num=60000,
        fps_den=1001,
        duration_ms=240_000.0,
        size_bytes=8_000_000_000,
    )
    session.add(seq)
    session.commit()
    session.refresh(seq)

    for index in range(2):
        name = f"DJI_20260819_120000_000{index + 1}_D.MP4"
        path = settings.raw_dir / name
        path.write_bytes(b"not really a rush")
        session.add(
            Clip(
                sequence_id=seq.id,
                part_index=index,
                filename=name,
                raw_path=f"raw/{name}",
                fingerprint=f"fp{index}",
                state=ClipState.INGESTED,
                width=3840,
                height=2880,
                fps_num=60000,
                fps_den=1001,
                duration_ms=120_000.0,
            )
        )
    session.commit()
    return seq
