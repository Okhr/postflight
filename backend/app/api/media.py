"""File serving with Range request support.

Indispensable : sans `Accept-Ranges`/`206 Partial Content`, un `<video>` ne peut
cannot seek inside the file, which makes frame-by-frame derushing impossible.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse

CHUNK_SIZE = 1 << 18  # 256 Kio
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def _guess_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _iter_range(path: Path, start: int, end: int):
    remaining = end - start + 1
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def serve_file(path: Path, request: Request, download_name: str | None = None) -> Response:
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"fichier absent : {path.name}")

    file_size = path.stat().st_size
    media_type = _guess_type(path)
    disposition = f'attachment; filename="{download_name}"' if download_name else None

    range_header = request.headers.get("range")
    if not range_header:
        headers = {"Accept-Ranges": "bytes"}
        if disposition:
            headers["Content-Disposition"] = disposition
        return FileResponse(path, media_type=media_type, headers=headers)

    match = _RANGE.fullmatch(range_header.strip())
    if not match:
        raise HTTPException(status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, "range illisible")

    raw_start, raw_end = match.groups()
    if raw_start:
        start = int(raw_start)
        end = int(raw_end) if raw_end else file_size - 1
    elif raw_end:
        # bytes=-N: the last N bytes
        start = max(file_size - int(raw_end), 0)
        end = file_size - 1
    else:
        raise HTTPException(status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, "range vide")

    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }
    if disposition:
        headers["Content-Disposition"] = disposition
    return StreamingResponse(
        _iter_range(path, start, end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers=headers,
    )
