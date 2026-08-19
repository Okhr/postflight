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
    Job,
    JobKind,
    JobState,
    Sequence,
    SequenceState,
    Worker,
    utcnow,
)
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
