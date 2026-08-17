from datetime import datetime, timezone

from app.services.naming import parse_filename


def test_dji_goggles_timestamp_is_utc():
    """O3/O4 filenames carry UTC time, not local time.

    Verified on a real rush: name at 14:48:28 for a local mtime of 16:52 in summer
    time (UTC+2), i.e. 14:52 UTC = start + duration.
    """
    parsed = parse_filename("DJI_20260811144828_0044_D.MP4")
    assert parsed.recorded_at == datetime(2026, 8, 11, 14, 48, 28, tzinfo=timezone.utc)
    assert parsed.camera_index == 44
    assert parsed.kind == "dji_goggles"


def test_dji_goggles_without_suffix():
    parsed = parse_filename("DJI_20260811144828_0044.MP4")
    assert parsed.camera_index == 44
    assert parsed.kind == "dji_goggles"


def test_dji_action_has_no_timestamp():
    parsed = parse_filename("DJI_0001_0044.MP4")
    assert parsed.recorded_at is None
    assert parsed.camera_index == 44
    assert parsed.group_key == "0001"


def test_gopro_chapter_is_the_incrementing_part():
    """On GoPro the chapter increments while the clip number stays fixed."""
    first = parse_filename("GX010042.MP4")
    second = parse_filename("GX020042.MP4")
    assert (first.camera_index, second.camera_index) == (1, 2)
    assert first.group_key == second.group_key == "0042"


def test_unknown_name_is_not_grouped_by_index():
    parsed = parse_filename("rush-du-samedi.mp4")
    assert parsed.camera_index is None
    assert parsed.kind == "unknown"
