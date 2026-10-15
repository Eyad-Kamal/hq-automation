import pytest

from hq.logic import goals

STATS = {
    "problems_total": 120,
    "points_total": 314.5,
    "cf_rating": 1435,
    "levels_complete": 1,
    "topics_competent": 4,
    "current_streak": 9,
    "contests_total": 17,
    "solved_ratings": [800, 1200, 1200, 1500, 1900],
}


@pytest.mark.parametrize(
    "metric, expected",
    [
        ("Problems Solved", 120.0),
        ("Points", 314.5),
        ("CF Rating", 1435.0),
        ("Level Reached", 1.0),
        ("Topics ≥ Competent", 4.0),
        ("Streak Days", 9.0),
        ("Contests Attended", 17.0),
    ],
)
def test_metric_mapping(metric, expected):
    value = goals.current_value(metric, STATS)
    assert value == expected
    assert isinstance(value, float)


def test_problems_at_rating_counts_threshold_and_above():
    assert goals.current_value("Problems ≥ Rating", STATS, threshold_rating=1200) == 4.0
    assert goals.current_value("Problems ≥ Rating", STATS, threshold_rating=1500) == 2.0
    assert goals.current_value("Problems ≥ Rating", STATS, threshold_rating=2000) == 0.0


def test_problems_at_rating_requires_threshold():
    with pytest.raises(ValueError):
        goals.current_value("Problems ≥ Rating", STATS)


def test_problems_at_rating_without_solved_ratings_is_zero():
    assert goals.current_value("Problems ≥ Rating", {}, threshold_rating=1000) == 0.0


def test_missing_numeric_key_is_zero():
    assert goals.current_value("CF Rating", {}) == 0.0
    assert goals.current_value("Points", {"points_total": None}) == 0.0


def test_unknown_metric_raises():
    with pytest.raises(ValueError):
        goals.current_value("Vibes", STATS)
