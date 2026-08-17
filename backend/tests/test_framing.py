"""Frame arithmetic at 60000/1001 fps.

This is where derush precision is won or lost: rounding to milliseconds drifts by
plusieurs images sur un rush de quatre minutes.
"""

import pytest

from app.framing import (
    cut_to_trim_range_ms,
    duration_to_frames,
    format_timecode,
    frame_to_ms,
    ms_to_frame,
)

NUM, DEN = 60000, 1001


def test_frame_to_ms_is_exact_not_rounded_to_5994():
    # 600 frames at 59.94 fps = exactly 10.010 s, not 10.000 s.
    assert frame_to_ms(600, NUM, DEN) == 10010.0


def test_round_trip_over_a_long_rush():
    """No drift across the 14299 frames of a four-minute rush."""
    for frame in (0, 1, 599, 6000, 9000, 14298):
        assert ms_to_frame(frame_to_ms(frame, NUM, DEN), NUM, DEN) == frame


def test_duration_to_frames_matches_the_measured_file():
    # Real case: a merged file of 238554.983 ms → 14299 frames.
    assert duration_to_frames(238554.983, NUM, DEN) == 14299


def test_trim_range_covers_the_last_kept_frame():
    """Gyroflow's upper bound is exclusive: to keep frame `end`, aim at the end of
    that frame, hence `end + 1`."""
    start, end = cut_to_trim_range_ms(6000, 6599, NUM, DEN)
    assert start == frame_to_ms(6000, NUM, DEN)
    assert end == frame_to_ms(6600, NUM, DEN)
    # 600 frames kept
    assert (end - start) == frame_to_ms(600, NUM, DEN)


def test_trim_range_of_a_single_frame():
    start, end = cut_to_trim_range_ms(10, 10, NUM, DEN)
    assert end > start
    # approx: subtracting two float milliseconds drags a 1e-15 error along
    assert (end - start) == pytest.approx(frame_to_ms(1, NUM, DEN))


def test_integer_fps_still_works():
    assert frame_to_ms(30, 30, 1) == 1000.0
    assert ms_to_frame(1000.0, 30, 1) == 30


def test_timecode_formatting():
    assert format_timecode(0, NUM, DEN) == "00:00.000"
    assert format_timecode(6000, NUM, DEN) == "01:40.100"
    assert format_timecode(14298, NUM, DEN).startswith("03:58")
