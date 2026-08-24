"""The black and white points, and the button that measures them.

They are two parameters like the others, with one difference that shapes the page:
they belong to a clip. What is unused range on this shot is picture on the next, so
they sit above a separator and the copy dialog leaves them alone.

The split is deliberate: `levels` is arithmetic on what the sliders say, and
`suggest_levels` is the judgement about a particular clip. Only the second one has
any business reading an analysis.
"""

from __future__ import annotations

from app.services import grading


def _levels(**points):
    return grading.levels(grading.merge_params(points))


def test_the_default_points_leave_the_clip_alone():
    assert _levels() is None
    assert grading.build_filters(grading.merge_params({})) == []


def test_a_black_point_stretches_from_there():
    low, gain = _levels(black_point=0.2)
    # 20% into the legal range, and everything above it scaled to reach white.
    assert round(low, 4) == round(grading.BLACK_N + 0.2 * (grading.WHITE_N - grading.BLACK_N), 4)
    assert round(gain, 3) == 1.25


def test_a_white_point_alone_also_counts():
    assert _levels(white_point=0.8) is not None


def test_points_that_meet_are_refused():
    """A stretch that steep is a mis-drag, not an intention."""
    assert _levels(black_point=0.5, white_point=0.52) is None


def test_the_points_reach_the_filter_chain():
    chain = grading.build_filters(grading.merge_params({"black_point": 0.2}))
    assert len(chain) == 1 and chain[0].startswith("lutyuv=y=")


# --------------------------------------------------------------------------- #
# What the button proposes
# --------------------------------------------------------------------------- #

MEASURED = {"y_low": 213.2, "y_high": 755.8, "clipped_white": 0.22, "clipped_black": 0.0}


def test_it_proposes_the_unused_range(): 
    """Measured on a real clip: blacks sit at 213 on a 64-940 scale, so 17% of the
    range below them is doing nothing."""
    assert grading.suggest_levels(dict(MEASURED, clipped_white=0.0)) == {
        "black_point": 0.1703,
        "white_point": 0.7897,
    }


def test_a_side_that_already_clips_is_left_alone():
    """Pushing the white point of a clip whose sky touches the ceiling blows the sky
    out completely, which is why the share of clipped frames is measured at all."""
    assert grading.suggest_levels(MEASURED) == {"black_point": 0.1703, "white_point": 1.0}


def test_a_clip_that_fills_the_range_gets_no_proposal():
    assert grading.suggest_levels(
        {"y_low": 70.0, "y_high": 935.0, "clipped_white": 0.0, "clipped_black": 0.0}
    ) is None


def test_nothing_to_propose_without_an_analysis():
    assert grading.suggest_levels(None) is None
    assert grading.suggest_levels({}) is None


def test_the_proposal_is_a_look_the_sliders_can_hold():
    """Whatever it proposes has to be applicable as parameters, or the button would
    write something the page cannot show."""
    proposal = grading.suggest_levels(MEASURED)
    assert grading.levels(grading.merge_params(proposal)) is not None
