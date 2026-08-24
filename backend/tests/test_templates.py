"""Gyroflow templates: editing one must not lose what the form does not show.

A template is a partial Gyroflow project. The interface edits seven settings of it;
the file holds a good deal more (per-axis smoothing, adaptive zoom, encoder options),
and every one of those has to survive a save.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import routes, schemas
from app.config import settings
from app.services import gyroflow


@pytest.fixture
def templates(tmp_path, monkeypatch):
    """A data dir of its own, seeded from the bundled templates."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.ensure_dirs()
    gyroflow.seed_templates()
    return settings.templates_dir


def _patch(template_id: str, **values) -> schemas.TemplateOut:
    return routes.update_template(template_id, schemas.TemplatePatch(**values))


# --------------------------------------------------------------------------- #
# What the interface reads
# --------------------------------------------------------------------------- #

def test_the_seven_settings_come_out_of_the_file(templates):
    [horizontal] = [t for t in routes.get_templates() if t.id == "h_1080"]

    assert (horizontal.width, horizontal.height) == (1920, 1080)
    assert horizontal.codec == "H.265/HEVC"
    # 0.2, not Gyroflow's 0.5: the bundled template smooths harder than stock.
    assert horizontal.smoothness == 0.2
    assert horizontal.lens_correction == 1.0
    assert horizontal.bundled is True


def test_the_aspect_is_derived_not_stored(templates):
    """So it cannot contradict the dimensions printed next to it."""
    _patch("h_1080", width=1080, height=1920)
    assert next(t for t in routes.get_templates() if t.id == "h_1080").aspect == "9:16"

    raw = json.loads((templates / "h_1080.json").read_text())
    assert "aspect" not in raw["$meta"]


def test_the_defaults_are_gyroflows_own(templates):
    defaults = routes.get_template_defaults()

    # Read off `gyroflow --export-project 1`, not chosen by us.
    assert defaults.smoothness == 0.5
    assert defaults.lens_correction == 1.0
    assert defaults.codec == "H.264/AVC"
    assert "H.265/HEVC" in defaults.codecs


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #

def test_a_save_keeps_everything_the_form_never_showed(templates):
    """The whole reason a save patches instead of rewriting."""
    before = json.loads((templates / "h_1080.json").read_text())

    _patch("h_1080", smoothness=0.35)

    after = json.loads((templates / "h_1080.json").read_text())
    assert after["stabilization"]["adaptive_zoom_window"] == before["stabilization"][
        "adaptive_zoom_window"
    ]
    assert after["output"]["encoder_options"] == before["output"]["encoder_options"]
    assert after["stabilization"]["max_zoom"] == before["stabilization"]["max_zoom"]
    # And the per-axis smoothing params, which the form does not offer either.
    names = {p["name"] for p in after["stabilization"]["smoothing_params"]}
    assert {"smoothness_pitch", "smoothness_yaw", "smoothness_roll"} <= names


def test_smoothness_is_written_into_the_list_it_lives_in(templates):
    saved = _patch("h_1080", smoothness=0.35)
    assert saved.smoothness == 0.35

    raw = json.loads((templates / "h_1080.json").read_text())
    entry = next(
        p for p in raw["stabilization"]["smoothing_params"] if p["name"] == "smoothness"
    )
    assert entry["value"] == 0.35


def test_the_frame_offset_is_a_pair(templates):
    saved = _patch("v_1080", frame_offset_y=-0.4)

    assert (saved.frame_offset_x, saved.frame_offset_y) == (0.0, -0.4)
    raw = json.loads((templates / "v_1080.json").read_text())
    assert raw["stabilization"]["adaptive_zoom_center_offset"] == [0.0, -0.4]


def test_one_axis_of_the_offset_does_not_wipe_the_other(templates):
    _patch("v_1080", frame_offset_x=0.25)
    saved = _patch("v_1080", frame_offset_y=-0.4)

    assert (saved.frame_offset_x, saved.frame_offset_y) == (0.25, -0.4)


def test_a_rename_leaves_the_settings_alone(templates):
    saved = _patch("h_1080", label="Landscape")

    assert (saved.label, saved.smoothness, saved.width) == ("Landscape", 0.2, 1920)


def test_an_empty_patch_is_refused(templates):
    with pytest.raises(HTTPException) as raised:
        routes.update_template("h_1080", schemas.TemplatePatch())
    assert raised.value.status_code == 400


def test_an_unknown_codec_is_refused(templates):
    with pytest.raises(HTTPException) as raised:
        _patch("h_1080", codec="H.266/VVC")
    assert raised.value.status_code == 400


def test_an_unknown_template_is_a_404(templates):
    with pytest.raises(HTTPException) as raised:
        _patch("nope", smoothness=0.5)
    assert raised.value.status_code == 404


def test_an_odd_height_is_refused_by_the_schema(templates):
    """4:2:0 chroma is subsampled by two, and x264 refuses an odd height outright."""
    with pytest.raises(Exception):
        schemas.TemplatePatch(height=1081)


# --------------------------------------------------------------------------- #
# Creating and removing
# --------------------------------------------------------------------------- #

def test_a_new_template_starts_from_gyroflow_defaults(templates):
    created = routes.create_template(schemas.TemplateIn(label="Square 1080"))

    assert created.id == "square_1080"
    assert created.smoothness == 0.5
    assert created.bundled is False


def test_a_new_template_can_copy_an_existing_one(templates):
    created = routes.create_template(
        schemas.TemplateIn(label="Vertical 4K", copy_of="v_1080")
    )

    assert (created.smoothness, created.width, created.height) == (0.2, 1080, 1920)
    assert created.label == "Vertical 4K"


def test_two_templates_with_the_same_name_get_different_ids(templates):
    first = routes.create_template(schemas.TemplateIn(label="Square"))
    second = routes.create_template(schemas.TemplateIn(label="Square"))

    assert (first.id, second.id) == ("square", "square_2")


def test_a_template_made_here_is_deleted_for_good(templates):
    created = routes.create_template(schemas.TemplateIn(label="Square"))

    assert routes.delete_template(created.id)["outcome"] == "deleted"
    assert [t.id for t in routes.get_templates()] == ["h_1080", "v_1080"]


def test_a_bundled_template_is_deleted_for_good(templates):
    """It used to reset instead, which made one icon mean two things. Now it goes, and
    the seeding on the next start must not bring it back."""
    _patch("h_1080", smoothness=0.9)

    assert routes.delete_template("h_1080")["outcome"] == "deleted"
    assert [t.id for t in routes.get_templates()] == ["v_1080"]

    gyroflow.seed_templates()
    assert [t.id for t in routes.get_templates()] == ["v_1080"]


def test_the_other_bundled_templates_still_seed(templates):
    """The removal list names one id, it does not stop the seeding."""
    routes.delete_template("h_1080")
    (templates / "v_1080.json").unlink()

    gyroflow.seed_templates()

    assert [t.id for t in routes.get_templates()] == ["v_1080"]


def test_deleting_something_that_never_existed_is_a_404(templates):
    with pytest.raises(HTTPException) as raised:
        routes.delete_template("nope")
    assert raised.value.status_code == 404


def test_a_template_id_cannot_escape_its_directory(templates):
    """It names a file, so it is checked rather than trusted."""
    with pytest.raises(HTTPException):
        routes.delete_template("../../etc/passwd")


def test_a_new_template_landing_on_a_deleted_id_is_listed(templates):
    """The removal is remembered by id, and an id comes from the label, so a label
    that lands on a deleted one has to clear it: otherwise the file would be written
    and never listed."""
    routes.delete_template("h_1080")

    made = routes.create_template(schemas.TemplateIn(label="H 1080"))

    assert made.id == "h_1080"
    assert [t.id for t in routes.get_templates()] == ["h_1080", "v_1080"]
    assert not (templates / gyroflow.REMOVED_FILE).exists()


# --------------------------------------------------------------------------- #
# What the preset forces on the way out
# --------------------------------------------------------------------------- #

def _preset(codec: str, **output) -> dict:
    template = gyroflow.Template(
        id="t", label="t", data={"output": {"codec": codec, **output}}
    )
    return gyroflow.build_preset(template, [[0.0, 1000.0]], Path("/tmp"), "out.mp4")["output"]


def test_h264_comes_out_8_bit():
    """Gyroflow follows the source's bit depth, and a 10-bit rush made H.264 High 10:
    a profile no browser decodes. Measured on a 10-bit source, an empty pixel_format
    gives yuv420p10le back and this one gives High/yuv420p."""
    assert _preset("H.264/AVC")["pixel_format"] == "yuv420p"


def test_hevc_keeps_its_depth():
    """10-bit HEVC is worth having, and it is not the codec meant to be shared."""
    assert _preset("H.265/HEVC").get("pixel_format", "") == ""


def test_a_template_that_names_a_format_keeps_it():
    assert _preset("H.264/AVC", pixel_format="yuv422p10le")["pixel_format"] == "yuv422p10le"


def test_the_gop_is_closed():
    """Gyroflow emits one IDR at frame zero and open-GOP I-frames after it, which no
    browser can seek to. Measured on an 8 s render: 9 keyframes and 1 IDR without the
    flag, 9 and 9 with it."""
    assert "-flags +cgop" in _preset("H.264/AVC", encoder_options="-preset superfast")["encoder_options"]
    assert _preset("H.264/AVC")["encoder_options"] == "-flags +cgop"


def test_a_template_that_already_closes_it_is_left_alone():
    options = "-preset veryfast -x264-params open-gop=0:cgop=1"
    assert _preset("H.264/AVC", encoder_options=options)["encoder_options"] == options
