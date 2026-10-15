"""Jobs recompute: Daily Log rows, streak, member totals, mastery, achievements, teams, goals."""

from __future__ import annotations

from datetime import date, datetime

from hq import notion as nt
from hq import schema as S
from hq.jobs import imports, onboard, recompute
from hq.logic import scoring
from hq.repo import get_checkbox, get_date, get_number, get_relation, get_select, get_status, get_text
from tests.fakes import NOW, build_world, seed_eyad_solves

DAY_OCT13 = date(2026, 10, 13)
DAY_OCT14 = date(2026, 10, 14)
TODAY = date(2026, 10, 15)


def _chain(w, now: datetime = NOW):
    onboard.run(w.context(now=now))
    imports.run(w.context(now=now))
    return recompute.run(w.context(now=now))


def _setup():
    w = build_world()
    seed_eyad_solves(w)
    return w


def _daily(w, day: date):
    key = f"{w.eyad}:{day.isoformat()}"
    rows = [r for r in w.notion.rows(S.DB_DAILY_LOG) if get_text(r, S.P_DailyLog.SOURCE_KEY) == key]
    assert len(rows) == 1, f"expected one Daily Log row for {day}"
    return rows[0]


def _lq(w, topic: str):
    rows = [
        r
        for r in w.notion.rows(S.DB_LEARNING_QUEUE)
        if get_relation(r, S.P_LearningQueue.MEMBER) == [w.eyad] and get_relation(r, S.P_LearningQueue.TOPIC) == [w.topics[topic]]
    ]
    assert len(rows) == 1
    return rows[0]


def _expected_points() -> float:
    # Rating @ Solve is 1150 for all three (the rating before the 1150 update).
    return round(
        scoring.points(1200, 1150, "Live Contest")
        + scoring.points(800, 1150, "Upsolve")
        + scoring.points(1300, 1150, "Practice"),
        2,
    )


def test_daily_log_rows_for_the_window_plus_today() -> None:
    w = _setup()
    _chain(w)
    eyad_rows = [r for r in w.notion.rows(S.DB_DAILY_LOG) if get_relation(r, S.P_DailyLog.MEMBER) == [w.eyad]]
    assert len(eyad_rows) == 61, "DAILY_LOG_DAYS default 60 days before today, plus today"

    oct13 = _daily(w, DAY_OCT13)
    assert get_number(oct13, S.P_DailyLog.PROBLEMS) == 1
    assert get_number(oct13, S.P_DailyLog.CONTESTS) == 1, "the live contest counts as an attended contest"
    assert get_checkbox(oct13, S.P_DailyLog.ACTIVE) is True
    assert get_select(oct13, S.P_DailyLog.TEAM) == "Team Alpha"

    oct14 = _daily(w, DAY_OCT14)
    assert get_number(oct14, S.P_DailyLog.PROBLEMS) == 2
    assert get_number(oct14, S.P_DailyLog.STREAK) == 2

    today = _daily(w, TODAY)
    assert get_checkbox(today, S.P_DailyLog.ACTIVE) is False
    assert get_number(today, S.P_DailyLog.TARGET) == 3, "DEFAULT_DAILY_TARGET"
    assert get_number(today, S.P_DailyLog.STREAK) == 2, "today not active yet does not break the streak"


def test_streak_points_and_totals_on_the_member() -> None:
    w = _setup()
    _chain(w)
    member = w.notion.page(w.eyad)
    assert get_number(member, S.P_Members.CURRENT_STREAK) == 2
    assert get_number(member, S.P_Members.BEST_STREAK) == 2
    assert get_number(member, S.P_Members.FREEZES) == 0
    assert get_number(member, S.P_Members.PROBLEMS_TOTAL) == 3
    assert abs(get_number(member, S.P_Members.TOTAL_POINTS) - _expected_points()) < 1e-9
    assert abs(get_number(member, S.P_Members.POINTS_THIS_WEEK) - _expected_points()) < 1e-9
    assert get_number(member, S.P_Members.POINTS_LAST_WEEK) == 0
    assert get_number(member, S.P_Members.POINTS_TODAY) == 0
    assert get_date(member, S.P_Members.LAST_ACTIVE).isoformat() == DAY_OCT14.isoformat()


def test_daily_log_points_match_the_scoring_module() -> None:
    w = _setup()
    _chain(w)
    assert get_number(_daily(w, DAY_OCT13), S.P_DailyLog.POINTS) == round(scoring.points(1200, 1150, "Live Contest"), 2)
    expected_oct14 = round(scoring.points(800, 1150, "Upsolve") + scoring.points(1300, 1150, "Practice"), 2)
    assert abs(get_number(_daily(w, DAY_OCT14), S.P_DailyLog.POINTS) - expected_oct14) < 1e-9


def test_learning_queue_counts_tiers_and_status() -> None:
    w = _setup()
    _chain(w)
    greedy = _lq(w, "greedy")
    assert get_number(greedy, S.P_LearningQueue.PROBLEMS_COUNT) == 1
    assert get_number(greedy, S.P_LearningQueue.ABOVE_BAND) == 1, "1200 is above the 800-1199 band"
    assert get_number(greedy, S.P_LearningQueue.MASTERY_POINTS) == 1.5
    assert get_select(greedy, S.P_LearningQueue.AUTO_TIER) == "Novice"
    assert get_status(greedy, S.P_LearningQueue.QUEUE_STATUS) == "Practicing"

    dp = _lq(w, "dp")
    assert get_number(dp, S.P_LearningQueue.IN_BAND) == 1
    assert get_select(dp, S.P_LearningQueue.AUTO_TIER) == "Novice"

    strings = _lq(w, "strings")
    assert get_number(strings, S.P_LearningQueue.MASTERY_POINTS) == 0.7, "upsolve counts 0.7"
    assert get_select(strings, S.P_LearningQueue.AUTO_TIER) == "Unseen"


def test_achievements_are_awarded_once() -> None:
    w = _setup()
    _chain(w)
    names = {get_text(r, S.P_Achievements.SOURCE_KEY) for r in w.notion.rows(S.DB_ACHIEVEMENTS)}
    assert f"First Blood:{w.eyad}" in names
    assert f"Stretch 1200:{w.eyad}" in names
    assert f"Stretch 1600:{w.eyad}" not in names

    count = len(w.notion.rows(S.DB_ACHIEVEMENTS))
    _chain(w)
    assert len(w.notion.rows(S.DB_ACHIEVEMENTS)) == count


def test_team_totals_are_the_sum_of_members() -> None:
    w = _setup()
    _chain(w)
    team = w.notion.page(w.team)
    assert abs(get_number(team, S.P_Teams.POINTS_THIS_WEEK) - _expected_points()) < 1e-9
    assert abs(get_number(team, S.P_Teams.TOTAL_POINTS) - _expected_points()) < 1e-9


def test_rerun_writes_nothing() -> None:
    w = _setup()
    _chain(w)
    w.notion.reset_writes()
    result = recompute.run(w.context())
    assert result.result == "OK"
    assert result.items == 0, "a rerun with unchanged data writes nothing"
    assert w.notion.creates == [] and w.notion.updates == []


def test_goals_track_their_metric_and_achieve() -> None:
    w = _setup()
    w.notion.seed(
        S.DB_GOALS,
        {
            S.P_Goals.TITLE: nt.p_title("Solve 3 problems"),
            S.P_Goals.SCOPE: nt.p_select("Person"),
            S.P_Goals.METRIC: nt.p_select("Problems Solved"),
            S.P_Goals.MEMBER: nt.p_relation([w.eyad]),
            S.P_Goals.TARGET: nt.p_number(3),
            S.P_Goals.STATUS: nt.p_status("Active"),
        },
    )
    w.notion.seed(
        S.DB_GOALS,
        {
            S.P_Goals.TITLE: nt.p_title("Team 10000 points"),
            S.P_Goals.SCOPE: nt.p_select("Team"),
            S.P_Goals.METRIC: nt.p_select("Points"),
            S.P_Goals.TEAM: nt.p_relation([w.team]),
            S.P_Goals.TARGET: nt.p_number(10000),
            S.P_Goals.STATUS: nt.p_status("Active"),
        },
    )
    _chain(w)
    person, team = w.notion.rows(S.DB_GOALS)
    assert get_number(person, S.P_Goals.CURRENT) == 3
    assert get_status(person, S.P_Goals.STATUS) == "Achieved"
    assert abs(get_number(team, S.P_Goals.CURRENT) - _expected_points()) < 1e-9
    assert get_status(team, S.P_Goals.STATUS) == "Active"


def test_practice_task_done_count_and_done_status() -> None:
    w = _setup()
    links = "CF-1985C | https://codeforces.com/problemset/problem/1985/C\nCF-1999B | https://x\nCF-5555A | https://y"
    w.notion.seed(
        S.DB_TASKS,
        {
            S.P_Tasks.TITLE: nt.p_title("🎯 Greedy: 3 problems"),
            S.P_Tasks.ASSIGNEE: nt.p_relation([w.eyad]),
            S.P_Tasks.TYPE: nt.p_select("Topic Practice"),
            S.P_Tasks.ORIGIN: nt.p_select("Auto"),
            S.P_Tasks.STATUS: nt.p_status("To Do"),
            S.P_Tasks.TARGET: nt.p_number(3),
            S.P_Tasks.LINKS: nt.p_text(links),
        },
    )
    w.notion.seed(
        S.DB_TASKS,
        {
            S.P_Tasks.TITLE: nt.p_title("🎯 Greedy: 2 problems"),
            S.P_Tasks.ASSIGNEE: nt.p_relation([w.eyad]),
            S.P_Tasks.TYPE: nt.p_select("Topic Practice"),
            S.P_Tasks.ORIGIN: nt.p_select("Auto"),
            S.P_Tasks.STATUS: nt.p_status("To Do"),
            S.P_Tasks.TARGET: nt.p_number(2),
            S.P_Tasks.LINKS: nt.p_text("CF-1985C | https://a\nCF-1999B | https://b"),
        },
    )
    _chain(w)
    three, two = w.notion.rows(S.DB_TASKS)
    assert get_number(three, S.P_Tasks.DONE_COUNT) == 2
    assert get_status(three, S.P_Tasks.STATUS) == "To Do"
    assert get_number(two, S.P_Tasks.DONE_COUNT) == 2
    assert get_status(two, S.P_Tasks.STATUS) == "Done"


def test_member_filter_limits_writes_to_that_member() -> None:
    w = _setup()
    onboard.run(w.context())
    imports.run(w.context())
    w.notion.reset_writes()
    recompute.run(w.context(member="Sara"))
    touched = {pid for pid, _ in w.notion.updates}
    assert w.eyad not in touched, "Eyad is not in the --member filter"
    assert all(ds != w.notion.ids[S.DB_TEAMS] for ds, _ in w.notion.creates)
