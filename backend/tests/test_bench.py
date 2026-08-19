"""Turning progress lines into a rate, and surviving a machine that cannot be measured.

The measuring itself needs ffmpeg, Gyroflow and a real rush, so it is not what these
tests are about. What is testable without any of that is the arithmetic that turns a
stream of progress reports into one number, and the promise that a worker whose
benchmark fails is still a worker that runs.
"""

from __future__ import annotations

import time

from app.config import settings
from app.services import bench
from app.services.bench import _Rate
from app.services.capabilities import Capabilities


def test_a_rate_is_timed_between_the_first_and_last_progress():
    """Not from the process launch: Gyroflow spends 1.4 s of a 3.4 s render loading
    lens profiles, and counting that would report a machine 40% slower than it is."""
    rate = _Rate()
    rate.started = time.monotonic() - 10.0  # a long, irrelevant startup
    now = time.monotonic()
    rate.first = (0.1, now)
    rate.last = (0.9, now + 2.0)
    # 80% of 100 frames in 2 s
    assert rate.rate(100) == 40.0


def test_zero_progress_measures_nothing():
    """A tool that printed no progress leaves the rate unknown, and unknown is a
    thing the dispatcher handles. Inventing a number here would be worse."""
    assert _Rate().rate(100) is None


def test_a_single_progress_line_still_yields_something():
    """The normal path for the proxy step, not an edge case: a proxy of this clip
    finishes before ffmpeg's second `-progress` report."""
    rate = _Rate()
    rate.started = time.monotonic()
    rate.first = rate.last = (1.0, rate.started + 0.5)
    assert rate.rate(30) == 60.0


def test_progress_going_nowhere_falls_back_on_the_elapsed_time():
    """Two reports at the same fraction is not a rate of zero, it is one sample."""
    rate = _Rate()
    rate.started = time.monotonic()
    rate.first = (0.5, rate.started + 1.0)
    rate.last = (0.5, rate.started + 1.5)
    assert rate.rate(60) == 20.0  # 0.5 of 60 frames in the 1.5 s it took to get there


def test_a_zero_progress_report_is_ignored():
    """ffmpeg opens with `frame=0`, which says nothing about speed."""
    rate = _Rate()
    rate(0.0)
    assert rate.first is None


def test_a_missing_clip_leaves_the_worker_unranked_rather_than_broken(tmp_path, monkeypatch):
    """No clip means no ranking, and that is all it means. The API image ships
    without one, and a dev checkout has none either."""
    monkeypatch.setattr(settings, "bench_clip", tmp_path / "nope.mp4")
    result = bench.measure(Capabilities())

    assert result.render_fps is None
    assert result.proxy_fps is None
    assert any("no benchmark clip" in note for note in result.notes)


def test_an_unreadable_clip_is_reported_not_raised(tmp_path, monkeypatch):
    """A truncated file must not take the worker down on startup."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not a video")
    monkeypatch.setattr(settings, "bench_clip", clip)
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    result = bench.measure(Capabilities())

    assert result.notes
    assert result.render_fps is None
