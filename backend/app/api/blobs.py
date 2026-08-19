"""Files, for the workers that do not share the dispatcher's volume.

A worker on another machine sees none of the rushes. So a job's inputs travel to it
before the work and its outputs travel back after, over the same HTTP channel the
queue already uses. Nothing new to open, nothing new to authenticate.

Addressed by their **relative path**, not by a content hash. The hash would be the
textbook answer and it is the wrong one here: it would mean reading 4 GB to name a
file the dispatcher already knows the name of. Paths are safe as identities in this
codebase because nothing is ever rewritten in place. A merged master carries the
sequence's stem, a graded file carries the hash of its look, a render carries its
template and cut. Same path always means same bytes, so the worker's cache checks a
path and a size and is right.

Two directions, two different permissions, on purpose. A worker may read anything
that is footage and may write only into the directories the pipeline produces: it has
no business writing to the inbox, and the dispatcher's database is not a file anybody
sends anywhere.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from ..config import settings
from .worker_api import require_worker_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["blobs"], dependencies=[Depends(require_worker_token)])

# Where a worker may put a file. Everything the pipeline produces, and nothing else.
WRITABLE = ("merged", "proxies", "out", "graded", "projects")
# What a worker has no reason to read. `db` above all: the queue is served over the
# endpoints above, never by shipping the SQLite file.
UNREADABLE = ("db", "tmp", "inbox")


def _resolve(rel: str, for_write: bool) -> Path:
    """Turn a relative path from a worker into an absolute one we are willing to touch."""
    root = settings.data_dir.resolve()
    try:
        resolved = (root / rel).resolve()
    except OSError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unusable path: {exc}") from exc

    # Resolved first, then checked: `..` and a symlink out of the volume both end up
    # somewhere outside, and this catches them the same way.
    if not resolved.is_relative_to(root):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "outside the data volume")
    parts = resolved.relative_to(root).parts
    if not parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no path given")

    top = parts[0]
    if for_write and top not in WRITABLE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"a worker may not write into {top}/")
    if not for_write and top in UNREADABLE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"a worker may not read {top}/")
    return resolved


@router.get("/blobs/{rel:path}")
def download(rel: str) -> FileResponse:
    path = _resolve(rel, for_write=False)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such file: {rel}")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.put("/blobs/{rel:path}")
async def upload(rel: str, request: Request) -> dict:
    """Take a file a worker produced.

    Written beside its final name and renamed only once the last byte has arrived, so
    a transfer cut off in the middle can never be mistaken for a finished master. That
    matters more here than it would locally: the file being sent is the one the next
    step of the pipeline will read.
    """
    path = _resolve(rel, for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")

    written = 0
    try:
        with partial.open("wb") as sink:
            async for chunk in request.stream():
                sink.write(chunk)
                written += len(chunk)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(path)
    log.info("Received %s (%.1f MB)", rel, written / (1 << 20))
    return {"path": rel, "bytes": written}
