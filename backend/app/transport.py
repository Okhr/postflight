"""The worker's own copy of the data volume, when it does not share the dispatcher's.

This module exists only for the remote case, and that is enforced by construction:
`Workspace` is instantiated only when registration came back with `shares_data: false`.
It has to be, because `evict()` deletes footage. Pointed at the dispatcher's volume it
would delete the masters, which is the one bug in this file worth designing against
rather than testing for.

The shape of a remote job:

    pull the inputs this machine does not already have
    run the job exactly as a local worker would
    send back whatever the job wrote

The middle step is untouched: `executor.py` resolves relative paths against its own
`data_dir`, so a spec runs identically on a worker holding its own copy and on one
reading the dispatcher's. That is what the relative-path convention bought.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from .config import settings
from .paths import to_absolute

log = logging.getLogger(__name__)

MB = 1 << 20

# Directories that are never anyone's input and never anyone's output. `tmp` holds
# scratch files by the ton, `db` belongs to the dispatcher alone, and the inbox is
# watched by the API on a volume a worker does not see anyway.
PRIVATE_DIRS = {"tmp", "db", "inbox"}

# What eviction is allowed to delete: footage, which is all of the weight. Templates
# and project files are a few kilobytes of JSON and deleting them would buy nothing.
EVICTABLE_DIRS = {"raw", "merged", "out", "graded", "proxies"}

Progress = Callable[..., None]


def _walk() -> dict[str, tuple[float, int]]:
    """Every file that could be an input or an output, by relative path.

    The value is (mtime, size), which is what makes a change detectable.
    """
    root = settings.data_dir
    seen: dict[str, tuple[float, int]] = {}
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if not rel.parts or rel.parts[0] in PRIVATE_DIRS:
                continue
            stat = path.stat()
        except OSError:
            continue
        seen[str(rel)] = (stat.st_mtime, stat.st_size)
    return seen


class Workspace:
    """Files in and files out, for a worker that holds a copy rather than the original."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ----------------------------------------------------------------- inputs

    def _have(self, item: dict[str, Any]) -> bool:
        """Whether this machine already holds that input.

        Path and size, and that is enough: nothing in this pipeline is ever rewritten
        in place, so a path that matches is the same bytes. See `api/blobs.py`.
        """
        local = to_absolute(item.get("path"))
        if local is None or not local.is_file():
            return False
        expected = int(item.get("bytes") or 0)
        return not expected or local.stat().st_size == expected

    def pull(self, inputs: list[dict[str, Any]], progress: Progress) -> dict[str, tuple[float, int]]:
        """Fetch what is missing, then return a snapshot to compare against later."""
        missing = [item for item in inputs if item.get("path") and not self._have(item)]
        if missing:
            wanted = sum(int(item.get("bytes") or 0) for item in missing)
            # The inputs this job already holds are not candidates for eviction: a
            # two-part merge whose first part is cached would otherwise have it
            # deleted to make room for the second.
            freed = self.evict(wanted, keep={item["path"] for item in inputs if item.get("path")})
            if freed:
                log.info("Cache: %.1f GB freed to make room", freed / (1 << 30))
            for item in missing:
                self._download(item, progress)

        # Taken *after* fetching, so a freshly pulled input can never be mistaken for
        # something the job produced and shipped straight back where it came from.
        return _walk()

    def _download(self, item: dict[str, Any], progress: Progress) -> None:
        rel = item["path"]
        dest = to_absolute(rel)
        assert dest is not None
        expected = int(item.get("bytes") or 0)

        def on_bytes(received: int) -> None:
            # Progress stays at zero and the message carries the transfer. A bar that
            # ran to full and then restarted at nothing would read as a job redone.
            progress(0.0, f"fetching {dest.name} ({received / MB:.0f}/{expected / MB:.0f} MB)")

        log.info("Fetching %s (%.1f MB)", rel, expected / MB)
        self._client.download(rel, dest, on_bytes)

    # ---------------------------------------------------------------- outputs

    def publish(self, before: dict[str, tuple[float, int]], progress: Progress) -> list[str]:
        """Send back everything the job wrote.

        Compared against a snapshot rather than against a timestamp, because mtime
        granularity is a filesystem's business and both ways of getting this wrong are
        bad: miss an output and the dispatcher records a path with no file behind it;
        count an input as an output and a 4 GB master goes back where it came from.

        Whatever the job created gets sent, not just the paths the result mentions. The
        proxy step alone writes a poster and a gyro chart that no result
        field names, and every one of them is something the interface later reads.
        """
        produced = sorted(rel for rel, fact in _walk().items() if before.get(rel) != fact)
        for rel in produced:
            source = to_absolute(rel)
            if source is None or not source.is_file():
                continue
            size = source.stat().st_size
            progress(1.0, f"sending {source.name} ({size / MB:.0f} MB)")
            log.info("Sending %s (%.1f MB)", rel, size / MB)
            self._client.upload(rel, source)
        return produced

    # ------------------------------------------------------------------ cache

    def cached(self, limit: int = 200) -> list[str]:
        """What this machine holds, so the dispatcher can price a job honestly.

        Without this, a second cut of a sequence already fetched would be scored as a
        4 GB transfer and handed to a slower machine that happens to have the file.
        """
        held = [rel for rel in _walk() if rel.split("/")[0] in ("raw", "merged", "out")]
        return sorted(held)[:limit]

    def evict(self, needed: int, keep: set[str] | None = None) -> int:
        """Delete the oldest footage until `needed` more bytes fit under the cap.

        Oldest by mtime, not by access time: `relatime` refreshes atime once a day at
        most, so a least-recently-used policy built on it would be a coin toss dressed
        up as a heuristic.

        Only ever reachable on a worker that holds its own copy, because that is the
        only case in which a `Workspace` exists at all. On the dispatcher's volume this
        would be deleting the originals.
        """
        cap = settings.worker_cache_bytes
        if cap <= 0:
            return 0

        root = settings.data_dir
        spared = keep or set()
        files: list[tuple[float, int, Path]] = []
        total = 0
        for rel, (mtime, size) in _walk().items():
            total += size
            if rel in spared or rel.split("/")[0] not in EVICTABLE_DIRS:
                continue
            files.append((mtime, size, root / rel))

        freed = 0
        for mtime, size, path in sorted(files):
            if total + needed <= cap:
                break
            path.unlink(missing_ok=True)
            total -= size
            freed += size
            log.info("Cache: %s dropped (%.1f MB)", path.name, size / MB)
        return freed
