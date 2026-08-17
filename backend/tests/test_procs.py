"""Graceful shutdown of running children.

Not a cosmetic detail: a child SIGKILLed while it had VAAPI decode jobs in flight
left this project's dev machine with a deadlocked GPU (AMD's out-of-tree amdgpu
waiting on a fence that never signalled), recoverable only by rebooting. What these
tests guard is that a shutdown asks nicely and waits.
"""

from __future__ import annotations

import threading
import time

from app.services import procs


def _run_in_background(cmd: list[str]) -> threading.Thread:
    thread = threading.Thread(
        target=lambda: procs.run_with_progress(cmd),
        daemon=True,
    )
    thread.start()
    return thread


def _wait_for_child(timeout: float = 5.0) -> list:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with procs._running_lock:
            children = list(procs._running)
        if children:
            return children
        time.sleep(0.05)
    raise AssertionError("child never registered")


def test_running_children_are_registered_then_forgotten():
    thread = _run_in_background(["sleep", "30"])
    children = _wait_for_child()
    assert len(children) == 1

    procs.terminate_all(grace=5.0)
    thread.join(timeout=5.0)
    with procs._running_lock:
        assert not procs._running


def test_terminate_all_sends_sigterm_not_sigkill():
    thread = _run_in_background(["sleep", "30"])
    children = _wait_for_child()

    assert procs.terminate_all(grace=5.0) == 1
    thread.join(timeout=5.0)
    # -15 is SIGTERM: the child was asked to stop and got to release whatever it
    # held. -9 would mean the grace period lapsed and it was killed outright.
    assert children[0].returncode == -15


def test_terminate_all_with_nothing_running():
    assert procs.terminate_all(grace=1.0) == 0
