from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import settings
from .db import init_db
from .services import gyroflow as gyroflow_service
from .services.capabilities import detect

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings.ensure_dirs()
    init_db()
    gyroflow_service.seed_templates()
    # Probe here rather than on the first /api/status: the probe really decodes an
    # HEVC 10-bit sample, which is a few seconds, and the header of the very first
    # page load is what would have paid for it.
    detect()
    log.info("API ready, data_dir=%s", settings.data_dir)
    yield


app = FastAPI(title="video-stab", version="0.1.0", lifespan=lifespan)
app.include_router(router)


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
