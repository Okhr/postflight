"""Moving files to a worker that cannot see the volume, and back.

Two things here can go badly wrong and neither would be loud. Missing an output leaves
the dispatcher holding a path with no file behind it, and the failure surfaces a step
later. Counting a fetched input as an output ships a 4 GB master straight back where it
came from, and nothing fails at all. So both directions are pinned down.

The third is eviction, which deletes footage. It cannot run on the dispatcher's volume
because a `Workspace` only exists when the worker does not share it, but what it spares
when it does run is worth stating.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import blobs
from app.config import settings
from app.transport import Workspace


class FakeClient:
    """Records what would have crossed the network, and fakes the bytes."""

    def __init__(self) -> None:
        self.fetched: list[str] = []
        self.sent: list[str] = []
        self.payload = b"pulled bytes"

    def download(self, rel, dest, on_bytes=None) -> int:
        self.fetched.append(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        if on_bytes:
            on_bytes(len(self.payload))
        return len(self.payload)

    def upload(self, rel, source) -> int:
        self.sent.append(rel)
        return source.stat().st_size


@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.ensure_dirs()
    return tmp_path


def _noop(*_args, **_kwargs) -> None:
    pass


# --------------------------------------------------------------------------- #
# What has to travel, and what does not
# --------------------------------------------------------------------------- #

def test_an_input_already_held_is_not_fetched_again(volume):
    """Path and size are the whole cache check: nothing in this pipeline is ever
    rewritten in place, so a path that matches is the same bytes."""
    (volume / "merged").mkdir(exist_ok=True)
    (volume / "merged" / "x.mp4").write_bytes(b"0123456789")
    client = FakeClient()

    Workspace(client).pull([{"path": "merged/x.mp4", "bytes": 10}], _noop)

    assert client.fetched == []


def test_an_input_held_at_the_wrong_size_is_fetched_again(volume):
    """A half-finished transfer from a previous attempt must not pass for a master."""
    (volume / "merged").mkdir(exist_ok=True)
    (volume / "merged" / "x.mp4").write_bytes(b"trunc")
    client = FakeClient()

    Workspace(client).pull([{"path": "merged/x.mp4", "bytes": 4_000_000}], _noop)

    assert client.fetched == ["merged/x.mp4"]


def test_everything_the_job_wrote_is_sent_back(volume):
    """Not just what the result names: the proxy step also writes a poster, a
    filmstrip and a gyro chart that no result field mentions, and the interface reads
    every one of them."""
    workspace = Workspace(FakeClient())
    before = workspace.pull([], _noop)

    (volume / "proxies" / "seq.mp4").write_bytes(b"proxy")
    (volume / "proxies" / "seq.poster.jpg").write_bytes(b"poster")
    (volume / "proxies" / "seq.gyro.json").write_bytes(b"{}")

    client = FakeClient()
    sent = Workspace(client).publish(before, _noop)

    assert sorted(sent) == [
        "proxies/seq.gyro.json",
        "proxies/seq.mp4",
        "proxies/seq.poster.jpg",
    ]


def test_a_fetched_input_is_never_sent_back(volume):
    """The snapshot is taken after the fetch precisely so this cannot happen: sending
    a freshly pulled 4 GB master back would be silent and slow."""
    client = FakeClient()
    workspace = Workspace(client)
    before = workspace.pull([{"path": "merged/master.mp4", "bytes": 999}], _noop)
    assert client.fetched == ["merged/master.mp4"]

    (volume / "out" / "render.mp4").write_bytes(b"render")
    assert workspace.publish(before, _noop) == ["out/render.mp4"]


def test_a_job_that_wrote_nothing_sends_nothing(volume):
    workspace = Workspace(FakeClient())
    before = workspace.pull([], _noop)
    assert workspace.publish(before, _noop) == []


def test_scratch_files_never_travel(volume):
    """`tmp/` fills with partial encodes and probe samples, and the database is the
    dispatcher's alone."""
    workspace = Workspace(FakeClient())
    before = workspace.pull([], _noop)

    (volume / "tmp" / "partial.mp4").write_bytes(b"scratch")
    (volume / "db" / "postflight.sqlite3").write_bytes(b"not yours")

    assert workspace.publish(before, _noop) == []


# --------------------------------------------------------------------------- #
# Eviction
# --------------------------------------------------------------------------- #

def test_eviction_drops_the_oldest_footage_first(volume, monkeypatch):
    """By mtime and not by access time: `relatime` refreshes atime once a day at most,
    so an LRU built on it would be a coin toss dressed up as a heuristic."""
    monkeypatch.setattr(settings, "worker_cache_bytes", 300)
    import os

    for name, mtime in (("old.mp4", 1_000), ("recent.mp4", 2_000)):
        path = volume / "merged" / name
        path.write_bytes(b"x" * 200)
        os.utime(path, (mtime, mtime))

    freed = Workspace(FakeClient()).evict(needed=100)

    assert freed == 200
    assert not (volume / "merged" / "old.mp4").exists()
    assert (volume / "merged" / "recent.mp4").exists()


def test_eviction_spares_the_inputs_of_the_job_it_is_making_room_for(volume, monkeypatch):
    """A two-part merge whose first part is cached would otherwise have it deleted to
    make room for the second, and then have to fetch both."""
    monkeypatch.setattr(settings, "worker_cache_bytes", 300)
    (volume / "raw" / "part1.mp4").write_bytes(b"x" * 250)
    client = FakeClient()

    Workspace(client).pull(
        [{"path": "raw/part1.mp4", "bytes": 250}, {"path": "raw/part2.mp4", "bytes": 250}],
        _noop,
    )

    assert (volume / "raw" / "part1.mp4").exists()
    assert client.fetched == ["raw/part2.mp4"]


def test_no_cap_means_no_eviction(volume, monkeypatch):
    """A volume you watch yourself is a legitimate choice, and deleting footage
    nobody asked to delete is not."""
    monkeypatch.setattr(settings, "worker_cache_bytes", 0)
    (volume / "merged" / "x.mp4").write_bytes(b"x" * 5_000)

    assert Workspace(FakeClient()).evict(needed=10**9) == 0
    assert (volume / "merged" / "x.mp4").exists()


def test_only_footage_is_reported_as_cached(volume):
    """The dispatcher prices transfers, and a 2 kB template is not a transfer."""
    (volume / "merged" / "x.mp4").write_bytes(b"master")
    (volume / "templates" / "h_1080.json").write_bytes(b"{}")

    assert Workspace(FakeClient()).cached() == ["merged/x.mp4"]


# --------------------------------------------------------------------------- #
# What the dispatcher is willing to serve
# --------------------------------------------------------------------------- #

def test_a_path_climbing_out_of_the_volume_is_refused(volume):
    with pytest.raises(HTTPException) as raised:
        blobs._resolve("../../etc/passwd", for_write=False)
    assert raised.value.status_code == 403


def test_the_database_is_not_a_blob(volume):
    """The queue travels over the worker endpoints; the SQLite file never travels."""
    with pytest.raises(HTTPException) as raised:
        blobs._resolve("db/postflight.sqlite3", for_write=False)
    assert raised.value.status_code == 403


def test_a_worker_may_not_write_where_the_pipeline_does_not_produce(volume):
    """The inbox is watched by the API on a volume a worker does not even see."""
    with pytest.raises(HTTPException) as raised:
        blobs._resolve("inbox/DJI_0001.MP4", for_write=True)
    assert raised.value.status_code == 403

    # And what it does produce is fine.
    assert blobs._resolve("merged/x.mp4", for_write=True).name == "x.mp4"


def test_reading_a_master_is_allowed(volume):
    assert blobs._resolve("raw/DJI_0001.MP4", for_write=False).name == "DJI_0001.MP4"
