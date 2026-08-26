"""Snapshots of the database, and putting one back.

Copying a live SQLite file is the wrong way to do this: the `-wal` holds committed
transactions the main file does not have yet, so a plain `cp` can hand you a database
that is missing its most recent writes without saying so. `VACUUM INTO` writes a
consistent snapshot from inside a read transaction instead, and it comes out as **one
self-contained file with no sidecars**, which is exactly what belongs on a NAS.

Measured before settling on it: a snapshot taken while another connection holds an open
write transaction succeeds and excludes the uncommitted row. So a snapshot never has to
wait for the pipeline to be idle.

Restoring is deliberate and needs a restart. Replacing the file a running engine has
open would race with whatever request is mid-transaction, so a restore is staged next to
the database and applied by `db._apply_pending_restore` at startup, in the same window
`_adopt_legacy_db` uses: before anything has opened anything.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

log = logging.getLogger(__name__)

PREFIX = "postflight-"
SUFFIX = ".sqlite3"
STAMP = "%Y%m%dT%H%M%SZ"
# Only a name of this exact shape is ever listed, deleted or restored. It is also what
# keeps retention from touching anything else somebody left in the directory.
NAME = re.compile(rf"^{re.escape(PREFIX)}\d{{8}}T\d{{6}}Z(-[a-z]+)?{re.escape(SUFFIX)}$")

# The staged restore, sitting next to the database it is going to replace.
PENDING = "restore.pending"


@dataclass(frozen=True)
class Snapshot:
    name: str
    size_bytes: int
    made_at: datetime


def _dir() -> Path:
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    return settings.backups_dir


def _stamped(tag: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime(STAMP)
    return f"{PREFIX}{stamp}{f'-{tag}' if tag else ''}{SUFFIX}"


def resolve(name: str) -> Path:
    """The path of a snapshot, or an error. Never a path outside the directory.

    The name arrives from an HTTP route, so it is checked against the naming rather
    than sanitised: anything that is not a snapshot name this module wrote is refused,
    which covers traversal without having to reason about it.
    """
    if not NAME.match(name):
        raise ValueError(f"not a snapshot name: {name!r}")
    path = _dir() / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def _describe(path: Path) -> Snapshot:
    """The timestamp comes from the name, not from the filesystem: a copy to a NAS does
    not carry an mtime, and the name is the thing we wrote."""
    stamp = path.name[len(PREFIX):len(PREFIX) + 16]
    made = datetime.strptime(stamp, STAMP).replace(tzinfo=timezone.utc)
    return Snapshot(path.name, path.stat().st_size, made)


def listing() -> list[Snapshot]:
    """Newest first.

    Ordered on the timestamp with the name only as a tiebreak. Sorting on the name
    alone looks equivalent and is not: a tag sorts a snapshot ahead of an untagged one
    from the same second, which is how `make` came to report the wrong file.
    """
    out = [
        _describe(path)
        for path in _dir().iterdir()
        if path.is_file() and NAME.match(path.name)
    ]
    return sorted(out, key=lambda s: (s.made_at, s.name), reverse=True)


def make(tag: str = "") -> Snapshot:
    """Take a snapshot now, verify it, and drop the oldest ones.

    Written to a `.partial` and renamed, like every other file this project produces,
    so a snapshot that appears in the listing is one that finished. And verified before
    the rename: an unchecked backup is a rumour, and `integrity_check` on a database
    this size costs milliseconds.
    """
    from ..db import get_engine  # local: db imports this module for the restore

    target = _dir() / _stamped(tag)
    partial = target.with_suffix(".partial")
    partial.unlink(missing_ok=True)
    try:
        with get_engine().connect() as con:
            con.exec_driver_sql("VACUUM INTO ?", (str(partial),))
        probe = sqlite3.connect(partial)
        try:
            verdict = probe.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            probe.close()
        if verdict != "ok":
            raise RuntimeError(f"snapshot failed its integrity check: {verdict}")
        partial.rename(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    snapshot = _describe(target)
    prune()
    log.info("Snapshot %s (%.1f kB)", snapshot.name, snapshot.size_bytes / 1000)
    return snapshot


def prune(keep: int | None = None) -> list[str]:
    """Delete the oldest snapshots beyond the retention. 0 keeps everything."""
    keep = settings.backup_keep if keep is None else keep
    if keep <= 0:
        return []
    doomed = listing()[keep:]
    for snapshot in doomed:
        (_dir() / snapshot.name).unlink(missing_ok=True)
    if doomed:
        log.info("Snapshots dropped: %s", ", ".join(s.name for s in doomed))
    return [s.name for s in doomed]


def stage_restore(name: str) -> Snapshot:
    """Queue a snapshot to replace the database at the next start.

    A snapshot of the current state is taken first, so the restore itself is
    reversible: the file about to be replaced is the one thing this operation
    destroys, and it should not be the only copy of it.
    """
    source = resolve(name)
    pending = pending_path()
    # Copied before the safety snapshot, not after: that snapshot runs a retention pass,
    # and with a full directory the pass can delete the very file being restored from.
    shutil.copyfile(source, pending)
    try:
        safety = make(tag="prerestore")
    except Exception:
        pending.unlink(missing_ok=True)
        raise
    log.warning("Restore staged from %s, applied on the next start", name)
    return safety


def pending_path() -> Path:
    return settings.db_path.parent / PENDING


def due(interval_h: float) -> bool:
    """Whether the schedule owes a snapshot, judged on what is on disk.

    Against the newest snapshot rather than against process start, so a container that
    restarts twice an hour does not take a snapshot each time and roll the retention
    over in an afternoon.
    """
    if interval_h <= 0:
        return False
    snapshots = listing()
    if not snapshots:
        return True
    age_h = (datetime.now(timezone.utc) - snapshots[0].made_at).total_seconds() / 3600
    return age_h >= interval_h
