from datetime import date

from hq.logic import weekly


def row(d, problems=0, points=0.0, contests=0, max_rating=None, active=None):
    return {
        "date": d,
        "problems": problems,
        "points": points,
        "contests": contests,
        "max_rating": max_rating,
        "active": (problems > 0) if active is None else active,
    }


def test_summarize_keys_exact():
    result = weekly.summarize([], 0)
    assert set(result) == {
        "points", "problems", "active_days", "avg_rating",
        "max_rating", "contests", "points_delta",
    }


def test_summarize_empty_week():
    result = weekly.summarize([], 7.5)
    assert result == {
        "points": 0,
        "problems": 0,
        "active_days": 0,
        "avg_rating": 0,
        "max_rating": 0,
        "contests": 0,
        "points_delta": -7.5,
    }


def test_summarize_totals():
    rows = [
        row(date(2026, 1, 17), problems=2, points=3.5, contests=1, max_rating=1200),
        row(date(2026, 1, 18), problems=1, points=1.25, contests=0, max_rating=1400),
        row(date(2026, 1, 19), problems=0, points=0.0, contests=0, max_rating=None),
    ]
    result = weekly.summarize(rows, prev_points=2.0)
    assert result["points"] == 4.75
    assert result["problems"] == 3
    assert result["active_days"] == 2
    assert result["contests"] == 1
    assert result["max_rating"] == 1400
    assert result["points_delta"] == 2.75


def test_avg_rating_excludes_days_without_problems():
    rows = [
        row(date(2026, 1, 17), problems=2, max_rating=1200),
        row(date(2026, 1, 18), problems=0, max_rating=0),  # must not drag the average down
        row(date(2026, 1, 19), problems=1, max_rating=1400),
    ]
    assert weekly.summarize(rows, 0)["avg_rating"] == 1300


def test_avg_rating_rounds_half_up():
    rows = [
        row(date(2026, 1, 17), problems=1, max_rating=1200),
        row(date(2026, 1, 18), problems=1, max_rating=1201),
    ]
    assert weekly.summarize(rows, 0)["avg_rating"] == 1201


def test_avg_rating_zero_when_no_solves():
    rows = [row(date(2026, 1, 17), problems=0, max_rating=0)]
    assert weekly.summarize(rows, 0)["avg_rating"] == 0


def test_max_rating_zero_when_no_ratings():
    rows = [row(date(2026, 1, 17), problems=0, max_rating=None)]
    assert weekly.summarize(rows, 0)["max_rating"] == 0


def test_active_days_counts_truthy_active_flags():
    rows = [
        row(date(2026, 1, 17), problems=0, active=True),
        row(date(2026, 1, 18), problems=0, active=False),
        row(date(2026, 1, 19), problems=3, active=True),
    ]
    assert weekly.summarize(rows, 0)["active_days"] == 2


def test_points_delta_negative_and_rounded():
    rows = [row(date(2026, 1, 17), points=10.5)]
    assert weekly.summarize(rows, 20.0)["points_delta"] == -9.5


def test_points_are_rounded_to_two_decimals():
    rows = [row(date(2026, 1, 17), points=0.1), row(date(2026, 1, 18), points=0.2)]
    result = weekly.summarize(rows, 0.0)
    assert result["points"] == 0.3
    assert result["points_delta"] == 0.3
