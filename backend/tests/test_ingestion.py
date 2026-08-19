"""What the inbox lets through, what it sets aside, and what it refuses outright.

The rules here come from a real 622 GB O3/O4 collection rather than from guesses:
13 masters out of 15 sampled carry the old `DJI_0327.MP4` name with no timestamp at
all, so their start time can only come from the container.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app import pipeline
from app.config import settings
from app.models import Clip, ClipState
from app.services.grouping import ClipInfo, chain_clips
from app.services.naming import parse_filename
from app.services.probe import ProbeResult

BASE = datetime(2025, 8, 22, 17, 25, 25, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# The name
# --------------------------------------------------------------------------- #

def test_the_old_o3_name_is_read():
    """`DJI_0327.MP4`, an index and nothing else. Unrecognized until 2026-08-19,
    which cost the grouping its strongest signal on most of the collection."""
    parsed = parse_filename("DJI_0327.MP4")
    assert parsed.kind == "dji_legacy"
    assert parsed.camera_index == 327
    assert parsed.group_key == ""
    # The name holds no timestamp: the start time can only come from the container.
    assert parsed.recorded_at is None


def test_the_timestamped_goggles_name_still_wins():
    parsed = parse_filename("DJI_20260703172854_0020_D.MP4")
    assert parsed.kind == "dji_goggles"
    assert parsed.camera_index == 20
    assert parsed.recorded_at == datetime(2026, 7, 3, 17, 28, 54, tzinfo=timezone.utc)


def test_a_hand_joined_master_claims_no_index():
    """`DJI_0044_0045_joined.MP4` exists in the collection, from an older manual
    workflow. It is one whole recording, so having no index is right: nothing should
    ever be chained onto it, and only the timing can say so."""
    parsed = parse_filename("DJI_0044_0045_joined.MP4")
    assert parsed.camera_index is None


def test_a_two_number_action_name_is_not_read_as_a_legacy_one():
    parsed = parse_filename("DJI_0001_0044.MP4")
    assert parsed.kind == "dji_action"
    assert parsed.camera_index == 44


# --------------------------------------------------------------------------- #
# Grouping legacy parts, which have no timestamp in the name
# --------------------------------------------------------------------------- #

def _legacy(cid: int, start: datetime, duration_ms: float, index: int | None) -> ClipInfo:
    """A legacy O3 part: 3840x2160 h264, empty group key, start time from the probe."""
    return ClipInfo(
        id=cid,
        filename=f"DJI_{300 + cid:04d}.MP4",
        recorded_at=start,
        duration_ms=duration_ms,
        width=3840,
        height=2160,
        fps_num=60000,
        fps_den=1001,
        codec="h264",
        camera_index=index,
        group_key="",
    )


def test_a_legacy_split_pair_is_chained():
    first = _legacy(1, BASE, 222_639.0, 327)
    second = _legacy(2, BASE + timedelta(seconds=223), 15_915.0, 328)
    groups = chain_clips([first, second], 2.0)
    assert len(groups) == 1
    assert [c.id for c in groups[0]] == [1, 2]


def test_two_legacy_flights_stay_apart_even_with_consecutive_indices():
    """Consecutive numbers prove nothing on their own: DJI increments on every new
    file, separate flights included. The timing is what decides."""
    first = _legacy(1, BASE, 60_000.0, 327)
    second = _legacy(2, BASE + timedelta(seconds=360), 60_000.0, 328)
    assert len(chain_chain := chain_clips([first, second], 2.0)) == 2
    assert [len(g) for g in chain_chain] == [1, 1]


def test_a_legacy_part_is_not_chained_onto_a_goggles_part():
    """Both can sit in the same inbox, and both can end up with an empty group key.
    The profile is what keeps them apart: 2160 h264 against 2880 hevc."""
    legacy = _legacy(1, BASE, 60_000.0, 327)
    goggles = ClipInfo(
        id=2, filename="DJI_20250822172625_0328_D.MP4",
        recorded_at=BASE + timedelta(seconds=60), duration_ms=60_000.0,
        width=3840, height=2880, fps_num=60000, fps_den=1001, codec="hevc",
        camera_index=328, group_key="",
    )
    assert len(chain_clips([legacy, goggles], 2.0)) == 2


# --------------------------------------------------------------------------- #
# What gets in
# --------------------------------------------------------------------------- #

def _probe_result(has_gyro: bool = True, recorded_at: datetime | None = BASE) -> ProbeResult:
    return ProbeResult(
        duration_ms=34_200.0,
        width=3840,
        height=2160,
        fps_num=60000,
        fps_den=1001,
        codec="h264",
        size_bytes=1024,
        has_gyro=has_gyro,
        recorded_at=recorded_at,
    )


def _drop(name: str) -> None:
    """Put a file in the inbox and flag it complete, the way the upload endpoint does,
    so the stability counter does not need several scans."""
    path = settings.inbox_dir / name
    path.write_bytes(b"pretend this is a rush")
    pipeline.mark_upload_complete(path)


def test_a_master_is_ingested(session: Session, monkeypatch):
    monkeypatch.setattr(pipeline, "probe", lambda _p: _probe_result())
    _drop("DJI_0327.MP4")

    result = pipeline.scan_inbox(session)

    assert [c.filename for c in result.ingested] == ["DJI_0327.MP4"]
    assert result.rejected == []
    assert (settings.raw_dir / "DJI_0327.MP4").exists()
    clip = session.exec(select(Clip)).one()
    assert clip.camera_index == 327  # read from the legacy name
    assert clip.has_gyro


def test_a_stabilized_output_is_set_aside_not_ingested(session: Session, monkeypatch):
    """A Gyroflow output back in the inbox. Recognized by its name, which is all the
    checking this deserves: these files are not supposed to be dropped here, and a
    name check costs no probe."""
    def explode(_p):
        raise AssertionError("a file set aside on its name must never be probed")

    monkeypatch.setattr(pipeline, "probe", explode)
    for name in (
        "DJI_0044_0045_joined_stabilized.mp4",
        "DJI_0046_stabilized.mp4",
        "DJI_20260703174523_0022_D_stabilized_16x9.mp4",
    ):
        _drop(name)

    result = pipeline.scan_inbox(session)

    assert sorted(result.rejected) == sorted(
        [
            "DJI_0044_0045_joined_stabilized.mp4",
            "DJI_0046_stabilized.mp4",
            "DJI_20260703174523_0022_D_stabilized_16x9.mp4",
        ]
    )
    assert result.ingested == []
    assert session.exec(select(Clip)).all() == []
    # Set aside, never deleted.
    assert (settings.inbox_dir / ".stabilized" / "DJI_0046_stabilized.mp4").exists()
    assert not (settings.raw_dir / "DJI_0046_stabilized.mp4").exists()


def test_a_file_set_aside_is_not_picked_up_again(session: Session, monkeypatch):
    """Otherwise every scan would move it around and report it afresh."""
    monkeypatch.setattr(pipeline, "probe", lambda _p: _probe_result())
    _drop("DJI_0046_stabilized.mp4")
    pipeline.scan_inbox(session)

    again = pipeline.scan_inbox(session)
    assert again.rejected == []
    assert again.ingested == []


def test_a_clip_with_no_usable_start_time_is_refused_with_a_reason(
    session: Session, monkeypatch
):
    """Guessing wrong is worse than not knowing. Without a start time, the parts of one
    flight cannot be told from two separate flights, so the file is refused and the
    reason is recorded rather than the grouping being quietly wrong."""
    monkeypatch.setattr(pipeline, "probe", lambda _p: _probe_result(recorded_at=None))
    _drop("MYSTERY.mp4")

    result = pipeline.scan_inbox(session)

    assert result.failed == ["MYSTERY.mp4"]
    assert result.ingested == []
    clip = session.exec(select(Clip)).one()
    assert clip.state == ClipState.FAILED
    assert "no reliable start time" in (clip.error or "")
    # Not moved into raw/: nothing downstream should see it.
    assert not (settings.raw_dir / "MYSTERY.mp4").exists()


def test_the_two_sidelines_do_not_collide(session: Session, monkeypatch):
    """Duplicates and gyro-less files each have their own folder, and a scan sees
    neither of them."""
    monkeypatch.setattr(pipeline, "probe", lambda _p: _probe_result())
    _drop("DJI_0327.MP4")
    pipeline.scan_inbox(session)
    # The same bytes again: a duplicate by fingerprint.
    _drop("DJI_0327.MP4")
    result = pipeline.scan_inbox(session)

    assert result.duplicates == ["DJI_0327.MP4"]
    assert (settings.inbox_dir / ".duplicates" / "DJI_0327.MP4").exists()
    assert pipeline.scan_inbox(session).duplicates == []


# --------------------------------------------------------------------------- #
# Where the start time comes from
# --------------------------------------------------------------------------- #

FFPROBE_JSON = {
    "streams": [
        {
            "codec_type": "video", "codec_name": "h264", "width": 3840, "height": 2160,
            "r_frame_rate": "60000/1001", "avg_frame_rate": "60000/1001",
        },
        {"codec_type": "data", "codec_tag_string": "djmd"},
    ],
    "format": {"duration": "194.6", "tags": {"creation_time": "2025-08-17T09:51:19.000000Z"}},
}


def _probed(tmp_path, monkeypatch, payload, name="DJI_0330.MP4", mtime=None):
    from app.services import probe as probe_mod

    path = tmp_path / name
    path.write_bytes(b"x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    monkeypatch.setattr(probe_mod, "_run_ffprobe", lambda _p: payload)
    return probe_mod.probe(path)


def test_the_container_time_is_used_when_the_name_carries_none(tmp_path, monkeypatch):
    """The measured fix: the old O3 names hold no timestamp, and the mtime does not
    survive a copy. The container does."""
    result = _probed(tmp_path, monkeypatch, FFPROBE_JSON, mtime=1_800_000_000)
    assert result.recorded_at == datetime(2025, 8, 17, 9, 51, 19, tzinfo=timezone.utc)


def test_the_name_still_wins_over_the_container(tmp_path, monkeypatch):
    """Both agree to the second on real files, so this is only about keeping the
    camera's own statement as the reference."""
    result = _probed(
        tmp_path, monkeypatch, FFPROBE_JSON, name="DJI_20260703172854_0020_D.MP4"
    )
    assert result.recorded_at == datetime(2026, 7, 3, 17, 28, 54, tzinfo=timezone.utc)


def test_an_unset_camera_clock_is_refused_not_believed(tmp_path, monkeypatch):
    """A drone that never got the time writes an epoch date. Believing it would make
    every rush of every flight look contiguous."""
    payload = {
        **FFPROBE_JSON,
        "format": {"duration": "194.6", "tags": {"creation_time": "1970-01-01T00:00:00.000000Z"}},
    }
    result = _probed(tmp_path, monkeypatch, payload, mtime=1_800_000_000)
    assert result.recorded_at is None


def test_no_creation_time_means_no_start_time_at_all(tmp_path, monkeypatch):
    """The mtime used to fill in here, and it lied on every copied file. None is the
    honest answer, and the caller refuses the file on it."""
    for tags in ({}, {"creation_time": "not a date"}, {"creation_time": ""}):
        payload = {**FFPROBE_JSON, "format": {"duration": "10.0", "tags": tags}}
        result = _probed(tmp_path, monkeypatch, payload, mtime=1_800_000_000)
        assert result.recorded_at is None


def test_a_real_split_pair_chains_even_when_the_mtime_was_lost(tmp_path, monkeypatch):
    """The end-to-end shape of the bug: DJI_0330 and DJI_0331, copied with plain `cp`
    so both carry the same fresh mtime, which is now ignored entirely. Their real start
    times are 3 min 15 apart and the first lasts 194.6 s, so they are one recording."""
    from app.services import probe as probe_mod

    same_mtime = 1_800_000_000
    payloads = {
        "DJI_0330.MP4": ("2025-08-17T09:51:19.000000Z", 194.6),
        "DJI_0331.MP4": ("2025-08-17T09:54:34.000000Z", 77.9),
    }
    infos = []
    for index, (name, (stamp, duration)) in enumerate(payloads.items()):
        payload = {
            "streams": FFPROBE_JSON["streams"],
            "format": {"duration": str(duration), "tags": {"creation_time": stamp}},
        }
        path = tmp_path / name
        path.write_bytes(b"x")
        os.utime(path, (same_mtime, same_mtime))
        monkeypatch.setattr(probe_mod, "_run_ffprobe", lambda _p, pl=payload: pl)
        got = probe_mod.probe(path)
        infos.append(
            ClipInfo(
                id=index, filename=name, recorded_at=got.recorded_at,
                duration_ms=got.duration_ms, width=got.width, height=got.height,
                fps_num=got.fps_num, fps_den=got.fps_den, codec=got.codec,
                camera_index=parse_filename(name).camera_index,
                group_key=parse_filename(name).group_key,
            )
        )

    groups = chain_clips(infos, 2.0)
    assert len(groups) == 1, "the real pair must be recognized as one recording"


# --------------------------------------------------------------------------- #
# The part size, which is what really separates a split from a restart
# --------------------------------------------------------------------------- #

LIMIT = 3_000_000_000
SPLIT_SIZE = 3_765_216_866  # measured, the size at which the camera closes a file


def _sized(cid: int, start: datetime, duration_ms: float, index: int, size: int) -> ClipInfo:
    return ClipInfo(
        id=cid, filename=f"DJI_{300 + cid:04d}.MP4", recorded_at=start,
        duration_ms=duration_ms, width=3840, height=2160, fps_num=60000, fps_den=1001,
        codec="h264", size_bytes=size, camera_index=index, group_key="",
    )


def test_a_genuine_split_is_chained():
    """The real DJI_0330 to DJI_0331 pair: 3.765 Go, 194.6 s, followed 0.44 s later."""
    first = _sized(1, BASE, 194_600.0, 330, SPLIT_SIZE)
    second = _sized(2, BASE + timedelta(milliseconds=195_040), 77_900.0, 331, 1_531_658_711)
    assert len(chain_clips([first, second], 2.0, LIMIT)) == 1


def test_a_quick_restart_is_not_glued_to_the_next_flight():
    """The 9 pairs the timing alone got wrong. Worst real case: a first part of
    1.398 Go, so nowhere near the file limit, followed 1.11 s later. The gap says
    contiguous, the size says the pilot stopped recording, and the size is right."""
    first = _sized(1, BASE, 60_000.0, 345, 1_398_000_000)
    second = _sized(2, BASE + timedelta(milliseconds=61_110), 60_000.0, 346, 900_000_000)
    assert len(chain_clips([first, second], 2.0, LIMIT)) == 2


def test_a_three_part_recording_still_chains_end_to_end():
    """Two consecutive splits, which the collection really contains (DJI_0285 to 0287).
    Parts 1 and 2 both hit the limit, only the last one is short."""
    one = _sized(1, BASE, 193_700.0, 285, SPLIT_SIZE)
    two = _sized(2, BASE + timedelta(milliseconds=193_960), 193_700.0, 286, SPLIT_SIZE)
    three = _sized(3, BASE + timedelta(milliseconds=387_920), 42_000.0, 287, 800_000_000)
    groups = chain_clips([one, two, three], 2.0, LIMIT)
    assert len(groups) == 1
    assert [c.id for c in groups[0]] == [1, 2, 3]


def test_an_unknown_size_falls_back_on_the_timing_alone():
    """Same convention as a missing camera index: a signal we do not have must not
    block, or a source that reports no size could never be chained at all."""
    first = _sized(1, BASE, 194_600.0, 330, 0)
    second = _sized(2, BASE + timedelta(milliseconds=195_040), 77_900.0, 331, 0)
    assert len(chain_clips([first, second], 2.0, LIMIT)) == 1
