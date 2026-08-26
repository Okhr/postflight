"""An unknown path under /api must be a 404, not the single-page app.

The SPA fallback is a catch-all on `/{full_path:path}`, so before this it answered a
mistyped endpoint with 200 and a page of HTML. The client then failed parsing JSON
somewhere else entirely, which is the kind of error that costs an hour to trace back to
a typo. A POST to the same path got 405, because the catch-all was GET-only and the path
still matched: wrong verb is a different diagnosis from wrong path, and it sent people
looking in the wrong place.

What decides all of this is the order of the route table, so that is what these read,
off a throwaway app rather than off the constants that built it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException

from app import main
from app.api.routes import router
from app.config import settings


@pytest.fixture()
def mounted(tmp_path, monkeypatch) -> FastAPI:
    """A fresh app with the real API router and a frontend to fall back on."""
    monkeypatch.setattr(settings, "static_dir", tmp_path)
    (tmp_path / "index.html").write_text("<!doctype html>")
    target = FastAPI()
    target.include_router(router)
    main._mount_frontend(target)
    return target


def paths(target: FastAPI) -> list[str]:
    return [getattr(route, "path", "") for route in target.routes]


def test_the_catch_all_sits_behind_the_real_api_routes(mounted):
    """The property that breaks if the mount ever moves above the routers, and the one
    a unit test would otherwise have to take on trust."""
    order = paths(mounted)

    assert order.index("/api/status") < order.index("/api/{rest:path}")
    assert order.index("/api/{rest:path}") < order.index("/{full_path:path}")


def test_the_catch_all_answers_every_method_the_api_uses(mounted):
    """Read off the route the app registered, not off the list that registered it."""
    route = next(r for r in mounted.routes if getattr(r, "path", "") == "/api/{rest:path}")

    assert route.methods >= {"GET", "POST", "PUT", "PATCH", "DELETE"}


def test_the_spa_fallback_is_still_there_for_everything_else(mounted):
    """The point is to carve /api out of the fallback, not to remove it: a deep link
    like /color/12/3 has to keep reaching index.html."""
    assert "/{full_path:path}" in paths(mounted)


def test_an_unknown_api_path_names_itself_in_the_404(mounted):
    with pytest.raises(HTTPException) as caught:
        main.api_not_found("sequences/nope")

    assert caught.value.status_code == 404
    assert "/api/sequences/nope" in caught.value.detail


def test_no_catch_all_at_all_without_a_built_frontend(tmp_path, monkeypatch):
    """It exists only to counteract the fallback. With no frontend there is no
    fallback, and an unknown path already 404s on its own."""
    monkeypatch.setattr(settings, "static_dir", tmp_path / "absent")
    target = FastAPI()
    target.include_router(router)

    main._mount_frontend(target)

    assert "/api/{rest:path}" not in paths(target)
    assert "/{full_path:path}" not in paths(target)
