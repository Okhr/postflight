"""The analysis has to mean the same thing whatever the clip's bit depth.

Written after breaking it: signalstats reports in the source's own depth, while every
constant in `grading` is 10-bit legal range. The renders happened to be 10-bit until
the day they were made 8-bit, and the same picture then measured `y_high` 183 on a
64-940 scale with every frame "clipping black", so the button that reads those numbers
went quiet.

Real ffmpeg, on two encodes of one synthetic source: it is the conversion that is
under test, and mocking it would test nothing.
"""

from __future__ import annotations

import subprocess

import pytest

from app.services import grading


def _clip(path, pix_fmt: str, profile: str) -> None:
    """A luma ramp: a real low end, a real high end, and the same every run.

    `gradients` was the first source used here and it seeds itself at random, so the
    two encodes held different pictures and the comparison meant nothing.
    """
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=320x180:rate=10:duration=1",
         "-vf", "format=yuv420p,geq=lum='X*255/W':cb=128:cr=128",
         "-c:v", "libx264", "-profile:v", profile, "-pix_fmt", pix_fmt, str(path)],
        check=True, capture_output=True, timeout=120,
    )


@pytest.fixture
def pair(tmp_path):
    eight, ten = tmp_path / "eight.mp4", tmp_path / "ten.mp4"
    _clip(eight, "yuv420p", "high")
    _clip(ten, "yuv420p10le", "high10")
    return eight, ten


def test_the_same_picture_measures_the_same_at_either_depth(pair):
    eight, ten = pair
    a, b = grading.analyse(eight).to_dict(), grading.analyse(ten).to_dict()

    # Two encodes of one source, so a few levels of codec difference are expected and
    # a scale error would be four times off, not five levels.
    assert abs(a["y_low"] - b["y_low"]) < 20
    assert abs(a["y_high"] - b["y_high"]) < 20


def test_an_8_bit_clip_reads_on_the_10_bit_scale(pair):
    """The bug in one assertion: an 8-bit clip cannot report a white point that only
    a very dark 10-bit clip could have."""
    measured = grading.analyse(pair[0]).to_dict()

    assert measured["y_high"] > grading.LEGAL_BLACK + grading.LEGAL_SPAN * 0.25
    assert measured["y_low"] <= measured["y_high"]
