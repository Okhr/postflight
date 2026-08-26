"""Snapshots of the database, and putting one back.

The interesting case is not that a copy exists, it is what happens around it: a stale
`-wal` left next to a restored file is corruption rather than a restore, and a retention
pass that runs at the wrong moment can delete the snapshot being restored from.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import db as db_module
from app.config import settings
from app.services import backup


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A database on disk, in WAL, with rows in it, reached through the app's engine."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(db_module, "_engine", None)
    settings.ensure_dirs()
    engine = db_module.get_engine()
    with engine.connect() as con:
        con.exec_driver_sql("CREATE TABLE t(id INTEGER PRIMARY KEY, who TEXT)")
        con.exec_driver_sql("INSERT INTO t(who) VALUES ('before')")
        con.commit()
    yield engine
    engine.dispose()
    monkeypatch.setattr(db_module, "_engine", None)


def rows(path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT who FROM t ORDER BY id")]
    finally:
        con.close()


def test_a_snapshot_is_a_readable_database_with_the_same_rows(live):
    snapshot = backup.make()

    path = settings.backups_dir / snapshot.name
    assert rows(path) == ["before"]
    assert snapshot.size_bytes > 0


def test_a_snapshot_has_no_sidecars_to_carry_around(live):
    """The whole reason for `VACUUM INTO` over a file copy: what lands on the share is
    one self-contained file, so copying it somewhere else cannot lose half of it."""
    backup.make()

    assert sorted(p.name.split(".")[-1] for p in settings.backups_dir.iterdir()) == ["sqlite3"]


def test_a_snapshot_taken_during_an_open_write_transaction_excludes_it(live):
    """So a snapshot never has to wait for the pipeline to be idle."""
    with live.connect() as writing:
        writing.exec_driver_sql("INSERT INTO t(who) VALUES ('uncommitted')")
        snapshot = backup.make()
        writing.rollback()

    assert rows(settings.backups_dir / snapshot.name) == ["before"]


def test_retention_keeps_the_newest_and_deletes_the_rest(live, monkeypatch):
    monkeypatch.setattr(settings, "backup_keep", 2)
    made = []
    for hour in range(4):
        stamp = datetime(2026, 8, 26, hour, tzinfo=timezone.utc).strftime(backup.STAMP)
        monkeypatch.setattr(backup, "_stamped", lambda _t="", s=stamp: f"{backup.PREFIX}{s}{backup.SUFFIX}")
        made.append(backup.make().name)

    assert [s.name for s in backup.listing()] == [made[3], made[2]]


def test_retention_never_touches_a_file_it_did_not_write(live, monkeypatch):
    monkeypatch.setattr(settings, "backup_keep", 1)
    stray = settings.backups_dir / "notes.txt"
    stray.write_text("mine")

    backup.make()
    backup.prune()

    assert stray.exists()


def test_a_name_that_is_not_a_snapshot_name_is_refused(live):
    """The name arrives from a URL, so traversal is covered by refusing anything this
    module did not write, rather than by reasoning about separators."""
    backup.make()

    for name in ["../../etc/passwd", "postflight-../x.sqlite3", "other.sqlite3", ""]:
        with pytest.raises(ValueError):
            backup.resolve(name)


def test_a_staged_restore_replaces_the_database_at_startup(live, monkeypatch):
    snapshot = backup.make()
    with live.connect() as con:  # a change made after the snapshot, which must vanish
        con.exec_driver_sql("INSERT INTO t(who) VALUES ('after')")
        con.commit()
    assert rows(settings.db_path) == ["before", "after"]

    backup.stage_restore(snapshot.name)
    live.dispose()
    db_module._apply_pending_restore()

    assert rows(settings.db_path) == ["before"]
    assert not backup.pending_path().exists()


def test_the_stale_wal_of_the_replaced_database_is_removed(live, monkeypatch):
    """The one way a restore can corrupt rather than restore.

    A `-wal` belongs to the file it was written beside. Left next to a restored
    database, SQLite replays it into a file it was never part of.
    """
    snapshot = backup.make()
    with live.connect() as con:
        con.exec_driver_sql("INSERT INTO t(who) VALUES ('after')")
        con.commit()
    wal = settings.db_path.with_name(settings.db_path.name + "-wal")
    assert wal.exists(), "expected a WAL beside a live WAL-mode database"

    backup.stage_restore(snapshot.name)
    live.dispose()
    db_module._apply_pending_restore()

    assert not wal.exists()
    assert rows(settings.db_path) == ["before"]


def test_staging_a_restore_snapshots_what_it_is_about_to_replace(live):
    snapshot = backup.make()
    with live.connect() as con:
        con.exec_driver_sql("INSERT INTO t(who) VALUES ('after')")
        con.commit()

    safety = backup.stage_restore(snapshot.name)

    assert rows(settings.backups_dir / safety.name) == ["before", "after"]


def test_the_snapshot_being_restored_survives_a_tight_retention(live, monkeypatch):
    """The safety snapshot runs a retention pass, and with a full directory that pass
    can delete the oldest file, which may be the one being restored from."""
    monkeypatch.setattr(settings, "backup_keep", 1)
    oldest = backup.make()

    backup.stage_restore(oldest.name)
    live.dispose()
    db_module._apply_pending_restore()

    assert rows(settings.db_path) == ["before"]


def test_nothing_is_applied_when_nothing_is_staged(live):
    before = settings.db_path.read_bytes()

    db_module._apply_pending_restore()

    assert settings.db_path.read_bytes() == before


def test_the_schedule_is_measured_against_the_newest_snapshot(live, monkeypatch):
    assert backup.due(24.0) is True, "no snapshot yet, so one is owed"

    backup.make()
    assert backup.due(24.0) is False, "just taken, so nothing is owed"

    old = datetime.now(timezone.utc) - timedelta(hours=30)
    stamp = old.strftime(backup.STAMP)
    for path in settings.backups_dir.iterdir():
        path.rename(path.with_name(f"{backup.PREFIX}{stamp}{backup.SUFFIX}"))
    assert backup.due(24.0) is True


def test_a_zero_interval_turns_the_schedule_off(live):
    assert backup.due(0) is False
