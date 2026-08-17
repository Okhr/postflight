"""Frame ↔ millisecond conversions.

Derush marks are stored as **frame numbers**, never as milliseconds: at
60000/1001 fps (59.94), rounding to milliseconds drifts by several frames over a
four-minute rush. We keep the fps as an exact rational and convert at the last
moment, when we have to talk to Gyroflow (`trim_ranges_ms`).
"""

from __future__ import annotations

from fractions import Fraction


def fps_fraction(fps_num: int, fps_den: int) -> Fraction:
    if fps_num <= 0 or fps_den <= 0:
        raise ValueError(f"fps invalide: {fps_num}/{fps_den}")
    return Fraction(fps_num, fps_den)


def frame_to_ms(frame: int, fps_num: int, fps_den: int) -> float:
    """Timestamp of the *start* of that frame, in milliseconds."""
    return float(Fraction(frame) * 1000 * Fraction(fps_den, fps_num))


def ms_to_frame(ms: float, fps_num: int, fps_den: int) -> int:
    """Frame holding that instant (floor, with a hair of float tolerance)."""
    exact = Fraction(ms).limit_denominator(10**6) * Fraction(fps_num, fps_den) / 1000
    return int(exact + Fraction(1, 1000))


def duration_to_frames(duration_ms: float, fps_num: int, fps_den: int) -> int:
    return max(0, round(Fraction(duration_ms).limit_denominator(10**6) * Fraction(fps_num, fps_den) / 1000))


def cut_to_trim_range_ms(start_frame: int, end_frame: int, fps_num: int, fps_den: int) -> list[float]:
    """Turn an inclusive [start_frame, end_frame] cut into a Gyroflow range.

    Gyroflow treats the upper bound as exclusive: we aim at the end of the last
    kept frame, hence `end_frame + 1`.
    """
    if end_frame < start_frame:
        raise ValueError("end_frame < start_frame")
    return [
        frame_to_ms(start_frame, fps_num, fps_den),
        frame_to_ms(end_frame + 1, fps_num, fps_den),
    ]


def format_timecode(frame: int, fps_num: int, fps_den: int) -> str:
    total_ms = frame_to_ms(frame, fps_num, fps_den)
    total_s, ms = divmod(int(round(total_ms)), 1000)
    m, s = divmod(total_s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{m:02d}:{s:02d}.{ms:03d}"
