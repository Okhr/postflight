"""The worker's half of the lease: notice when the job stops being ours, and stop.

Fencing is the subtle part. A worker that keeps encoding after losing its lease
races the machine the job was requeued to, and both write the same output file. So
these tests drive the heartbeat with a fake dispatcher and check that the worker
gives up in each of the ways it can lose a job.
"""

from __future__ import annotations

import time

import pytest

from app import worker as worker_mod
from app.executor import SpecError, execute
from app.services import procs
from app.worker import Heartbeat, TransportError


class FakeDispatcher:
    """Answers heartbeats however the test asks it to."""

    def __init__(self, answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[float, str]] = []

    def heartbeat(self, _job_id, _worker_id, progress, message) -> bool:
        self.calls.append((progress, message))
        answer = self.answers.pop(0) if self.answers else True
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def no_kill(monkeypatch):
    """Count calls to terminate_all instead of signalling real processes."""
    killed: list[int] = []
    monkeypatch.setattr(procs, "terminate_all", lambda *a, **k: killed.append(1) or 0)
    return killed


def _beat(client, renew_s=0.02, lease_s=0.2) -> Heartbeat:
    return Heartbeat(client, job_id=1, worker_id=7, renew_s=renew_s, lease_s=lease_s)


def _wait_for(predicate, timeout=2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_refused_heartbeat_stops_the_job(no_kill):
    """The dispatcher answering `ok: false` means the job was given to someone else."""
    client = FakeDispatcher([False])
    with _beat(client) as beat:
        assert _wait_for(lambda: beat.lost), "the worker kept going after losing the job"
    assert no_kill, "the running encoder should have been asked to stop"


def test_progress_is_reported_without_the_executor_touching_the_network(no_kill):
    client = FakeDispatcher([True, True, True])
    with _beat(client) as beat:
        beat.report(0.25, "encoding")
        assert _wait_for(lambda: client.calls)
    assert client.calls[0] == (0.25, "encoding")
    assert not beat.lost
    assert not no_kill


def test_a_short_outage_does_not_lose_the_job(no_kill):
    """The API restarting is normal. Only silence longer than the lease is fatal."""
    client = FakeDispatcher([TransportError("connection refused"), True])
    with _beat(client, renew_s=0.02, lease_s=5.0) as beat:
        assert _wait_for(lambda: len(client.calls) >= 2)
        assert not beat.lost
    assert not no_kill


def test_a_dispatcher_gone_for_longer_than_the_lease_loses_the_job(no_kill):
    """Nobody renewed the lease, so it has certainly been requeued by now: whatever
    this worker produces from here on must not be reported."""
    client = FakeDispatcher([TransportError("unreachable")] * 50)
    with _beat(client, renew_s=0.02, lease_s=0.05) as beat:
        assert _wait_for(lambda: beat.lost)
    assert no_kill


def test_reporting_a_result_gives_up_after_retrying(monkeypatch):
    """A lost result is not a disaster: the lease lapses and the job comes back. It
    is worth a few retries though, because coming back means redoing the work."""
    monkeypatch.setattr(worker_mod.time, "sleep", lambda _s: None)
    attempts: list[int] = []

    def always_down(*_args):
        attempts.append(1)
        raise TransportError("down")

    assert worker_mod._report(always_down, 1, 7, {}, what="result") is False
    assert len(attempts) == 3


def test_reporting_stops_at_the_first_success(monkeypatch):
    monkeypatch.setattr(worker_mod.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def up_on_second(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise TransportError("down")
        return True

    assert worker_mod._report(up_on_second, 1, 7, {}, what="result") is True
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# The spec is the whole contract, so a broken one must say so plainly
# --------------------------------------------------------------------------- #

def test_an_unknown_kind_is_refused():
    with pytest.raises(SpecError, match="unknown job kind"):
        execute({"kind": "transcode-to-betamax"}, lambda *_: None)


def test_a_render_without_a_template_is_refused():
    """The template travels resolved inside the spec: the worker has no database to
    look one up in."""
    with pytest.raises(SpecError, match="no template"):
        execute({"kind": "render", "source": "merged/x.mp4", "template": {}}, lambda *_: None)


def test_a_missing_part_is_named(session):
    with pytest.raises(SpecError, match="part.* missing"):
        execute({"kind": "merge", "parts": ["raw/gone.MP4"], "dest": "merged/x.mp4"}, lambda *_: None)


def test_an_empty_merge_is_refused(session):
    with pytest.raises(SpecError, match="no destination|no part"):
        execute({"kind": "merge", "parts": [], "dest": None}, lambda *_: None)
