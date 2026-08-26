from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import dispatch
from .api.blobs import router as blob_router
from .api.routes import router
from .api.worker_api import router as worker_router
from .config import settings
from .db import init_db, session_scope
from .paths import ensure_volume_id
from .pipeline import ingest_and_group
from .services import backup
from .services import gyroflow as gyroflow_service

log = logging.getLogger(__name__)


def _scan_once() -> None:
    with session_scope() as session:
        ingest_and_group(session)


def _reap_once() -> None:
    with session_scope() as session:
        dispatch.reap_expired(session)


def _backup_once() -> None:
    """Snapshot the database if the schedule owes one.

    The loop ticks more often than the interval and this decides, so the schedule is
    measured against the newest snapshot on disk rather than against process start: a
    container restarted twice an hour must not take a snapshot each time and roll a
    week of retention over in an afternoon.
    """
    if backup.due(settings.backup_interval_h):
        backup.make()


async def _every(interval_s: float, work, label: str) -> None:
    """Run `work` in a thread, forever, without letting one failure end the loop."""
    while True:
        try:
            await run_in_threadpool(work)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s failed", label)
        await asyncio.sleep(interval_s)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings.ensure_dirs()
    init_db()
    # Mark the volume, so a worker can tell by reading whether it is looking at the
    # dispatcher's files or at a copy of its own. Written here rather than configured:
    # see paths.read_volume_id.
    volume = ensure_volume_id()
    # Templates are read here and travel inside each render spec: the dispatcher owns
    # the editable copies under `templates/`, and a worker has no database to look one
    # up in.
    gyroflow_service.seed_templates()
    with session_scope() as session:
        dispatch.drop_stale_jobs(session)

    # Both loops belong to the dispatcher, not to a worker. Scanning is a metadata
    # walk over the volume the dispatcher owns, and reaping leases is what makes an
    # interrupted job come back. Neither can be left to a machine that may be off.
    tasks = [
        asyncio.create_task(_every(settings.scan_interval_s, _scan_once, "inbox scan")),
        asyncio.create_task(_every(dispatch.REAP_INTERVAL_S, _reap_once, "lease reaping")),
    ]
    if settings.backup_interval_h > 0:
        # Ticks hourly at most, so the interval is honoured within the hour whatever the
        # uptime; `_backup_once` is what decides whether one is actually owed.
        tick = min(settings.backup_interval_h, 1.0) * 3600
        tasks.append(asyncio.create_task(_every(tick, _backup_once, "database snapshot")))
    log.info(
        "API ready, data_dir=%s (volume %s, scanning every %.0fs)",
        settings.data_dir, volume[:8] or "unmarked", settings.scan_interval_s,
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="PostFlight", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(worker_router)
app.include_router(blob_router)


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# Every method the API answers on, so an unknown path under /api gets the same 404
# whichever verb asked for it.
_API_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def api_not_found(rest: str) -> None:
    """Everything under /api that no route claimed.

    Only reachable because of the SPA fallback below, which is a catch-all on every
    path: without this, a mistyped endpoint got 200 and a page of HTML, and the client
    then failed parsing JSON somewhere else entirely.
    """
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such endpoint: /api/{rest}")


def _mount_frontend(target: FastAPI) -> None:
    """Serve the built frontend, with an SPA fallback on index.html.

    Takes the app rather than closing over the module one, so the route table this
    builds can be inspected on a throwaway app: what matters here is the order, and
    the order is the thing a unit test would otherwise have to take on trust.
    """
    index = settings.static_dir / "index.html"
    if not index.exists():
        log.warning("frontend missing (%s): serving the API only", settings.static_dir)
        return

    assets = settings.static_dir / "assets"
    if assets.is_dir():
        target.mount("/assets", StaticFiles(directory=assets), name="assets")

    # After the routers and before the fallback, so the real API routes still win and
    # everything else under /api stops here. Registered inside this function because it
    # exists only to counteract the fallback: with no frontend built there is no
    # catch-all, and an unknown path already 404s on its own.
    target.api_route("/api/{rest:path}", methods=_API_METHODS, include_in_schema=False)(
        api_not_found
    )

    @target.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = settings.static_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    log.info("Frontend served from %s", settings.static_dir)


_mount_frontend(app)
