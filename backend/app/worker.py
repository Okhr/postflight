"""Worker: ask the dispatcher for a job, run it, report what came out.

A separate process from the API, and now a separate machine if you want one. It
holds no database and knows no row id: it registers, polls, executes a spec, and
posts back the facts it measured. `executor.py` does the work, this module is the
loop and the plumbing around it.

Three things earn their keep here.

**The heartbeat.** A claimed job is held on a lease that only this worker renews.
Stop renewing (crash, shutdown, cable pulled) and the dispatcher hands the job to
somebody else. That is what makes a worker something you can simply switch off in
the middle of a render.

**Fencing.** The other side of the same coin: if the lease is gone, the job may
already be running elsewhere, so this worker has to stop touching the output file.
Two ffmpeg processes writing the same path is the one outcome worth going out of
the way to prevent, and the heartbeat thread is what notices.

**Progress is decoupled from the network.** The executor's callback only writes to
a variable; the heartbeat thread is what posts. So a slow or missing dispatcher
never slows down a render, and a render that stalls without printing a line still
keeps its lease. A stalled *process* is a different problem, already handled where
it belongs, by the per-process timeouts in `procs.run_with_progress`.

Concurrency is deliberately 1: ffmpeg, mp4_merge and Gyroflow each already
saturate every core, so running two at once only makes both slower.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import settings
from .executor import SpecError, execute
from .paths import read_volume_id
from .services import bench, procs
from .services.capabilities import detect
from .transport import Workspace

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
# Startup backoff while the API is not answering yet. `depends_on` in compose waits
# for the container, not for uvicorn to be listening.
REGISTER_RETRY_S = 3.0
HTTP_TIMEOUT_S = 30.0

# The link probe transfers for a fixed *duration* rather than a fixed size. 8 MB
# would finish inside TCP slow start on a gigabit LAN and report a link far slower
# than it is; 64 MB would take a minute on a home uplink. One second of transfer,
# whatever that turns out to be, answers both cases with one code path.
LINK_PROBE_S = 1.0
LINK_PROBE_MAX = 128 << 20
LINK_READ_CHUNK = 1 << 18

# Moving a 4 GB master is minutes, not seconds. The timeout is per socket operation,
# not for the whole transfer, so this is "the link went quiet for five minutes".
BLOB_TIMEOUT_S = 300.0
BLOB_CHUNK = 1 << 20


class TransportError(RuntimeError):
    """The dispatcher could not be reached. Says nothing about the job itself."""


class Unregistered(RuntimeError):
    """The dispatcher does not know this worker any more: register again."""


class Dispatcher:
    """Everything this worker knows about the outside world."""

    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Worker-Token"] = self.token
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                body = response.read()
                if not body:
                    return response.status, {}
                return response.status, json.loads(body)
        except urllib.error.HTTPError as exc:
            # An answer, just not a happy one: the caller decides what it means.
            return exc.code, {"detail": exc.read().decode(errors="replace")[:500]}
        except (urllib.error.URLError, socket.timeout, OSError, json.JSONDecodeError) as exc:
            raise TransportError(str(exc)) from exc

    def register(
        self,
        name: str,
        capabilities: dict[str, Any],
        rates: dict[str, Any],
        concurrency: int,
        volume_id: str,
    ) -> dict[str, Any]:
        code, body = self._post(
            "/api/workers/register",
            {
                "name": name,
                "capabilities": capabilities,
                "rates": rates,
                "concurrency": concurrency,
                "volume_id": volume_id,
            },
        )
        if code != 200:
            raise TransportError(f"register refused ({code}): {body.get('detail', '')}")
        return body

    def link_mbps(self) -> float | None:
        """Megabytes per second pulled from the dispatcher, or None if unmeasurable.

        Half of what decides where a job runs: a machine twice as fast is not worth
        using if getting the master to it costs more than the time it saves.
        """
        headers = {"X-Worker-Token": self.token} if self.token else {}
        request = urllib.request.Request(
            f"{self.base_url}/api/workers/bandwidth?size={LINK_PROBE_MAX}", headers=headers
        )
        received = 0
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                started = time.monotonic()
                while time.monotonic() - started < LINK_PROBE_S:
                    block = response.read(LINK_READ_CHUNK)
                    if not block:
                        break
                    received += len(block)
                elapsed = time.monotonic() - started
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError) as exc:
            log.warning("Link speed not measured (%s)", exc)
            return None
        if not received or elapsed <= 0:
            return None
        return received / (1 << 20) / elapsed

    def claim(self, worker_id: int, cached: list[str] | None = None) -> dict[str, Any] | None:
        code, body = self._post(
            f"/api/workers/{worker_id}/claim", {"cached": cached or []}
        )
        if code == 204:
            return None
        if code == 404:
            raise Unregistered(body.get("detail", "unknown worker"))
        if code != 200:
            raise TransportError(f"claim refused ({code}): {body.get('detail', '')}")
        return body

    def _headers(self) -> dict[str, str]:
        return {"X-Worker-Token": self.token} if self.token else {}

    def download(self, rel: str, dest: Path, on_bytes: Any = None) -> int:
        """Pull one file into this machine's data directory.

        Written beside its final name and renamed at the end, so a transfer cut off
        halfway is never mistaken for a master this worker already holds: the cache
        check is a path and a size, and a truncated file would pass it.
        """
        url = f"{self.base_url}/api/blobs/{quote(rel)}"
        request = urllib.request.Request(url, headers=self._headers())
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        received = 0
        try:
            with urllib.request.urlopen(request, timeout=BLOB_TIMEOUT_S) as response:
                with partial.open("wb") as sink:
                    while True:
                        block = response.read(BLOB_CHUNK)
                        if not block:
                            break
                        sink.write(block)
                        received += len(block)
                        if on_bytes:
                            on_bytes(received)
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, OSError) as exc:
            partial.unlink(missing_ok=True)
            raise TransportError(f"{rel} could not be fetched: {exc}") from exc
        partial.replace(dest)
        return received

    def upload(self, rel: str, source: Path) -> int:
        """Send one file the job produced back to the dispatcher."""
        size = source.stat().st_size
        url = f"{self.base_url}/api/blobs/{quote(rel)}"
        headers = {
            **self._headers(),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        }
        try:
            with source.open("rb") as body:
                request = urllib.request.Request(url, data=body, headers=headers, method="PUT")
                with urllib.request.urlopen(request, timeout=BLOB_TIMEOUT_S) as response:
                    response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, OSError) as exc:
            raise TransportError(f"{rel} could not be sent: {exc}") from exc
        return size

    def heartbeat(self, job_id: int, worker_id: int, progress: float, message: str) -> bool:
        code, body = self._post(
            f"/api/jobs/{job_id}/heartbeat",
            {"worker_id": worker_id, "progress": progress, "message": message},
        )
        if code != 200:
            raise TransportError(f"heartbeat refused ({code})")
        return bool(body.get("ok"))

    def complete(
        self, job_id: int, worker_id: int, result: dict[str, Any], elapsed_s: float = 0.0
    ) -> bool:
        code, body = self._post(
            f"/api/jobs/{job_id}/complete",
            {"worker_id": worker_id, "result": result, "elapsed_s": elapsed_s},
        )
        if code != 200:
            raise TransportError(f"complete refused ({code})")
        return bool(body.get("ok"))

    def fail(self, job_id: int, worker_id: int, error: str) -> bool:
        code, body = self._post(
            f"/api/jobs/{job_id}/fail", {"worker_id": worker_id, "error": error[:2000]}
        )
        if code != 200:
            raise TransportError(f"fail refused ({code})")
        return bool(body.get("ok"))

    def release(self, worker_id: int) -> None:
        self._post(f"/api/workers/{worker_id}/release", {})


class Heartbeat:
    """Holds the lease while a job runs, and fences the job when it cannot.

    Runs in its own thread so that neither a slow dispatcher nor a silent encoder
    can be mistaken for the other.
    """

    def __init__(
        self,
        client: Dispatcher,
        job_id: int,
        worker_id: int,
        renew_s: float,
        lease_s: float,
    ) -> None:
        self._client = client
        self._job_id = job_id
        self._worker_id = worker_id
        # The dispatcher owns both timings and sends them at registration. The floor
        # is only there so a nonsense value cannot turn this into a busy loop, and a
        # lease shorter than the renewal interval would expire by construction.
        self._renew_s = max(0.01, renew_s)
        self._lease_s = max(self._renew_s, lease_s)
        self._progress = 0.0
        self._message = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)

    @property
    def lost(self) -> bool:
        """True once the job stopped being ours. Whatever it produced is not to be
        reported: the dispatcher has already given the work to someone else."""
        return self._lost.is_set()

    def report(self, progress: float, message: str = "") -> None:
        """Progress callback handed to the executor. Never touches the network."""
        with self._lock:
            self._progress = progress
            self._message = message

    def _run(self) -> None:
        last_ok = time.monotonic()
        while not self._stop.wait(self._renew_s):
            with self._lock:
                progress, message = self._progress, self._message
            try:
                still_ours = self._client.heartbeat(
                    self._job_id, self._worker_id, progress, message
                )
            except TransportError as exc:
                # A blip is normal (the API restarting, for one). Only a silence
                # longer than the lease means the job is certainly gone.
                if time.monotonic() - last_ok > self._lease_s:
                    log.error(
                        "Job %s: dispatcher unreachable for more than %.0fs, giving it up (%s)",
                        self._job_id, self._lease_s, exc,
                    )
                    self._give_up()
                    return
                continue
            last_ok = time.monotonic()
            if not still_ours:
                log.warning("Job %s is no longer ours: stopping", self._job_id)
                self._give_up()
                return

    def _give_up(self) -> None:
        self._lost.set()
        # Concurrency is 1, so every child belongs to this job.
        procs.terminate_all()

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)


def _worker_name() -> str:
    return settings.worker_name or os.environ.get("HOSTNAME") or socket.gethostname()


def run_job(
    client: Dispatcher,
    worker_id: int,
    claimed: dict[str, Any],
    lease: dict[str, Any],
    workspace: Workspace | None = None,
    stopping: threading.Event | None = None,
) -> None:
    """Execute one claimed job and report the outcome. Never raises.

    `workspace` is set only when this worker keeps its own copy of the data volume, in
    which case the job is bracketed by a fetch and a send. Everything between the two is
    identical either way: the spec carries relative paths and the executor resolves them
    against whatever data directory it has.
    """
    job_id = claimed["job_id"]
    spec = claimed["spec"]
    log.info("Job %s#%s starting", claimed.get("kind"), job_id)

    with Heartbeat(client, job_id, worker_id, lease["renew_s"], lease["lease_s"]) as beat:
        try:
            # Inside the heartbeat block, because a 4 GB transfer outlasts a lease and
            # the job has to stay ours while it happens.
            before = workspace.pull(spec.get("inputs") or [], beat.report) if workspace else {}
            # Timed around the work alone: the dispatcher folds this number into what it
            # knows about this machine's speed, so counting a transfer here would call a
            # fast machine slow for having a thin cable. Waiting for the heartbeat thread
            # to notice it should stop is excluded for the same reason.
            started = time.monotonic()
            result = execute(spec, beat.report)
            elapsed = time.monotonic() - started
            if workspace:
                workspace.publish(before, beat.report)
        except Exception as exc:  # noqa: BLE001 (a job that breaks must not take the worker down)
            if beat.lost:
                log.warning("Job %s stopped because it was taken away", job_id)
                return
            if stopping is not None and stopping.is_set():
                # We killed this ffmpeg ourselves, on the way out. Reporting a failure
                # here would be a lie, and it would win the race: `release` gives back
                # what this worker holds without spending an attempt, but only what is
                # still RUNNING, and the failure report gets there first. So say nothing
                # and let the job be handed back. If this process is killed before it can
                # say goodbye, the lease lapses and the reaper does the same thing.
                log.info("Job %s given back: this worker is shutting down", job_id)
                return
            message = str(exc)
            if isinstance(exc, procs.ProcessError) and exc.log_tail:
                message = f"{message}\n{exc.log_tail}"
            elif not isinstance(exc, (SpecError, procs.ProcessError, TransportError)):
                log.exception("Job %s raised", job_id)
            _report(client.fail, job_id, worker_id, message, what="failure")
            return

    if beat.lost:
        # Finished, but somebody else owns the job now. Reporting would overwrite
        # their work with ours.
        log.warning("Job %s finished after being taken away: result dropped", job_id)
        return

    reporter = partial(client.complete, elapsed_s=elapsed)
    if _report(reporter, job_id, worker_id, result, what="result"):
        log.info("Job %s#%s done in %.1fs", claimed.get("kind"), job_id, elapsed)


def _report(method, job_id: int, worker_id: int, payload, what: str) -> bool:
    """Post an outcome, retrying briefly: losing it would leave the job hanging
    until its lease lapses, and the work would then be redone for nothing."""
    for attempt in range(3):
        try:
            return method(job_id, worker_id, payload)
        except TransportError as exc:
            log.warning("Job %s: %s could not be posted (%s)", job_id, what, exc)
            time.sleep(2.0 * (attempt + 1))
    log.error("Job %s: %s lost, the dispatcher will requeue it", job_id, what)
    return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings.ensure_dirs()
    name = _worker_name()
    client = Dispatcher(settings.api_url, settings.worker_token)
    stop = threading.Event()

    def _shutdown(signum, _frame):  # noqa: ANN001
        log.info("signal %s received, stopping the worker", signum)
        stop.set()
        # Hand the signal down rather than letting the container's grace period run
        # out and SIGKILL everything: an ffmpeg killed mid-VAAPI-decode leaves this
        # machine's amdgpu deadlocked on an orphaned fence. See procs.terminate_all.
        hit = procs.terminate_all()
        if hit:
            log.info("shutdown: %d child process(es) asked to stop", hit)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Probing really decodes an HEVC 10-bit sample, so it costs a few seconds. Pay
    # it once, here, and hand the answer to the dispatcher: the hardware that
    # matters is the hardware that does the work.
    caps = detect()

    # And then run the four jobs for real on half a second of baked-in rush, which is
    # the only thing that answers how *fast* this machine is. Measured 3.6 s all in,
    # which is why there is no cache to invalidate: it is cheaper to measure again
    # than to reason about when a stored number went stale.
    benchmark = bench.measure(caps)

    capabilities = caps.to_dict()
    worker_id = 0
    workspace: Workspace | None = None
    lease: dict[str, Any] = {}
    while not stop.is_set():
        try:
            if not worker_id:
                # Re-measured at every registration rather than once: the link is the
                # one property of a worker that can change without the process
                # restarting, and it costs a second.
                benchmark.link_mbps = client.link_mbps()
                lease = client.register(
                    name,
                    capabilities,
                    benchmark.to_dict(),
                    settings.worker_concurrency,
                    read_volume_id(),
                )
                worker_id = lease["worker_id"]
                log.info(
                    "Worker %s registered as #%s on %s (lease %.0fs, shares_data=%s, link %s)",
                    name, worker_id, settings.api_url, lease["lease_s"],
                    lease.get("shares_data"),
                    f"{benchmark.link_mbps:.0f} MB/s" if benchmark.link_mbps else "?",
                )

            if workspace is None and not lease.get("shares_data", True):
                # Built only now, because only registration can say whether this worker
                # is looking at the dispatcher's files or at a copy. It has to be that
                # way round: a Workspace evicts footage, and on the dispatcher's volume
                # that would be deleting the originals.
                workspace = Workspace(client)
                log.info("This worker keeps its own copy: files will travel over HTTP")
            claimed = client.claim(worker_id, workspace.cached() if workspace else None)
        except Unregistered as exc:
            log.warning("Dispatcher no longer knows us (%s): registering again", exc)
            worker_id = 0
            stop.wait(REGISTER_RETRY_S)
            continue
        except TransportError as exc:
            log.warning("Dispatcher unreachable (%s), retrying in %.0fs", exc, REGISTER_RETRY_S)
            stop.wait(REGISTER_RETRY_S)
            continue

        if claimed is None:
            stop.wait(POLL_INTERVAL_S)
            continue

        run_job(client, worker_id, claimed, lease, workspace, stopping=stop)

    if worker_id:
        # Give the jobs back at once instead of leaving the queue idle until the
        # lease lapses.
        try:
            client.release(worker_id)
        except TransportError as exc:
            log.warning("Jobs could not be released (%s): their lease will lapse", exc)

    log.info("Worker stopped")


if __name__ == "__main__":
    main()
