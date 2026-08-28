"""A 4 GB rush arrives in pieces, and comes back out byte for byte.

The public chain caps one request at 100 MiB (measured on the real Cloudflare path:
104 857 600 bytes pass, one more is refused at the edge), so a rush cannot be sent in
a single PUT from outside the LAN. It is cut client-side and reassembled here.

What these hold onto is the part that can go silently wrong: a piece lost on a flaky
link leaves a hole in a preallocated file, and a file renamed with a hole in it fails
much later, in a merge or a stabilization, far from the cause.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import pipeline
from app.api.routes import router
from app.config import settings

NAME = "DJI_20260809144616_0034_D.MP4"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """The real routes on a throwaway app: no lifespan, no background loops."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.ensure_dirs()
    target = FastAPI()
    target.include_router(router)
    return TestClient(target)


@pytest.fixture()
def payload() -> bytes:
    """Distinctive enough that a misplaced chunk cannot pass for the right one."""
    return bytes((i * 7 + 13) % 256 for i in range(300_000))


def begin(client: TestClient, size: int, name: str = NAME) -> str:
    response = client.post(f"/api/upload/begin?filename={name}&size={size}")
    assert response.status_code == 200, response.text
    return response.json()["partial"]


def send(client: TestClient, partial: str, data: bytes, offset: int):
    return client.put(f"/api/upload/{partial}/chunk?offset={offset}", content=data[offset:])


def send_piece(client: TestClient, partial: str, data: bytes, offset: int, length: int):
    return client.put(
        f"/api/upload/{partial}/chunk?offset={offset}", content=data[offset : offset + length]
    )


def pieces(size: int, chunk: int) -> list[tuple[int, int]]:
    return [(o, min(chunk, size - o)) for o in range(0, size, chunk)]


def test_a_file_sent_in_pieces_arrives_byte_for_byte(client, payload):
    """Out of order on purpose: chunks travel concurrently, so nothing may depend on
    the order they land in."""
    partial = begin(client, len(payload))
    plan = pieces(len(payload), 100_000)

    for offset, length in reversed(plan):
        assert send_piece(client, partial, payload, offset, length).status_code == 200

    response = client.post(f"/api/upload/{partial}/finish")
    assert response.status_code == 200, response.text
    landed = settings.inbox_dir / response.json()["filename"]
    assert landed.read_bytes() == payload
    assert response.json()["size_bytes"] == len(payload)


def test_finish_refuses_a_file_with_a_hole_and_says_where(client, payload):
    """The client is the one thing that cannot be trusted about completeness: a chunk
    lost to a dropped tunnel is exactly what it would not know about."""
    partial = begin(client, len(payload))
    plan = pieces(len(payload), 100_000)
    for offset, length in plan:
        if offset == 100_000:
            continue  # the middle piece never arrives
        send_piece(client, partial, payload, offset, length)

    refused = client.post(f"/api/upload/{partial}/finish")

    assert refused.status_code == 409
    assert "100000" in refused.json()["detail"]
    assert not (settings.inbox_dir / NAME).exists()

    send_piece(client, partial, payload, 100_000, 100_000)
    accepted = client.post(f"/api/upload/{partial}/finish")
    assert accepted.status_code == 200
    assert (settings.inbox_dir / accepted.json()["filename"]).read_bytes() == payload


def test_a_resent_piece_is_the_same_piece(client, payload):
    """Retrying one chunk is the whole point of cutting the file up: it must not
    corrupt the result nor leave the count thinking a range is missing."""
    partial = begin(client, len(payload))
    for offset, length in pieces(len(payload), 100_000):
        send_piece(client, partial, payload, offset, length)
    assert send_piece(client, partial, payload, 100_000, 100_000).status_code == 200

    response = client.post(f"/api/upload/{partial}/finish")

    assert response.status_code == 200
    assert (settings.inbox_dir / response.json()["filename"]).read_bytes() == payload


def test_a_piece_running_past_the_announced_size_is_refused(client, payload):
    partial = begin(client, len(payload))

    too_far = client.put(
        f"/api/upload/{partial}/chunk?offset={len(payload) - 10}", content=b"x" * 100
    )
    past_the_end = client.put(
        f"/api/upload/{partial}/chunk?offset={len(payload)}", content=b"x"
    )

    assert too_far.status_code == 400
    assert past_the_end.status_code == 416


def test_a_name_that_is_not_an_upload_in_progress_is_refused(client, payload):
    """The name comes from a URL, so it is checked against the shape rather than
    sanitised, which covers traversal without reasoning about separators."""
    begin(client, len(payload))

    for name in ["../../etc/passwd", "..%2Fx.partial", "nothing.partial", NAME]:
        response = client.put(f"/api/upload/{name}/chunk?offset=0", content=b"x")
        assert response.status_code in (400, 404), f"{name} was accepted"


def test_two_files_of_the_same_name_each_get_their_own_destination(client, payload):
    """The destination is resolved once, at begin. Resolving it per request would
    give the second chunk of a colliding name a file of its own."""
    first = begin(client, len(payload))
    second = begin(client, len(payload))

    assert first != second
    for partial in (first, second):
        for offset, length in pieces(len(payload), 150_000):
            send_piece(client, partial, payload, offset, length)
        assert client.post(f"/api/upload/{partial}/finish").status_code == 200

    landed = sorted(p.name for p in settings.inbox_dir.glob("*.MP4"))
    assert landed == [NAME, "DJI_20260809144616_0034_D__1.MP4"]


def test_giving_up_leaves_nothing_behind(client, payload):
    partial = begin(client, len(payload))
    send_piece(client, partial, payload, 0, 100_000)

    assert client.delete(f"/api/upload/{partial}").status_code == 200

    assert not (settings.inbox_dir / partial).exists()
    assert list((settings.inbox_dir / ".uploads").iterdir()) == []


def test_an_unsupported_name_is_refused_before_anything_is_written(client):
    bad_extension = client.post("/api/upload/begin?filename=notes.txt&size=10")
    hidden = client.post("/api/upload/begin?filename=.hidden.mp4&size=10")

    assert bad_extension.status_code == 415
    assert hidden.status_code == 400
    assert list(settings.inbox_dir.iterdir()) == [settings.inbox_dir / ".uploads"] or not any(
        p.name.endswith(".partial") for p in settings.inbox_dir.iterdir()
    )


def test_the_scan_holds_off_between_two_pieces_of_one_file(client, payload):
    """The 2026-08-20 bug, now with a narrower window. Cutting a file up means the
    in-flight count opens and closes between pieces, so what covers the gap is
    UPLOAD_SETTLE_S, which is orders of magnitude longer than one."""
    partial = begin(client, len(payload))
    send_piece(client, partial, payload, 0, 100_000)

    assert pipeline.uploads_in_flight() == 0, "the request is over"
    assert pipeline.uploading() is True, "the batch is not"


def test_a_partial_is_invisible_to_the_scanner_until_it_is_finished(client, payload):
    """Half a rush must never be ingested, and the markers must not look like rushes."""
    partial = begin(client, len(payload))
    send_piece(client, partial, payload, 0, 100_000)

    assert pipeline._candidate_files() == []

    for offset, length in pieces(len(payload), 100_000)[1:]:
        send_piece(client, partial, payload, offset, length)
    response = client.post(f"/api/upload/{partial}/finish")

    landed = settings.inbox_dir / response.json()["filename"]
    assert pipeline._candidate_files() == [landed]
    # Flagged as complete by construction, so the scan fired right after does not
    # have to wait two seconds to watch a size that will never change again.
    assert str(landed) in pipeline._completed_uploads


def test_an_abandoned_upload_is_swept_with_its_markers(client, payload):
    """Nothing used to clean these up, and the file is preallocated to its final
    size: one interrupted 4 GB rush held 3.4 GB of the volume for two days."""
    import os
    partial = begin(client, len(payload))
    send_piece(client, partial, payload, 0, 100_000)
    path = settings.inbox_dir / partial
    old = time.time() - 7200
    os.utime(path, (old, old))

    gone = pipeline.sweep_abandoned_uploads()

    assert gone == [partial]
    assert not path.exists()
    assert list((settings.inbox_dir / ".uploads").iterdir()) == []


def test_an_upload_still_being_written_is_not_swept(client, payload):
    """A live upload rewrites its .partial on every chunk, so its mtime is seconds
    old. This is the whole reason the test is on mtime and not on coverage."""
    partial = begin(client, len(payload))
    send_piece(client, partial, payload, 0, 100_000)

    assert pipeline.sweep_abandoned_uploads() == []
    assert (settings.inbox_dir / partial).exists()


def test_the_check_reports_an_upload_left_half_done(client, payload):
    """Otherwise the page says "new" about a rush already most of the way there: the
    fingerprint matches nothing, because a .partial never became a clip."""
    partial = begin(client, len(payload))
    send_piece(client, partial, payload, 0, 100_000)

    found = pipeline.partial_for(NAME)

    assert found is not None
    path, received = found
    assert path.name == partial
    assert received == 100_000
    assert path.stat().st_size == len(payload)


def test_the_check_says_nothing_when_no_upload_is_pending(client):
    assert pipeline.partial_for(NAME) is None
    assert pipeline.partial_for("../../etc/passwd") is None
