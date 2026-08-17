from datetime import datetime, timedelta, timezone

from app.services.grouping import ClipInfo, chain_clips, sequence_hash

TOLERANCE = 2.0
BASE = datetime(2026, 8, 11, 14, 48, 28, tzinfo=timezone.utc)


def clip(cid: int, start: datetime, duration_ms: float, index: int | None, **overrides) -> ClipInfo:
    fields = dict(
        width=3840, height=2880, fps_num=60000, fps_den=1001, codec="hevc", group_key="D"
    )
    fields.update(overrides)
    return ClipInfo(
        id=cid,
        filename=f"clip{cid}.MP4",
        recorded_at=start,
        duration_ms=duration_ms,
        camera_index=index,
        **fields,  # type: ignore[arg-type]
    )


def test_real_split_pair_is_chained():
    """The real measured case: 0.36 s between the end of part 1 and the start of
    part 2 (222.639 s long, second part started at 14:52:11)."""
    first = clip(1, BASE, 222639.083, 44)
    second = clip(2, BASE + timedelta(seconds=223), 15915.9, 45)
    groups = chain_clips([first, second], TOLERANCE)
    assert len(groups) == 1
    assert [c.id for c in groups[0]] == [1, 2]


def test_two_separate_flights_stay_separate():
    first = clip(1, BASE, 222639.0, 44)
    # Took off again five minutes after the first part ended.
    second = clip(2, BASE + timedelta(seconds=523), 60000.0, 45)
    groups = chain_clips([first, second], TOLERANCE)
    assert len(groups) == 2


def test_non_consecutive_index_breaks_the_chain():
    """Same time contiguity, but a file is missing in between: do not merge, or we
    would glue two pieces together and hide the hole."""
    first = clip(1, BASE, 222639.0, 44)
    second = clip(2, BASE + timedelta(seconds=223), 15915.0, 46)
    assert len(chain_clips([first, second], TOLERANCE)) == 2


def test_different_resolution_breaks_the_chain():
    first = clip(1, BASE, 222639.0, 44)
    second = clip(2, BASE + timedelta(seconds=223), 15915.0, 45, width=1920, height=1080)
    assert len(chain_clips([first, second], TOLERANCE)) == 2


def test_three_parts_chain_transitively():
    a = clip(1, BASE, 200000.0, 44)
    b = clip(2, BASE + timedelta(milliseconds=200000), 200000.0, 45)
    c = clip(3, BASE + timedelta(milliseconds=400000), 50000.0, 46)
    groups = chain_clips([a, b, c], TOLERANCE)
    assert len(groups) == 1
    assert [x.id for x in groups[0]] == [1, 2, 3]


def test_input_order_does_not_matter():
    a = clip(1, BASE, 200000.0, 44)
    b = clip(2, BASE + timedelta(milliseconds=200000), 50000.0, 45)
    assert [c.id for c in chain_clips([b, a], TOLERANCE)[0]] == [1, 2]


def test_missing_index_falls_back_to_timing_only():
    a = clip(1, BASE, 200000.0, None)
    b = clip(2, BASE + timedelta(milliseconds=200000), 50000.0, None)
    assert len(chain_clips([a, b], TOLERANCE)) == 1


def test_sequence_hash_is_stable_for_the_same_parts():
    assert sequence_hash(["aaa", "bbb"]) == sequence_hash(["aaa", "bbb"])


def test_sequence_hash_depends_on_order():
    """Two parts joined the other way round are a different video."""
    assert sequence_hash(["aaa", "bbb"]) != sequence_hash(["bbb", "aaa"])


def test_sequence_hash_separates_parts():
    """Concatenating the fingerprints without a separator would collide."""
    assert sequence_hash(["aa", "abbb"]) != sequence_hash(["aaa", "bbb"])


def test_sequence_hash_of_one_part_differs_from_two():
    assert sequence_hash(["aaa"]) != sequence_hash(["aaa", "aaa"])
