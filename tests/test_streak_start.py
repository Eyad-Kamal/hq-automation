"""Members "Streak Start": streaks (Current, Best, freezes, Daily Log streak) ignore days before it;
an empty Streak Start changes nothing. Daily Log "Active" flags and Last Active are not affected."""

from __future__ import annotations

from datetime import date, timedelta

from hq import notion as nt
from hq import schema as S
from hq.jobs import imports, onboard, recompute
from hq.logic import streak
from hq.repo import get_checkbox, get_number, get_text
from tests.fakes import NOW, build_world, seed_eyad_solves

DAY_OCT13 = date(2026, 10, 13)
DAY_OCT14 = date(2026, 10, 14)
TODAY = date(2026, 10, 15)
BASE = date(2026, 1, 1)


def _day(n: int) -> date:
    return BASE + timedelta(days=n)


def _span(a: int, b: int) -> set[date]:
    return {_day(n) for n in range(a, b + 1)}


# -- pure logic --------------------------------------------------------------------------------
def test_streak_ignores_days_before_the_start() -> None:
    active = _span(0, 9)
    unlimited = streak.compute(active, _day(9))
    windowed = streak.compute(streak.within_streak_start(active, _day(5)), _day(9))
    assert (unlimited.current, unlimited.best) == (10, 10)
    assert (windowed.current, windowed.best) == (5, 5), "days 0 to 4 are before the start and do not count"


def test_empty_start_changes_nothing() -> None:
    active = _span(0, 9) | {_day(12)}
    assert streak.within_streak_start(active, None) == active
    assert streak.compute(streak.within_streak_start(active, None), _day(12)) == streak.compute(active, _day(12))


def test_freezes_still_work_inside_the_window() -> None:
    # Days 0-2 are before the start (day 3). Days 3-9 (seven active days) earn a freeze, day 10 is
    # missed and uses it, day 11 is active again: the streak carries on through the freeze.
    active = _span(0, 9) | {_day(11)}
    windowed = streak.compute(streak.within_streak_start(active, _day(3)), _day(11))
    assert windowed.current == 8, "seven active days plus day 11; the frozen day does not count"
    assert windowed.frozen_days == {_day(10)}
    assert windowed.freezes == 0


def test_a_missed_day_inside_the_window_still_breaks_without_a_freeze() -> None:
    active = {_day(3), _day(4), _day(6)}
    windowed = streak.compute(streak.within_streak_start(active, _day(3)), _day(6))
    assert (windowed.current, windowed.best) == (1, 2)


# -- recompute job -------------------------------------------------------------------------------
def _setup():
    w = build_world()
    seed_eyad_solves(w)
    now = NOW
    onboard.run(w.context(now=now))
    imports.run(w.context(now=now))
    recompute.run(w.context(now=now))
    return w


def _member(w):
    return w.notion.page(w.eyad)


def test_recompute_counts_only_days_from_streak_start() -> None:
    w = _setup()
    assert get_number(_member(w), S.P_Members.CURRENT_STREAK) == 2, "baseline: Oct 13 and Oct 14"
    w.notion.page(w.eyad)["properties"][S.P_Members.STREAK_START] = nt.p_date(DAY_OCT14, with_time=False)
    recompute.run(w.context(now=NOW))
    member = _member(w)
    assert get_number(member, S.P_Members.CURRENT_STREAK) == 1
    assert get_number(member, S.P_Members.BEST_STREAK) == 1


def test_daily_log_streak_and_active_flags_follow_the_start() -> None:
    w = _setup()
    w.notion.page(w.eyad)["properties"][S.P_Members.STREAK_START] = nt.p_date(DAY_OCT14, with_time=False)
    recompute.run(w.context(now=NOW))
    rows = {get_text(r, S.P_DailyLog.SOURCE_KEY): r for r in w.notion.rows(S.DB_DAILY_LOG)}
    oct13 = rows[f"{w.eyad}:{DAY_OCT13.isoformat()}"]
    oct14 = rows[f"{w.eyad}:{DAY_OCT14.isoformat()}"]
    assert get_number(oct13, S.P_DailyLog.STREAK) == 0, "Oct 13 is before the start"
    assert get_checkbox(oct13, S.P_DailyLog.ACTIVE) is True, "the Active flag still shows the real activity"
    assert get_number(oct14, S.P_DailyLog.STREAK) == 1


def test_empty_streak_start_leaves_the_streak_as_before() -> None:
    w = _setup()
    before = (get_number(_member(w), S.P_Members.CURRENT_STREAK), get_number(_member(w), S.P_Members.BEST_STREAK))
    recompute.run(w.context(now=NOW))
    after = (get_number(_member(w), S.P_Members.CURRENT_STREAK), get_number(_member(w), S.P_Members.BEST_STREAK))
    assert before == after == (2, 2)


def test_streak_start_after_today_gives_no_streak() -> None:
    w = _setup()
    w.notion.page(w.eyad)["properties"][S.P_Members.STREAK_START] = nt.p_date(TODAY + timedelta(days=1), with_time=False)
    recompute.run(w.context(now=NOW))
    assert get_number(_member(w), S.P_Members.CURRENT_STREAK) == 0
    assert get_number(_member(w), S.P_Members.BEST_STREAK) == 0
