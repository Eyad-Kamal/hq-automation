from datetime import date, timedelta

from hq.logic import streak

BASE = date(2026, 1, 1)


def day(n: int) -> date:
    return BASE + timedelta(days=n)


def days(*ns) -> set[date]:
    return {day(n) for n in ns}


def span(a: int, b: int) -> set[date]:
    """Inclusive range of day offsets."""
    return {day(n) for n in range(a, b + 1)}


def test_empty_history_is_all_zero():
    result = streak.compute(set(), day(5))
    assert result == streak.StreakResult(current=0, best=0, freezes=0, frozen_days=set())


def test_single_active_today():
    result = streak.compute(days(3), day(3))
    assert (result.current, result.best, result.freezes) == (1, 1, 0)
    assert result.frozen_days == set()


def test_consecutive_days_increase_streak():
    result = streak.compute(span(0, 4), day(4))
    assert (result.current, result.best) == (5, 5)


def test_today_inactive_does_not_break_streak():
    # Active Jan 1..Jan 6, today Jan 7 not yet active: streak still 6.
    result = streak.compute(span(0, 5), day(6))
    assert (result.current, result.best, result.freezes) == (6, 6, 0)
    assert result.frozen_days == set()


def test_today_inactive_with_only_earlier_misses_before_today():
    # Active Jan 1..Jan 4 and today Jan 5 (inactive): no break on today.
    result = streak.compute(span(0, 3), day(4))
    assert result.current == 4


def test_freeze_earned_at_seven_days():
    result = streak.compute(span(0, 6), day(7))
    assert (result.current, result.freezes) == (7, 1)


def test_freeze_earned_then_consumed_by_one_missed_day():
    # Days 0..6 active (freeze earned), day 7 missed (freeze consumed),
    # day 8 active. Current counts only active days: 7 + 1 = 8.
    active = span(0, 6) | days(8)
    result = streak.compute(active, day(8))
    assert result.current == 8
    assert result.best == 8
    assert result.freezes == 0
    assert result.frozen_days == {day(7)}


def test_frozen_day_does_not_increase_streak():
    active = span(0, 6) | days(8)
    result = streak.compute(active, day(8))
    # 7 active days before the miss plus 1 after; the frozen day adds nothing.
    assert result.current == 8


def test_two_misses_in_a_row_with_one_freeze_breaks_streak():
    # Freeze covers day 7; day 8 is a second miss with no freeze left -> break.
    active = span(0, 6) | days(9)
    result = streak.compute(active, day(9))
    assert result.current == 1  # only day 9 counts after the reset
    assert result.best == 7
    assert result.freezes == 0
    assert result.frozen_days == {day(7)}


def test_two_misses_in_a_row_with_two_freezes_both_consumed():
    # 14 active days earn two freezes (7 and 14 milestones; cap 2).
    active = span(0, 13) | days(16)
    result = streak.compute(active, day(16))
    assert result.freezes == 0
    assert result.frozen_days == {day(14), day(15)}
    assert result.current == 15  # 14 + day 16
    assert result.best == 15


def test_single_miss_without_freeze_resets_to_zero_then_restarts():
    # Days 0..3 active, day 4 missed with no freeze -> reset, day 5 active.
    active = span(0, 3) | days(5)
    result = streak.compute(active, day(5))
    assert result.current == 1
    assert result.best == 4
    assert result.freezes == 0
    assert result.frozen_days == set()


def test_multiple_runs_best_is_max_across_runs():
    # Run of 3, break, run of 2 -> current 2, best 3.
    active = span(0, 2) | span(5, 6)
    result = streak.compute(active, day(6))
    assert (result.current, result.best) == (2, 3)


def test_freeze_cap_is_respected():
    # 21 consecutive days would earn 3 freezes at milestones 7, 14, 21; cap is 2.
    result = streak.compute(span(0, 20), day(20))
    assert result.current == 21
    assert result.freezes == 2


def test_freeze_cap_custom_max_one():
    result = streak.compute(span(0, 13), day(13), freeze_max=1)
    assert result.freezes == 1


def test_custom_freeze_every():
    # freeze_every=3: freeze earned at 3 and 6, capped at 2.
    result = streak.compute(span(0, 5), day(5), freeze_every=3)
    assert result.freezes == 2


def test_days_after_today_are_ignored():
    result = streak.compute(span(0, 3) | days(10), day(3))
    assert (result.current, result.best) == (4, 4)


def test_missed_today_alone_does_not_count_as_miss():
    # Only history up to yesterday; today has no activity yet.
    result = streak.compute(span(0, 4), day(5))
    assert result.current == 5
    assert result.frozen_days == set()


def test_streak_after_freeze_earned_again_at_fourteen():
    # Freeze at 7 consumed by a miss on day 7, then 7 more active days.
    # Current reaches 14 on day 15 (days 0..6 + days 8..14), earning a freeze.
    active = span(0, 6) | span(8, 14)
    result = streak.compute(active, day(14))
    assert result.current == 14
    assert result.freezes == 1
    assert result.frozen_days == {day(7)}
