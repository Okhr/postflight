"""The environment prefix, which fails silently when it is wrong.

Renaming the project from `VS_` to `PF_` was mostly a search and replace, and the one
occurrence it missed was `env_prefix="VS_"` itself. Nothing broke: every setting simply
fell back to its default, and a worker whose `PF_WORKER_NAME=local` was ignored
registered under its container hostname instead, leaving an orphaned row behind. There
is no error to catch here, only a wrong value that looks like a deliberate one.
"""

from __future__ import annotations

from app.config import Settings


def test_settings_read_the_documented_prefix(monkeypatch):
    monkeypatch.setenv("PF_WORKER_NAME", "some-machine")
    monkeypatch.setenv("PF_PROXY_HEIGHT", "720")

    settings = Settings()

    assert settings.worker_name == "some-machine"
    assert settings.proxy_height == 720


def test_the_former_prefix_is_not_read_any_more(monkeypatch):
    """Both prefixes being live would be worse than either: two names for one setting,
    and whichever wins is invisible."""
    monkeypatch.delenv("PF_WORKER_NAME", raising=False)
    monkeypatch.setenv("VS_WORKER_NAME", "stale")

    assert Settings().worker_name == ""
