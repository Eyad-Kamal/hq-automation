import pytest

from hq.logic import mastery

BAND_LOW, BAND_HIGH = 1200, 1400
IN = 1300     # in band
BELOW = 1000  # below band
ABOVE = 1500  # above band


def solves(rating: int, n: int, mode: str = "Live Contest"):
    return [(rating, mode)] * n


def mixed(in_band: int = 0, above: int = 0, below: int = 0, mode: str = "Live Contest"):
    return solves(IN, in_band, mode) + solves(ABOVE, above, mode) + solves(BELOW, below, mode)


def tier(result):
    return result.tier


# ---- factors -------------------------------------------------------------

def test_band_factors_single_solve_points():
    assert mastery.compute([(BELOW, "Live Contest")], BAND_LOW, BAND_HIGH, False).points == 0.25
    assert mastery.compute([(IN, "Live Contest")], BAND_LOW, BAND_HIGH, False).points == 1.0
    assert mastery.compute([(ABOVE, "Live Contest")], BAND_LOW, BAND_HIGH, False).points == 1.5


def test_band_edges_are_in_band():
    result = mastery.compute([(BAND_LOW, "Practice"), (BAND_HIGH, "Practice")], BAND_LOW, BAND_HIGH, False)
    assert (result.below, result.in_band, result.above) == (0, 2, 0)
    assert result.points == 2.0


def test_mode_factors_applied():
    assert mastery.compute([(IN, "Upsolve")], BAND_LOW, BAND_HIGH, False).points == 0.7
    assert mastery.compute([(IN, "With Editorial/Hint")], BAND_LOW, BAND_HIGH, False).points == 0.5
    assert mastery.compute([(IN, "Virtual")], BAND_LOW, BAND_HIGH, False).points == 1.0


def test_band_and_mode_factors_multiply():
    result = mastery.compute([(ABOVE, "Upsolve")], BAND_LOW, BAND_HIGH, False)
    assert result.points == pytest.approx(1.05)
    result = mastery.compute([(BELOW, "With Editorial/Hint")], BAND_LOW, BAND_HIGH, False)
    assert result.points == pytest.approx(0.125)


def test_counts_and_max_rating():
    solves_list = [(BELOW, "Practice"), (IN, "Practice"), (IN, "Upsolve"), (ABOVE, "Virtual")]
    result = mastery.compute(solves_list, BAND_LOW, BAND_HIGH, False)
    assert result.count == 4
    assert (result.below, result.in_band, result.above) == (1, 2, 1)
    assert result.max_rating == ABOVE


def test_empty_solves_unseen_or_theory():
    result = mastery.compute([], BAND_LOW, BAND_HIGH, False)
    assert (result.count, result.max_rating, result.points, result.tier) == (0, 0, 0.0, "Unseen")
    assert mastery.compute([], BAND_LOW, BAND_HIGH, True).tier == "Theory"


# ---- tier boundaries -----------------------------------------------------

def test_novice_at_exactly_one_point():
    assert tier(mastery.compute(solves(IN, 1), BAND_LOW, BAND_HIGH, False)) == "Novice"


def test_below_one_point_is_theory_or_unseen():
    below_only = solves(BELOW, 3)  # 0.75 points
    assert tier(mastery.compute(below_only, BAND_LOW, BAND_HIGH, False)) == "Unseen"
    assert tier(mastery.compute(below_only, BAND_LOW, BAND_HIGH, True)) == "Theory"


def test_novice_just_below_apprentice():
    assert tier(mastery.compute(solves(IN, 4), BAND_LOW, BAND_HIGH, False)) == "Novice"


def test_apprentice_at_exactly_five():
    assert tier(mastery.compute(solves(IN, 5), BAND_LOW, BAND_HIGH, False)) == "Apprentice"


def test_apprentice_from_below_band_sums():
    # 20 below-band solves = 20 * 0.25 = 5.0 exactly
    assert tier(mastery.compute(solves(BELOW, 20), BAND_LOW, BAND_HIGH, False)) == "Apprentice"


def test_competent_at_exactly_twelve():
    assert tier(mastery.compute(solves(IN, 12), BAND_LOW, BAND_HIGH, False)) == "Competent"


def test_apprentice_just_below_competent():
    assert tier(mastery.compute(solves(IN, 11), BAND_LOW, BAND_HIGH, False)) == "Apprentice"


def test_competent_needs_no_above_band_solves():
    assert tier(mastery.compute(solves(IN, 12), BAND_LOW, BAND_HIGH, False)) == "Competent"
    assert mastery.compute(solves(IN, 12), BAND_LOW, BAND_HIGH, False).above == 0


def test_proficient_at_exactly_25_points_with_3_above():
    # 3 above-band (4.5) + 20 in-band (20) + 1 editorial in-band (0.5) = 25.0
    s = solves(ABOVE, 3) + solves(IN, 20) + [(IN, "With Editorial/Hint")]
    result = mastery.compute(s, BAND_LOW, BAND_HIGH, False)
    assert result.points == 25.0
    assert result.tier == "Proficient"


def test_competent_when_25_points_but_only_2_above():
    # 2 above (3.0) + 22 in-band (22) = 25.0, but above-band < 3
    s = solves(ABOVE, 2) + solves(IN, 22)
    result = mastery.compute(s, BAND_LOW, BAND_HIGH, False)
    assert result.points == 25.0
    assert result.tier == "Competent"


def test_master_at_exactly_40_points_with_8_above():
    # 8 above (12.0) + 28 in-band (28.0) = 40.0
    s = solves(ABOVE, 8) + solves(IN, 28)
    result = mastery.compute(s, BAND_LOW, BAND_HIGH, False)
    assert result.points == 40.0
    assert result.tier == "Master"


def test_proficient_when_40_points_but_only_7_above():
    # 7 above (10.5) + 29.5 in-band -> use 29 in-band plus 1 editorial in-band (0.5)
    s = solves(ABOVE, 7) + solves(IN, 29) + [(IN, "With Editorial/Hint")]
    result = mastery.compute(s, BAND_LOW, BAND_HIGH, False)
    assert result.points == 40.0
    assert result.above == 7
    assert result.tier == "Proficient"


def test_competent_when_40_points_but_only_2_above_and_not_proficient():
    # Points ok for Master but above-band count 2 fails both above thresholds.
    s = solves(ABOVE, 2) + solves(IN, 37)
    result = mastery.compute(s, BAND_LOW, BAND_HIGH, False)
    assert result.points == 40.0
    assert result.tier == "Competent"


def test_master_point_just_below_forty_is_proficient():
    # 39.5 points with 8 above-band solves -> Proficient (not Master).
    s = solves(ABOVE, 8) + solves(IN, 27) + [(IN, "With Editorial/Hint")]
    result = mastery.compute(s, BAND_LOW, BAND_HIGH, False)
    assert result.points == 39.5
    assert result.tier == "Proficient"


# ---- float noise ---------------------------------------------------------

def test_twenty_upsolves_in_band_is_exactly_fourteen_points_competent():
    # 20 * 0.7 accumulates float error; rounding keeps it at 14.0 -> Competent.
    result = mastery.compute(solves(IN, 20, mode="Upsolve"), BAND_LOW, BAND_HIGH, False)
    assert result.points == 14.0
    assert result.tier == "Competent"


def test_ten_upsolves_is_seven_points_apprentice():
    result = mastery.compute(solves(IN, 10, mode="Upsolve"), BAND_LOW, BAND_HIGH, False)
    assert result.points == 7.0
    assert result.tier == "Apprentice"


# ---- cfg -----------------------------------------------------------------

def test_custom_tier_threshold_from_cfg():
    cfg = {"TIER_NOVICE": 2}
    assert mastery.compute(solves(IN, 1), BAND_LOW, BAND_HIGH, False, cfg).tier == "Unseen"
    assert mastery.compute(solves(IN, 2), BAND_LOW, BAND_HIGH, False, cfg).tier == "Novice"


def test_custom_band_factor_from_cfg():
    cfg = {"BAND_BELOW_FACTOR": 0.5}
    assert mastery.compute([(BELOW, "Practice")], BAND_LOW, BAND_HIGH, False, cfg).points == 0.5


def test_custom_mode_factor_from_cfg():
    cfg = {"MODE_UPSOLVE": 0.9}
    assert mastery.compute([(IN, "Upsolve")], BAND_LOW, BAND_HIGH, False, cfg).points == 0.9


def test_none_value_in_cfg_falls_back_to_default():
    cfg = {"TIER_NOVICE": None}
    assert mastery.compute(solves(IN, 1), BAND_LOW, BAND_HIGH, False, cfg).tier == "Novice"


def test_missing_cfg_keys_use_spec_defaults():
    assert mastery.compute(solves(IN, 5), BAND_LOW, BAND_HIGH, False, {}).tier == "Apprentice"


def test_mode_factor_helper():
    assert mastery.mode_factor("Upsolve") == 0.7
    assert mastery.mode_factor("With Editorial/Hint") == 0.5
    assert mastery.mode_factor("Practice") == 1.0
    assert mastery.mode_factor(None) == 1.0
