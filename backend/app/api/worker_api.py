"""The endpoints a worker talks to. Not meant for the browser.

Workers **pull**: the dispatcher never calls out to a machine. That single choice
removes most of the moving parts one would otherwise need. There is no discovery
(a worker is handed a URL and registers itself), no inbound port and no firewall
rule on the worker side, no health probe (a worker that stops asking is gone), and
no way for the dispatcher to overload a worker, since work only moves when the
worker asks for it.

The models here are the worker protocol, deliberately separate from `schemas.py`,
which describes what the UI sees.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from .. import dispatch
from ..config import settings
from ..db import get_session
from ..models import Worker

log = logging.getLogger(__name__)


def require_worker_token(x_worker_token: str | None = Header(default=None)) -> None:
    """Shared secret, checked only when one is configured.

    Left empty, these endpoints are open, which is what a worker sitting next to the
    dispatcher on a private volume needs. Set `VS_WORKER_TOKEN` on both sides before
    the API is reachable by anything else.
    """
    if not settings.worker_token:
        return
    if x_worker_token != settings.worker_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid worker token")


router = APIRouter(prefix="/api", tags=["workers"], dependencies=[Depends(require_worker_token)])


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #

class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = 1


class RegisterOut(BaseModel):
    worker_id: int
    # The worker does not hardcode the timings: the dispatcher owns them, so both
    # sides cannot disagree about when a lease is dead.
    lease_s: float
    renew_s: float


class ClaimOut(BaseModel):
    job_id: int
    kind: str
    spec: dict[str, Any]


class HeartbeatIn(BaseModel):
    worker_id: int
    progress: float = 0.0
    message: str = ""


class CompleteIn(BaseModel):
    worker_id: int
    result: dict[str, Any]


class FailIn(BaseModel):
    worker_id: int
    error: str


class AckOut(BaseModel):
    # False means "this job is not yours any more, stop working on it". A worker
    # that ignored it would race the machine the job was requeued to, and both
    # would write the same output file.
    ok: bool


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.post("/workers/register", response_model=RegisterOut)
def register(payload: RegisterIn, session: Session = Depends(get_session)) -> RegisterOut:
    worker = dispatch.upsert_worker(
        session, payload.name, payload.capabilities, payload.concurrency
    )
    return RegisterOut(
        worker_id=worker.id or 0,
        lease_s=dispatch.LEASE_S,
        renew_s=dispatch.HEARTBEAT_S,
    )


@router.post("/workers/{worker_id}/claim", response_model=None)
def claim(worker_id: int, session: Session = Depends(get_session)) -> ClaimOut | Response:
    """Hand out one job, or 204 when the queue is empty.

    Plain polling on purpose: a homelab queue sees a handful of jobs a day, and one
    request per second per worker costs nothing. Long polling would only save that.
    """
    worker = session.get(Worker, worker_id)
    if worker is None:
        # The database was wiped, or this is a stale process: re-register.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown worker, register again")

    taken = dispatch.claim(session, worker)
    if taken is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    job, spec = taken
    return ClaimOut(job_id=job.id or 0, kind=job.kind.value, spec=spec)


@router.post("/jobs/{job_id}/heartbeat", response_model=AckOut)
def heartbeat(
    job_id: int, payload: HeartbeatIn, session: Session = Depends(get_session)
) -> AckOut:
    ok = dispatch.heartbeat(
        session, job_id, payload.worker_id, payload.progress, payload.message
    )
    return AckOut(ok=ok)


@router.post("/jobs/{job_id}/complete", response_model=AckOut)
def complete(
    job_id: int, payload: CompleteIn, session: Session = Depends(get_session)
) -> AckOut:
    ok = dispatch.complete(session, job_id, payload.worker_id, payload.result)
    if not ok:
        log.warning("Job %s completed by worker %s, which no longer holds it", job_id, payload.worker_id)
    return AckOut(ok=ok)


@router.post("/jobs/{job_id}/fail", response_model=AckOut)
def fail(job_id: int, payload: FailIn, session: Session = Depends(get_session)) -> AckOut:
    ok = dispatch.fail(session, job_id, payload.worker_id, payload.error)
    return AckOut(ok=ok)


@router.post("/workers/{worker_id}/release", response_model=AckOut)
def release(worker_id: int, session: Session = Depends(get_session)) -> AckOut:
    """A worker shutting down cleanly gives its jobs back at once.

    Without this the queue would sit idle until the lease lapsed, every time a
    worker is switched off.
    """
    dispatch.release(session, worker_id)
    return AckOut(ok=True)
