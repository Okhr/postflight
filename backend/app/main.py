from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
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
from .services import gyroflow as gyroflow_service

log = logging.getLogger(__name__)


def _scan_once() -> None:
    with session_scope() as session:
        ingest_and_group(session)


def _reap_once() -> None:
    with session_scope() as session:
        dispatch.reap_expired(session)


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


app = FastAPI(title="video-stab", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(worker_router)
app.include_router(blob_router)


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _mount_frontend() -> None:
    """Serve the built frontend, with an SPA fallback on index.html."""
    index = settings.static_dir / "index.html"
    if not index.exists():
        log.warning("frontend missing (%s): serving the API only", settings.static_dir)
        return

    assets = settings.static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = settings.static_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    log.info("Front servi depuis %s", settings.static_dir)


_mount_frontend()
