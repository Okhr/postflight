"""What the dispatcher promises: one job goes to one worker, and no job is lost.

These tests are about the queue, not about ffmpeg. They never run a subprocess:
the worker's side of the contract is a result dictionary, so a fabricated one is
exactly as good as a measured one for checking what gets recorded.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app import dispatch
from app.config import settings
from app.models import (
    Clip,
    ClipState,
    Grade,
    Job,
    JobKind,
    JobState,
    Render,
    RenderState,
    Sequence,
    SequenceState,
    Worker,
    utcnow,
)
from app.paths import ensure_volume_id, read_volume_id
from app.pipeline import enqueue_merge


def _worker(session: Session, name: str) -> Worker:
    return dispatch.upsert_worker(session, name, {"decode_backend": "cpu"}, 1)


def _merge_result() -> dict:
    """What a worker reports after merging: measured on the file, never estimated."""
    return {
        "path": "merged/DJI_20260819_120000_0001_D__abc123.mp4",
        "method": "mp4_merge",
        "width": 3840,
        "height": 2880,
        "fps_num": 60000,
        "fps_den": 1001,
        "duration_ms": 240_240.0,
        "size_bytes": 8_100_000_000,
    }


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def test_a_worker_that_restarts_is_the_same_worker(session: Session):
    """Keyed by name, so history follows the machine and not the process."""
    first = _worker(session, "proxima")
    again = dispatch.upsert_worker(session, "proxima", {"decode_backend": "cuda"}, 4)
    assert again.id == first.id
    assert again.capabilities["decode_backend"] == "cuda"
    assert again.concurrency == 4
    assert len(session.exec(select(Worker)).all()) == 1


def test_a_worker_that_stopped_asking_is_offline(session: Session):
    worker = _worker(session, "proxima")
    assert dispatch.is_online(worker)
    worker.last_seen_at = utcnow() - timedelta(seconds=dispatch.ONLINE_S + 1)
    assert not dispatch.is_online(worker)


# --------------------------------------------------------------------------- #
# Claiming
# --------------------------------------------------------------------------- #

def test_one_job_goes_to_exactly_one_worker(session: Session, sequence: Sequence):
    """The whole point of a conditional UPDATE. A read followed by a write would
    hand the same 4 GB merge to two machines, both writing the same output."""
    job = enqueue_merge(session, sequence)
    first, second = _worker(session, "nas"), _worker(session, "proxima")

    assert dispatch._take(session, job.id, first.id) is True
    assert dispatch._take(session, job.id, second.id) is False

    session.refresh(job)
    assert job.worker_id == first.id
    assert job.state == JobState.RUNNING
    assert job.attempts == 1


def test_claim_returns_a_spec_the_worker_can_run_without_a_database(
    session: Session, sequence: Sequence
):
    enqueue_merge(session, sequence)
    worker = _worker(session, "nas")

    taken = dispatch.claim(session, worker)
    assert taken is not None
    job, spec = taken

    assert spec["kind"] == "merge"
    assert spec["job_id"] == job.id
    # Relative to data_dir, so the same spec works whether the worker shares the
    # volume or keeps its own copy.
    assert spec["parts"] == [
        "raw/DJI_20260819_120000_0001_D.MP4",
        "raw/DJI_20260819_120000_0002_D.MP4",
    ]
    assert spec["dest"] == "merged/DJI_20260819_120000_0001_D__abc123.mp4"
    assert "sequence_id" not in spec  # no row id ever leaves the dispatcher

    session.refresh(sequence)
    assert sequence.state == SequenceState.MERGING


def test_an_empty_queue_gives_nothing(session: Session):
    assert dispatch.claim(session, _worker(session, "nas")) is None


def test_claim_refreshes_last_seen(session: Session):
    worker = _worker(session, "nas")
    worker.last_seen_at = utcnow() - timedelta(seconds=300)
    session.add(worker)
    session.commit()

    dispatch.claim(session, worker)
    assert dispatch.is_online(worker)


def test_an_undescribable_job_is_failed_instead_of_handed_out(
    session: Session, sequence: Sequence
):
    """A sequence with no part cannot be merged. Failing it here keeps a stale
    reference from travelling to a worker only to come back as an error."""
    for clip in session.exec(select(Clip).where(Clip.sequence_id == sequence.id)).all():
        session.delete(clip)
    session.commit()
    job = enqueue_merge(session, sequence)

    assert dispatch.claim(session, _worker(session, "nas")) is None

    session.refresh(job)
    assert job.state == JobState.FAILED
    assert "no part" in (job.error or "")


def test_priority_decides_which_job_goes_first(session: Session, sequence: Sequence):
    """The proxy of an already-merged sequence outranks the merge of the next one:
    a rush becomes workable as soon as its own proxy exists."""
    from app.pipeline import enqueue_proxy

    sequence.merged_path = "merged/x.mp4"
    sequence.state = SequenceState.MERGED
    session.add(sequence)
    session.commit()

    other = Sequence(key="DJI_other", content_hash="def456", state=SequenceState.NEW)
    session.add(other)
    session.commit()
    session.add(
        Clip(
            sequence_id=other.id, part_index=0, filename="o.MP4",
            raw_path="raw/o.MP4", fingerprint="fpo", state=ClipState.INGESTED,
        )
    )
    session.commit()

    enqueue_merge(session, other)
    enqueue_proxy(session, sequence)

    taken = dispatch.claim(session, _worker(session, "nas"))
    assert taken is not None
    assert taken[0].kind == JobKind.PROXY


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #

def test_a_heartbeat_renews_the_lease_and_records_progress(
    session: Session, sequence: Sequence
):
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "nas")
    dispatch._take(session, job.id, worker.id)
    session.refresh(job)
    first_deadline = job.lease_expires_at

    assert dispatch.heartbeat(session, job.id, worker.id, 0.42, "merging") is True
    session.refresh(job)
    assert job.progress == pytest.approx(0.42)
    assert job.message == "merging"
    assert job.lease_expires_at >= first_deadline


def test_a_busy_worker_stays_online_on_its_heartbeat_alone(
    session: Session, sequence: Sequence
):
    """It stops asking for work while its only slot is taken, and asking for work used
    to be the only thing that proved it was alive. So a worker went dark in the
    interface for the whole length of a job: seen on 2026-08-20, a proxy running at
    52% under a header reading "no worker"."""
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "nas")
    dispatch._take(session, job.id, worker.id)

    worker.last_seen_at = utcnow() - timedelta(seconds=dispatch.ONLINE_S * 2)
    session.add(worker)
    session.commit()
    assert dispatch.is_online(worker) is False

    dispatch.heartbeat(session, job.id, worker.id, 0.52, "encoding")
    session.refresh(worker)
    assert dispatch.is_online(worker) is True


def test_a_heartbeat_from_a_worker_that_lost_the_job_proves_nothing(
    session: Session, sequence: Sequence
):
    """The lease went elsewhere, so this one is being told to stop. Marking it alive
    would be reading a claim it no longer holds as evidence."""
    job = enqueue_merge(session, sequence)
    holder, other = _worker(session, "nas"), _worker(session, "proxima")
    dispatch._take(session, job.id, holder.id)
    other.last_seen_at = utcnow() - timedelta(seconds=dispatch.ONLINE_S * 2)
    session.add(other)
    session.commit()

    assert dispatch.heartbeat(session, job.id, other.id, 0.5, "") is False
    session.refresh(other)
    assert dispatch.is_online(other) is False


def test_a_worker_that_lost_the_job_is_told_to_stop(session: Session, sequence: Sequence):
    """False on a heartbeat is how fencing works: the job was requeued elsewhere,
    and two encoders writing the same output is what this prevents."""
    job = enqueue_merge(session, sequence)
    holder, other = _worker(session, "nas"), _worker(session, "proxima")
    dispatch._take(session, job.id, holder.id)

    assert dispatch.heartbeat(session, job.id, other.id, 0.5, "") is False
    assert dispatch.complete(session, job.id, other.id, _merge_result()) is False
    assert dispatch.fail(session, job.id, other.id, "boom") is False

    session.refresh(job)
    assert job.state == JobState.RUNNING
    assert job.worker_id == holder.id


def test_a_result_posted_after_being_requeued_is_refused(
    session: Session, sequence: Sequence
):
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "nas")
    dispatch._take(session, job.id, worker.id)
    dispatch._requeue(session, session.get(Job, job.id), "test")
    session.commit()

    assert dispatch.complete(session, job.id, worker.id, _merge_result()) is False
    session.refresh(sequence)
    assert sequence.merged_path is None


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

def test_a_merge_result_is_recorded_and_the_proxy_queued(
    session: Session, sequence: Sequence
):
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "nas")
    dispatch._take(session, job.id, worker.id)

    assert dispatch.complete(session, job.id, worker.id, _merge_result()) is True

    session.refresh(job)
    session.refresh(sequence)
    assert job.state == JobState.DONE
    assert job.progress == 1.0
    assert job.lease_expires_at is None
    # Measured on the merged file, not summed from the parts: 240_240 and not 240_000.
    assert sequence.duration_ms == pytest.approx(240_240.0)
    assert sequence.frame_count == 14400
    assert sequence.state == SequenceState.MERGED
    assert all(
        c.state == ClipState.MERGED
        for c in session.exec(select(Clip).where(Clip.sequence_id == sequence.id)).all()
    )
    queued = session.exec(select(Job).where(Job.state == JobState.QUEUED)).all()
    assert [j.kind for j in queued] == [JobKind.PROXY]


def test_purging_the_parts_is_the_dispatchers_job(
    session: Session, sequence: Sequence, monkeypatch
):
    """The copy that matters is the one on the dispatcher's volume. A worker only
    ever holds a copy, so deleting from there would delete the wrong file."""
    monkeypatch.setattr(settings, "purge_parts_after_merge", True)
    parts = [settings.raw_dir / f"DJI_20260819_120000_000{i}_D.MP4" for i in (1, 2)]
    assert all(p.exists() for p in parts)

    job = enqueue_merge(session, sequence)
    worker = _worker(session, "nas")
    dispatch._take(session, job.id, worker.id)
    dispatch.complete(session, job.id, worker.id, _merge_result())

    assert not any(p.exists() for p in parts)


def test_a_copied_single_part_is_never_purged(
    session: Session, sequence: Sequence, monkeypatch
):
    """Only a verified `mp4_merge` proves the bytes are elsewhere. A hardlink shares
    them, and deleting the source of a copy that failed halfway loses the rush."""
    monkeypatch.setattr(settings, "purge_parts_after_merge", True)
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "nas")
    dispatch._take(session, job.id, worker.id)
    dispatch.complete(
        session, job.id, worker.id, {**_merge_result(), "method": "hardlink"}
    )

    parts = [settings.raw_dir / f"DJI_20260819_120000_000{i}_D.MP4" for i in (1, 2)]
    assert all(p.exists() for p in parts)


def test_a_failure_marks_the_sequence_too(session: Session, sequence: Sequence):
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "nas")
    dispatch._take(session, job.id, worker.id)

    assert dispatch.fail(session, job.id, worker.id, "mp4_merge exited with code 1") is True

    session.refresh(job)
    session.refresh(sequence)
    assert job.state == JobState.FAILED
    assert sequence.state == SequenceState.FAILED
    assert "mp4_merge" in (sequence.error or "")


def test_an_unusable_result_fails_the_job_rather_than_crashing(
    session: Session, sequence: Sequence
):
    """The work happened; only the recording broke. Better a failed job carrying the
    reason than an exception surfacing as a transport error on the worker."""
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "nas")
    dispatch._take(session, job.id, worker.id)

    assert dispatch.complete(session, job.id, worker.id, {"path": "merged/x.mp4"}) is True
    session.refresh(job)
    assert job.state == JobState.FAILED
    assert "could not be applied" in (job.error or "")


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #

def test_an_expired_lease_puts_the_job_back(session: Session, sequence: Sequence):
    """The only automatic retry in the system, and what makes a worker something you
    can switch off mid-render."""
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "proxima")
    dispatch._take(session, job.id, worker.id)
    session.refresh(job)
    job.lease_expires_at = utcnow() - timedelta(seconds=1)
    session.add(job)
    session.commit()
    # Force a re-read so the deadline comes back naive, the way SQLite returns it.
    session.expire_all()

    assert dispatch.reap_expired(session) == 1
    session.refresh(job)
    assert job.state == JobState.QUEUED
    assert job.worker_id is None
    assert job.lease_expires_at is None
    assert job.attempts == 1  # spent, so a poisoned job cannot loop forever


def test_a_live_lease_is_left_alone(session: Session, sequence: Sequence):
    job = enqueue_merge(session, sequence)
    dispatch._take(session, job.id, _worker(session, "nas").id)
    assert dispatch.reap_expired(session) == 0
    session.refresh(job)
    assert job.state == JobState.RUNNING


def test_a_running_job_without_a_lease_is_reaped(session: Session, sequence: Sequence):
    """Written by a database that predates leases, or by a claim that crashed
    halfway: either way nobody is holding it."""
    job = enqueue_merge(session, sequence)
    job.state = JobState.RUNNING
    job.lease_expires_at = None
    session.add(job)
    session.commit()

    assert dispatch.reap_expired(session) == 1
    session.refresh(job)
    assert job.state == JobState.QUEUED


def test_a_job_that_keeps_killing_its_worker_ends_up_failed(
    session: Session, sequence: Sequence
):
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "proxima")
    for _ in range(dispatch.MAX_ATTEMPTS):
        dispatch._take(session, job.id, worker.id)
        session.refresh(job)
        job.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(job)
        session.commit()
        dispatch.reap_expired(session)
        session.refresh(job)

    assert job.state == JobState.FAILED
    assert "abandoned" in (job.error or "")


def test_a_clean_shutdown_hands_the_job_back_without_spending_an_attempt(
    session: Session, sequence: Sequence
):
    """Being switched off is not the job's fault, and the queue should not sit idle
    for a minute waiting for a lease nobody is going to renew."""
    job = enqueue_merge(session, sequence)
    worker = _worker(session, "proxima")
    dispatch._take(session, job.id, worker.id)
    session.refresh(job)
    assert job.attempts == 1

    assert dispatch.release(session, worker.id) == 1
    session.refresh(job)
    assert job.state == JobState.QUEUED
    assert job.attempts == 0
    assert job.worker_id is None


def test_releasing_touches_only_that_workers_jobs(session: Session, sequence: Sequence):
    job = enqueue_merge(session, sequence)
    holder, other = _worker(session, "nas"), _worker(session, "proxima")
    dispatch._take(session, job.id, holder.id)

    assert dispatch.release(session, other.id) == 0
    session.refresh(job)
    assert job.state == JobState.RUNNING


# --------------------------------------------------------------------------- #
# Ranking: what the benchmark said, and what real jobs proved
# --------------------------------------------------------------------------- #

def test_a_real_job_beats_the_startup_benchmark(session: Session):
    """The benchmark runs on 30 frames and overstates by a fixed-ish factor
    (measured: 28 img/s against 22.7 on a real sequence). Once a real job has been
    timed, that is the number worth using."""
    worker = dispatch.upsert_worker(session, "proxima", {}, 1, rates={"render_fps": 28.0})
    assert dispatch.rate_for(worker, JobKind.RENDER) == 28.0

    worker.observed = {"render_fps": 22.7}
    assert dispatch.rate_for(worker, JobKind.RENDER) == 22.7


def test_an_unmeasured_worker_stays_unmeasured(session: Session):
    """Neither fast nor slow: unknown, which is a case the caller has to handle."""
    worker = dispatch.upsert_worker(session, "proxima", {}, 1)
    assert dispatch.rate_for(worker, JobKind.RENDER) is None


def test_registering_again_keeps_what_real_jobs_measured(session: Session):
    """A container restart re-runs the benchmark. It must not throw away the moving
    average, which is the only number that ever came from real work."""
    worker = dispatch.upsert_worker(session, "proxima", {}, 1, rates={"render_fps": 28.0})
    worker.observed = {"render_fps": 22.7, "render_fps_n": 4}
    session.add(worker)
    session.commit()

    again = dispatch.upsert_worker(session, "proxima", {}, 1, rates={"render_fps": 30.0})
    assert again.rates["render_fps"] == 30.0
    assert again.observed["render_fps"] == 22.7


def test_a_finished_job_teaches_the_worker_its_own_speed(
    session: Session, sequence: Sequence
):
    worker = _worker(session, "proxima")
    job = Job(kind=JobKind.PROXY, sequence_id=sequence.id, state=JobState.QUEUED)
    session.add(job)
    session.commit()
    sequence.frame_count = 14_400  # four minutes at 59.94
    sequence.merged_path = "merged/x.mp4"
    session.add(sequence)
    session.commit()

    dispatch.claim(session, worker)
    dispatch.complete(
        session, job.id, worker.id,
        {"proxy_path": "proxies/x.mp4", "proxy_width": 1706, "proxy_height": 960},
        elapsed_s=600.0,
    )

    session.refresh(worker)
    assert worker.observed["proxy_fps"] == pytest.approx(24.0)
    assert worker.observed["proxy_fps_n"] == 1


def test_a_second_measurement_moves_the_average_without_replacing_it(session: Session, sequence: Sequence):
    """One unlucky job must not rewrite what a machine has proved, and a machine
    that genuinely changed must still be followed within a handful of jobs."""
    worker = _worker(session, "proxima")
    worker.observed = {"render_fps": 20.0, "render_fps_n": 1}
    session.add(worker)
    session.commit()

    render = Render(sequence_id=sequence.id, template="h_1080", start_frame=0, end_frame=999)
    session.add(render)
    session.commit()
    job = Job(kind=JobKind.RENDER, render_id=render.id, state=JobState.RUNNING, worker_id=worker.id)
    session.add(job)
    session.commit()

    dispatch.observe(session, job, worker.id, {}, elapsed_s=25.0)  # 1000 frames → 40/s
    session.commit()

    session.refresh(worker)
    # 0.7 * 20 + 0.3 * 40
    assert worker.observed["render_fps"] == pytest.approx(26.0)
    assert worker.observed["render_fps_n"] == 2


def test_a_job_too_small_to_mean_anything_is_not_learned_from(session: Session, sequence: Sequence):
    """A 30-frame cut measures process startup, which is exactly the bias the
    benchmark already has. Learning it would teach the same lie twice."""
    worker = _worker(session, "proxima")
    render = Render(sequence_id=sequence.id, template="h_1080", start_frame=0, end_frame=29)
    session.add(render)
    session.commit()
    job = Job(kind=JobKind.RENDER, render_id=render.id, state=JobState.RUNNING, worker_id=worker.id)
    session.add(job)
    session.commit()

    dispatch.observe(session, job, worker.id, {}, elapsed_s=1.0)
    session.commit()

    session.refresh(worker)
    assert worker.observed == {}


def test_a_job_with_no_measured_time_is_not_learned_from(session: Session, sequence: Sequence):
    """A result posted without an elapsed time (an older worker) is still a valid
    result; it just teaches nothing."""
    worker = _worker(session, "proxima")
    render = Render(sequence_id=sequence.id, template="h_1080", start_frame=0, end_frame=9999)
    session.add(render)
    session.commit()
    job = Job(kind=JobKind.RENDER, render_id=render.id, state=JobState.RUNNING, worker_id=worker.id)
    session.add(job)
    session.commit()

    dispatch.observe(session, job, worker.id, {}, elapsed_s=0.0)
    session.commit()

    session.refresh(worker)
    assert worker.observed == {}


# --------------------------------------------------------------------------- #
# Whose files are these
# --------------------------------------------------------------------------- #

def test_a_worker_reading_the_dispatchers_volume_is_told_so(session: Session):
    """Compared, never configured: a worker wrongly told it shares the volume goes
    looking for files that are not there and fails every job it takes."""
    ours = ensure_volume_id()
    assert ours

    shared = dispatch.upsert_worker(session, "local", {}, 1, volume_id=ours)
    assert shared.shares_data is True

    remote = dispatch.upsert_worker(session, "desktop", {}, 1, volume_id="something-else")
    assert remote.shares_data is False

    silent = dispatch.upsert_worker(session, "old", {}, 1)
    assert silent.shares_data is False


def test_marking_a_volume_twice_keeps_the_first_mark(session: Session):
    """Otherwise every API restart would tell every worker its cache is worthless."""
    first = ensure_volume_id()
    assert ensure_volume_id() == first
    assert read_volume_id() == first


# --------------------------------------------------------------------------- #
# The bandwidth probe
# --------------------------------------------------------------------------- #

def test_the_bandwidth_probe_sends_exactly_what_was_asked():
    """A ceiling, not a cost: the worker closes the connection once it has timed a
    second's worth, so what matters is that the stream is well-formed."""
    import asyncio

    from app.api.worker_api import bandwidth

    async def drain(chunks) -> int:
        # Starlette wraps the sync generator into an async one.
        return sum([len(chunk) async for chunk in chunks])

    response = bandwidth(size=(1 << 20) + 123)
    assert asyncio.run(drain(response.body_iterator)) == (1 << 20) + 123


def test_an_unreachable_dispatcher_measures_no_link():
    """Unknown, not zero. A worker whose link could not be timed must not look
    infinitely slow and drop out of the running."""
    from app.worker import Dispatcher

    # Port 1 is reserved and nothing listens there.
    assert Dispatcher("http://127.0.0.1:1").link_mbps() is None


# --------------------------------------------------------------------------- #
# Where a job goes: the cost of running it, transfer included
# --------------------------------------------------------------------------- #

def _ranked(session: Session, name: str, *, render_fps: float, link: float | None, shares: bool):
    worker = dispatch.upsert_worker(
        session, name, {}, 1,
        rates={"render_fps": render_fps, "merge_mbps": 500.0, "link_mbps": link},
    )
    worker.shares_data = shares
    session.add(worker)
    session.commit()
    return worker


def test_sharing_the_volume_costs_no_transfer(session: Session):
    local = _ranked(session, "nas", render_fps=10.0, link=100.0, shares=True)
    assert dispatch.cost(local, JobKind.RENDER, 1000, 4000, 4000, fallback_rate=0.0) == 100.0


def test_what_comes_back_is_priced_even_when_the_input_is_already_there(session: Session):
    """The hole this replaced: folding the return trip into a multiplier on the
    *missing* input made the output cost vanish exactly when a cached master made the
    input free. Measured, a 451 MB master renders to 114 MB, so a render sends back
    about a third of what it read."""
    remote = _ranked(session, "desktop", render_fps=10.0, link=100.0, shares=False)

    # Nothing cached: 1000 frames at 10/s, plus 4000 MB in and 0.3 x 4000 MB back.
    assert dispatch.cost(remote, JobKind.RENDER, 1000, 4000, 4000, 0.0) == pytest.approx(152.0)
    # Master already held: the download is free, the render still has to travel home.
    assert dispatch.cost(remote, JobKind.RENDER, 1000, 0, 4000, 0.0) == pytest.approx(112.0)


def test_an_unmeasured_link_is_not_a_slow_link(session: Session):
    """Unknown, as everywhere else here. Assuming it is slow would quietly exclude the
    machine from every job that has to travel."""
    remote = _ranked(session, "desktop", render_fps=10.0, link=None, shares=False)
    assert dispatch.cost(remote, JobKind.RENDER, 1000, 4000, 4000, 0.0) == 100.0


def test_an_unmeasured_rate_stands_in_at_what_the_others_measured(session: Session):
    """Not slow: unknown. A worker whose benchmark step failed must stay in the running
    rather than be quietly excluded from every job of that kind."""
    blank = dispatch.upsert_worker(session, "blank", {}, 1)
    blank.shares_data = True
    session.add(blank)
    session.commit()
    assert dispatch.cost(blank, JobKind.RENDER, 1000, 0, 0, fallback_rate=20.0) == 50.0


def test_a_faster_machine_wins_a_render_when_the_master_is_already_there(
    session: Session, sequence: Sequence
):
    """The whole point of tracking what a worker holds: a second cut of a sequence
    already fetched must not be priced as another 4 GB transfer."""
    sequence.merged_path = "merged/master.mp4"
    sequence.frame_count = 12_000
    session.add(sequence)
    session.commit()
    render = Render(sequence_id=sequence.id, template="h_1080", start_frame=0, end_frame=11_999)
    session.add(render)
    session.commit()
    job = Job(kind=JobKind.RENDER, sequence_id=sequence.id, render_id=render.id)
    session.add(job)
    session.commit()

    slow = _ranked(session, "nas", render_fps=5.0, link=100.0, shares=True)
    fast = _ranked(session, "desktop", render_fps=25.0, link=100.0, shares=False)
    fast.cached = ["merged/master.mp4"]
    session.add(fast)
    session.commit()

    scored = dispatch._score(session, job, [slow, fast], magnitude=12_000)
    assert scored[0][2].name == "desktop"


def test_a_worker_steps_aside_for_one_that_is_clearly_better(
    session: Session, sequence: Sequence
):
    """It will be asked for the job within the second, since every worker polls that
    often. Holding it is worth more than starting it five times slower."""
    sequence.merged_path = "merged/master.mp4"
    sequence.frame_count = 12_000
    session.add(sequence)
    session.commit()
    render = Render(sequence_id=sequence.id, template="h_1080", start_frame=0, end_frame=11_999)
    session.add(render)
    session.commit()
    session.add(Job(kind=JobKind.RENDER, sequence_id=sequence.id, render_id=render.id))
    session.commit()

    slow = _ranked(session, "nas", render_fps=5.0, link=100.0, shares=True)
    fast = _ranked(session, "desktop", render_fps=25.0, link=100.0, shares=True)

    assert dispatch._pick(session, slow, [slow, fast]) is None
    assert dispatch._pick(session, fast, [slow, fast]) is not None


def test_nothing_is_held_for_a_worker_that_is_not_available(
    session: Session, sequence: Sequence
):
    """A job held for a machine that is busy or gone would be a job nobody runs. The
    pool is the whole guard: it holds only workers that are online with room to spare."""
    sequence.merged_path = "merged/master.mp4"
    sequence.frame_count = 12_000
    session.add(sequence)
    session.commit()
    render = Render(sequence_id=sequence.id, template="h_1080", start_frame=0, end_frame=11_999)
    session.add(render)
    session.commit()
    session.add(Job(kind=JobKind.RENDER, sequence_id=sequence.id, render_id=render.id))
    session.commit()

    slow = _ranked(session, "nas", render_fps=5.0, link=100.0, shares=True)
    fast = _ranked(session, "desktop", render_fps=25.0, link=100.0, shares=True)

    # The fast one is offline: it is not in the pool, so nothing is held for it.
    fast.last_seen_at = utcnow() - timedelta(seconds=dispatch.ONLINE_S + 5)
    session.add(fast)
    session.commit()
    assert dispatch.available(session) == [slow]
    assert dispatch._pick(session, slow, dispatch.available(session)) is not None


def test_a_near_tie_is_taken_rather_than_deferred(session: Session, sequence: Sequence):
    """Two workers score each other from slightly different data: each knows its own
    cache exactly and the other's as of its last poll. On a near-tie both taking it is
    a race with a winner; both deferring would be a stall with none."""
    sequence.merged_path = "merged/master.mp4"
    sequence.frame_count = 12_000
    session.add(sequence)
    session.commit()
    render = Render(sequence_id=sequence.id, template="h_1080", start_frame=0, end_frame=11_999)
    session.add(render)
    session.commit()
    session.add(Job(kind=JobKind.RENDER, sequence_id=sequence.id, render_id=render.id))
    session.commit()

    one = _ranked(session, "aaa", render_fps=20.0, link=100.0, shares=True)
    two = _ranked(session, "bbb", render_fps=22.0, link=100.0, shares=True)

    # 10% apart, under the margin: whoever asks first gets it.
    assert dispatch._pick(session, one, [one, two]) is not None
    assert dispatch._pick(session, two, [one, two]) is not None


def test_a_single_worker_always_takes_the_next_job(session: Session, sequence: Sequence):
    """The normal deployment here. All of the scoring has to collapse to the plain
    queue behaviour when there is only one candidate."""
    enqueue_merge(session, sequence)
    lone = _worker(session, "nas")

    taken = dispatch.claim(session, lone)
    assert taken is not None
    assert taken[0].kind == JobKind.MERGE


# --------------------------------------------------------------------------- #
# What a spec says has to travel
# --------------------------------------------------------------------------- #

def test_a_spec_names_its_inputs_with_their_sizes(session: Session, sequence: Sequence):
    """A worker holding its own copy has to know what to fetch and how big it is
    before it can start, and only the dispatcher can see the files."""
    enqueue_merge(session, sequence)
    worker = _worker(session, "nas")

    taken = dispatch.claim(session, worker)
    assert taken is not None
    _, spec = taken

    assert [item["path"] for item in spec["inputs"]] == spec["parts"]
    assert all(item["bytes"] > 0 for item in spec["inputs"])


def test_listing_the_inputs_of_a_job_changes_nothing(session: Session, sequence: Sequence):
    """Scoring calls this for jobs that end up going elsewhere. Marking a sequence as
    merging while merely pricing it would flag work nobody took."""
    enqueue_merge(session, sequence)
    job = session.exec(select(Job)).first()
    assert job is not None

    before = sequence.state
    inputs = dispatch.job_inputs(session, job)
    session.refresh(sequence)

    assert len(inputs) == 2
    assert sequence.state == before


def test_a_hardlink_is_not_a_merge_rate(session: Session, sequence: Sequence):
    """A single-part sequence is hardlinked, not merged. Measured on a real 451 MB
    rush: 0.28 s, which reads as 1592 MB/s and is not a throughput at all. Worse than
    useless for ranking, since a worker holding its own copy has to copy those bytes
    and would look thirty times slower for being honest about it."""
    worker = _worker(session, "nas")
    job = Job(kind=JobKind.MERGE, sequence_id=sequence.id, state=JobState.RUNNING, worker_id=worker.id)
    session.add(job)
    session.commit()

    dispatch.observe(session, job, worker.id, {**_merge_result(), "method": "hardlink"}, 0.28)
    session.commit()
    session.refresh(worker)
    assert worker.observed == {}

    dispatch.observe(session, job, worker.id, _merge_result(), 20.0)
    session.commit()
    session.refresh(worker)
    assert worker.observed["merge_mbps"] > 0


def test_a_worker_that_said_goodbye_is_gone_at_once(session: Session, sequence: Sequence):
    """Measured on 2026-08-19: with the local worker stopped and a render queued, the
    remote worker correctly stepped aside for a machine that was cheaper and no longer
    there, and the job sat for 59 s until `is_online` lapsed. A lease expiry is the
    right backstop for a worker that vanished, and the wrong answer for one that said
    goodbye."""
    worker = _worker(session, "nas")
    assert dispatch.is_online(worker)

    dispatch.release(session, worker.id or 0)

    session.refresh(worker)
    assert not dispatch.is_online(worker)
    assert dispatch.available(session) == []


def test_two_grades_of_one_rush_are_not_duplicates(session: Session, sequence: Sequence):
    """The startup sweep keys on what a job writes, not on the rush it belongs to.

    Keyed on the sequence, it dropped every render but the first when a rush was
    stabilized in two formats at once, and every grade but the first once a clip could
    hold several looks. Silently, at the next API restart, leaving a row queued for ever
    with no job behind it. Measured on 2026-08-25: three queued grade jobs on two rushes
    came back as two, then as none.
    """
    render = Render(
        sequence_id=sequence.id,  # type: ignore[arg-type]
        template="h_1080",
        state=RenderState.DONE,
        out_path="out/clip.mp4",
    )
    session.add(render)
    session.commit()
    session.refresh(render)

    ids = []
    for look in (7400, 5200):
        grade = Grade(render_id=render.id, label=f"{look} K", params={"temperature": look})  # type: ignore[arg-type]
        session.add(grade)
        session.commit()
        session.refresh(grade)
        job = Job(
            kind=JobKind.GRADE,
            state=JobState.QUEUED,
            sequence_id=sequence.id,
            render_id=render.id,
            grade_id=grade.id,
            payload={"grade_id": grade.id},
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        ids.append(job.id)

    assert dispatch.drop_stale_jobs(session) == (0, 0)
    left = session.exec(select(Job).where(Job.kind == JobKind.GRADE)).all()
    assert sorted(job.id for job in left) == sorted(ids)


def test_two_proxies_of_one_rush_are_still_duplicates(session: Session, sequence: Sequence):
    """The half of the rule that was right: a proxy is one per rush, and the second
    would write over the first one's output."""
    for _ in range(2):
        session.add(
            Job(kind=JobKind.PROXY, state=JobState.QUEUED, sequence_id=sequence.id, payload={})
        )
    session.commit()

    assert dispatch.drop_stale_jobs(session) == (0, 1)
    assert len(session.exec(select(Job).where(Job.kind == JobKind.PROXY)).all()) == 1
