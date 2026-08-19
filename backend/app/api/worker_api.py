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
from secrets import compare_digest
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
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
    the API is reachable by anything else, and note that this matters more since the
    blob endpoints exist: they guard reading any footage on the volume and writing into
    the directories the pipeline produces, not just asking for a job.

    Compared with `compare_digest` rather than `!=`. Timing is not the threat model on a
    private network, but this is the one place a secret is checked and the cost of doing
    it properly is a line.
    """
    if not settings.worker_token:
        return
    if not x_worker_token or not compare_digest(x_worker_token, settings.worker_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid worker token")


router = APIRouter(prefix="/api", tags=["workers"], dependencies=[Depends(require_worker_token)])


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #

class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    # What the startup benchmark measured, one rate per job kind. See services/bench.
    rates: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = 1
    # The mark on the data volume this worker sees, empty if it sees none. Compared
    # with the dispatcher's own, and that comparison is the whole of how the system
    # knows whether files have to travel.
    volume_id: str = Field(default="", max_length=64)


class RegisterOut(BaseModel):
    worker_id: int
    # The worker does not hardcode the timings: the dispatcher owns them, so both
    # sides cannot disagree about when a lease is dead.
    lease_s: float
    renew_s: float
    # What the dispatcher concluded about the volume. The worker needs to be told,
    # because it is what decides whether it reads inputs where they are or fetches
    # them over HTTP first.
    shares_data: bool


class ClaimIn(BaseModel):
    # What this worker already holds, so a job whose master is already there is not
    # priced as if it had to travel again. Empty from a worker that shares the volume:
    # nothing travels for it in the first place.
    cached: list[str] = Field(default_factory=list)


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
    # Seconds the worker spent on the work itself, transfers excluded. The dispatcher
    # cannot derive this from the job row: that would also count the time a remote
    # worker spent pulling a 4 GB master, and call the machine slow for having a thin
    # cable.
    elapsed_s: float = 0.0


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
        session,
        payload.name,
        payload.capabilities,
        payload.concurrency,
        rates=payload.rates,
        volume_id=payload.volume_id,
    )
    return RegisterOut(
        worker_id=worker.id or 0,
        lease_s=dispatch.LEASE_S,
        renew_s=dispatch.HEARTBEAT_S,
        shares_data=worker.shares_data,
    )


# How much the bandwidth probe is willing to send. The worker stops reading once it
# has seen a second's worth, so this is a ceiling and not a cost.
BANDWIDTH_CHUNK = 1 << 20
BANDWIDTH_MAX = 128 << 20


@router.get("/workers/bandwidth")
def bandwidth(size: int = Query(default=BANDWIDTH_MAX, ge=1, le=BANDWIDTH_MAX)) -> StreamingResponse:
    """Bytes to nowhere, so a worker can time how fast it pulls from the dispatcher.

    That number is half of what decides where a job runs: a machine twice as fast is
    not worth using if getting the master to it costs more than the time it saves.

    Zeros rather than random bytes, and deliberately: nothing in this path is
    compressed, and generating 128 MB of randomness would measure the dispatcher's CPU
    instead of the link. The worker closes the connection when it has enough, which is
    what keeps this bounded on a fast link and honest on a slow one.
    """
    block = b"\0" * BANDWIDTH_CHUNK

    def stream():
        sent = 0
        while sent < size:
            piece = block[: min(BANDWIDTH_CHUNK, size - sent)]
            sent += len(piece)
            yield piece

    return StreamingResponse(stream(), media_type="application/octet-stream")


@router.post("/workers/{worker_id}/claim", response_model=None)
def claim(
    worker_id: int,
    payload: ClaimIn = ClaimIn(),
    session: Session = Depends(get_session),
) -> ClaimOut | Response:
    """Hand out one job, or 204 when the queue is empty.

    Plain polling on purpose: a homelab queue sees a handful of jobs a day, and one
    request per second per worker costs nothing. Long polling would only save that.
    """
    worker = session.get(Worker, worker_id)
    if worker is None:
        # The database was wiped, or this is a stale process: re-register.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown worker, register again")

    taken = dispatch.claim(session, worker, payload.cached)
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
    ok = dispatch.complete(
        session, job_id, payload.worker_id, payload.result, payload.elapsed_s
    )
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
