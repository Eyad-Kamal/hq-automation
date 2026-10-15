import pytest

from hq.logic import scoring


def test_effective_rating_uses_rating_when_present():
    assert scoring.effective_rating(1350, "Hard") == 1350
    assert scoring.effective_rating(0, None) == 0


@pytest.mark.parametrize(
    "difficulty, expected",
    [("Easy", 1000), ("Medium", 1400), ("Hard", 1900), (None, 800), ("Unknown", 800)],
)
def test_effective_rating_difficulty_mapping(difficulty, expected):
    assert scoring.effective_rating(None, difficulty) == expected


def test_effective_rating_custom_map_and_default():
    assert scoring.effective_rating(None, "Easy", lc_map={"Easy": 900}) == 900
    assert scoring.effective_rating(None, None, default=1100) == 1100


def test_points_example_r800_ref1200_is_half():
    assert scoring.points(800, 1200, None) == 0.5


def test_points_example_r1200_ref1200_is_244():
    assert scoring.points(1200, 1200, None) == 2.44


def test_points_example_r1600_ref1200_is_919():
    assert scoring.points(1600, 1200, None) == 9.19


def test_points_upsolve_multiplier():
    # 2.440061... * 0.7 = 1.708... -> 1.71
    assert scoring.points(1200, 1200, "Upsolve") == 1.71


def test_points_editorial_multiplier():
    # 2.440061... * 0.5 = 1.220... -> 1.22
    assert scoring.points(1200, 1200, "With Editorial/Hint") == 1.22


def test_points_unknown_or_live_mode_has_multiplier_one():
    assert scoring.points(1200, 1200, "Live Contest") == 2.44
    assert scoring.points(1200, 1200, "Virtual") == 2.44
    assert scoring.points(1200, 1200, None) == 2.44


def test_points_custom_mode_factors_are_honoured():
    assert scoring.points(1200, 1200, "Upsolve", upsolve=0.5) == 1.22
    assert scoring.points(1200, 1200, "With Editorial/Hint", editorial=0.25) == 0.61


def test_points_s_clamped_at_upper_bound_two():
    # r=2400, ref=800 -> raw s = 2^4 = 16, clamped to 2.
    w = (2400 / 800) ** 2.2
    expected = scoring.round_half_up(w * 2.0, 2)
    assert scoring.points(2400, 800, None) == expected


def test_points_s_clamped_at_lower_bound_half():
    # r=400, ref=2000 -> raw s = 2^-4, clamped to 0.5.
    w = (400 / 800) ** 2.2
    expected = scoring.round_half_up(w * 0.5, 2)
    assert scoring.points(400, 2000, None) == expected


def test_points_is_monotone_in_rating_for_fixed_ref():
    assert scoring.points(1400, 1200, None) > scoring.points(1200, 1200, None)


def test_points_result_has_two_decimals():
    value = scoring.points(1337, 1100, None)
    assert value == round(value, 2)


def test_round_half_up_matches_notion_for_exact_halves():
    # Notion: round(x*100)/100 rounds 12.5 up to 13.
    assert scoring.round_half_up(0.125, 2) == 0.13
    assert scoring.round_half_up(0.5, 2) == 0.5


def test_round_half_up_plain_values():
    assert scoring.round_half_up(2.4400, 2) == 2.44
    assert scoring.round_half_up(2.4449, 2) == 2.44
    # 0.375 is exact in binary, so 37.5 -> 38 (half up).
    assert scoring.round_half_up(0.375, 2) == 0.38
