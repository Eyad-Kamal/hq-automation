"""Jobs daily and weekly: one practice task per member per day, skipping, upsolve reminders,
weekly summaries, MVP and the weekly report (created once)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from hq import config
from hq import notion as nt
from hq import schema as S
from hq.jobs import cairo_end_of_day, daily, imports, onboard, recompute, weekly
from hq.repo import get_checkbox, get_date, get_number, get_relation, get_select, get_status, get_text
from hq.timeutil import CAIRO_TZ
from tests.fakes import NOW, NOW_SATURDAY, build_world, seed_eyad_solves

TODAY = date(2026, 10, 15)
PROBLEMSET = [
    {"key": "CF-1000A", "name": "Solved already", "link": "https://cf/1000/A", "rating": 900, "tags": ["greedy"]},
    {"key": "CF-1000B", "name": "Bee", "link": "https://cf/1000/B", "rating": 1000, "tags": ["greedy"]},
    {"key": "CF-1000C", "name": "Sea", "link": "https://cf/1000/C", "rating": 1100, "tags": ["greedy"]},
    {"key": "CF-1000D", "name": "Dee", "link": "https://cf/1000/D", "rating": 1150, "tags": ["greedy"]},
    {"key": "CF-1000E", "name": "No rating", "link": "https://cf/1000/E", "rating": None, "tags": ["greedy"]},
    {"key": "CF-2000A", "name": "Dynamic", "link": "https://cf/2000/A", "rating": 1500, "tags": ["dp"]},
]


def _setup():
    w = build_world()
    w.sources.problemset = list(PROBLEMSET)
    onboard.run(w.context())
    w.notion.reset_writes()
    return w


def _practice_tasks(w, member_id: str):
    return [
        t
        for t in w.notion.rows(S.DB_TASKS)
        if get_select(t, S.P_Tasks.TYPE) == "Topic Practice" and get_relation(t, S.P_Tasks.ASSIGNEE) == [member_id]
    ]


def test_daily_creates_one_practice_task_per_member_and_is_idempotent() -> None:
    w = _setup()
    seeded = w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("Solved already"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
            S.P_Problems.PROBLEM_KEY: nt.p_text("CF-1000A"),
            S.P_Problems.STATUS: nt.p_status("Solved"),
            S.P_Problems.SOURCE_KEY: nt.p_text(f"CF-1000A:{w.eyad}"),
        },
    )
    assert seeded
    w.notion.reset_writes()

    result = daily.run(w.context())
    assert result.result == "OK"
    assert len(_practice_tasks(w, w.eyad)) == 1
    assert len(_practice_tasks(w, w.sara)) == 1

    task = _practice_tasks(w, w.eyad)[0]
    assert get_text(task, S.P_Tasks.SOURCE_KEY) == f"auto:2026-10-15:{w.eyad}"
    assert get_select(task, S.P_Tasks.ORIGIN) == "Auto"
    assert get_status(task, S.P_Tasks.STATUS) == "To Do"
    assert get_number(task, S.P_Tasks.TARGET) == 3
    assert get_relation(task, S.P_Tasks.TOPIC) == [w.topics["greedy"]]
    assert get_date(task, S.P_Tasks.DUE) == cairo_end_of_day(TODAY)
    links = get_text(task, S.P_Tasks.LINKS).splitlines()
    assert links == [
        "CF-1000B | https://cf/1000/B",
        "CF-1000C | https://cf/1000/C",
        "CF-1000D | https://cf/1000/D",
    ], "solved and unrated problems are excluded; picks are sorted by rating"
    assert task["properties"][S.P_Tasks.TITLE]["title"][0]["text"]["content"] == "🎯 Greedy & Constructive: 3 problems"
    assert w.notion.blocks[task["id"]], "the picks are also listed as bullets in the page body"

    tasks_before = len(w.notion.rows(S.DB_TASKS))
    w.notion.reset_writes()
    daily.run(w.context())
    assert len(w.notion.rows(S.DB_TASKS)) == tasks_before, "a second run on the same day adds nothing"
    assert not [ds for ds, _ in w.notion.creates if ds == w.notion.ids[S.DB_TASKS]]


def test_daily_creates_todays_daily_log_row_even_without_recompute() -> None:
    w = _setup()
    daily.run(w.context())
    keys = {get_text(r, S.P_DailyLog.SOURCE_KEY) for r in w.notion.rows(S.DB_DAILY_LOG)}
    assert f"{w.eyad}:2026-10-15" in keys


def test_yesterdays_open_practice_task_is_skipped() -> None:
    w = _setup()
    yesterday_due = cairo_end_of_day(TODAY - timedelta(days=1))
    w.notion.seed(
        S.DB_TASKS,
        {
            S.P_Tasks.TITLE: nt.p_title("🎯 old"),
            S.P_Tasks.ASSIGNEE: nt.p_relation([w.eyad]),
            S.P_Tasks.TYPE: nt.p_select("Topic Practice"),
            S.P_Tasks.ORIGIN: nt.p_select("Auto"),
            S.P_Tasks.STATUS: nt.p_status("To Do"),
            S.P_Tasks.DUE: nt.p_date(yesterday_due),
            S.P_Tasks.SOURCE_KEY: nt.p_text("auto:2026-10-14:old"),
        },
    )
    daily.run(w.context())
    old = [t for t in w.notion.rows(S.DB_TASKS) if get_text(t, S.P_Tasks.SOURCE_KEY) == "auto:2026-10-14:old"][0]
    assert get_status(old, S.P_Tasks.STATUS) == "Skipped"
    today_task = [t for t in _practice_tasks(w, w.eyad) if get_text(t, S.P_Tasks.SOURCE_KEY) == f"auto:2026-10-15:{w.eyad}"][0]
    assert get_status(today_task, S.P_Tasks.STATUS) == "To Do", "today's task is not touched"


def test_daily_due_once_per_cairo_day() -> None:
    w = _setup()
    assert daily.daily_due(w.context())
    daily.run(w.context())
    assert not daily.daily_due(w.context())
    assert daily.daily_due(w.context(now=NOW + timedelta(days=1)))


def test_practice_topic_follows_the_learning_queue_priority() -> None:
    w = _setup()
    for row in w.notion.rows(S.DB_LEARNING_QUEUE):
        topic = get_relation(row, S.P_LearningQueue.TOPIC)
        if topic == [w.topics["dp"]] and get_relation(row, S.P_LearningQueue.MEMBER) == [w.eyad]:
            w.notion.update_page(
                row["id"],
                {S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning"), S.P_LearningQueue.PRIORITY: nt.p_select("Very Important")},
            )
    daily.run(w.context())
    eyad_task = _practice_tasks(w, w.eyad)[0]
    assert get_relation(eyad_task, S.P_Tasks.TOPIC) == [w.topics["dp"]]
    assert "DP Basics" in eyad_task["properties"][S.P_Tasks.TITLE]["title"][0]["text"]["content"]


def test_upsolve_reminder_once_for_an_ended_contest() -> None:
    w = _setup()
    contest = w.notion.seed(
        S.DB_CONTESTS,
        {
            S.P_Contests.TITLE: nt.p_title("Codeforces Round 1985 (Div. 2)"),
            S.P_Contests.START: nt.p_date(NOW - timedelta(days=3)),
            S.P_Contests.DURATION_MIN: nt.p_number(120),
            S.P_Contests.PHASE: nt.p_select("Finished"),
            S.P_Contests.EXTERNAL_ID: nt.p_text("CF-1985"),
        },
    )
    entry = w.notion.seed(
        S.DB_CONTEST_ENTRIES,
        {
            S.P_ContestEntries.TITLE: nt.p_title("Codeforces Round 1985 (Div. 2) · Eyad"),
            S.P_ContestEntries.MEMBER: nt.p_relation([w.eyad]),
            S.P_ContestEntries.CONTEST: nt.p_relation([contest]),
            S.P_ContestEntries.STATUS: nt.p_status("Attended"),
            S.P_ContestEntries.SOLVED: nt.p_number(1),
            S.P_ContestEntries.TOTAL_PROBLEMS: nt.p_number(5),
            S.P_ContestEntries.UPSOLVED: nt.p_number(1),
        },
    )
    daily.run(w.context())
    reminders = [t for t in w.notion.rows(S.DB_TASKS) if get_select(t, S.P_Tasks.TYPE) == "Upsolve"]
    assert len(reminders) == 1
    assert get_text(reminders[0], S.P_Tasks.SOURCE_KEY) == f"upsolve:{entry}:{w.eyad}"
    assert get_relation(reminders[0], S.P_Tasks.CONTEST_ENTRY) == [entry]

    daily.run(w.context())
    assert len([t for t in w.notion.rows(S.DB_TASKS) if get_select(t, S.P_Tasks.TYPE) == "Upsolve"]) == 1


def test_upsolve_not_reminded_when_fully_upsolved_or_not_yet_ended() -> None:
    w = _setup()
    contest = w.notion.seed(
        S.DB_CONTESTS,
        {
            S.P_Contests.TITLE: nt.p_title("Fresh contest"),
            S.P_Contests.START: nt.p_date(NOW - timedelta(hours=5)),
            S.P_Contests.DURATION_MIN: nt.p_number(120),
            S.P_Contests.EXTERNAL_ID: nt.p_text("CF-2222"),
        },
    )
    w.notion.seed(
        S.DB_CONTEST_ENTRIES,
        {
            S.P_ContestEntries.TITLE: nt.p_title("Fresh · Eyad"),
            S.P_ContestEntries.MEMBER: nt.p_relation([w.eyad]),
            S.P_ContestEntries.CONTEST: nt.p_relation([contest]),
            S.P_ContestEntries.STATUS: nt.p_status("Attended"),
        },
    )
    daily.run(w.context())
    assert [t for t in w.notion.rows(S.DB_TASKS) if get_select(t, S.P_Tasks.TYPE) == "Upsolve"] == []


# ---------------------------------------------------------------------------
# Weekly
# ---------------------------------------------------------------------------
def _weekly_setup(*, admin: str | None = None):
    w = build_world()
    seed_eyad_solves(w)
    if admin is not None:
        w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("ADMIN_USER_ID"), S.P_Config.VALUE: nt.p_text(admin)})
    onboard.run(w.context(now=NOW_SATURDAY))
    imports.run(w.context(now=NOW_SATURDAY))
    recompute.run(w.context(now=NOW_SATURDAY))
    w.notion.reset_writes()
    return w


def _summaries(w, scope: str):
    return [r for r in w.notion.rows(S.DB_WEEKLY_SUMMARY) if get_select(r, S.P_WeeklySummary.SCOPE) == scope]


def test_weekly_due_only_after_saturday_0010_cairo() -> None:
    w = _weekly_setup()
    config.set_state(w.context().repo, config.STATE_LAST_WEEKLY, "2026-10-10")  # last week already done
    assert not weekly.weekly_due(w.context(now=NOW))  # Thursday, same week
    assert weekly.weekly_due(w.context(now=NOW_SATURDAY))
    saturday_midnight = datetime.combine(date(2026, 10, 17), time(0, 0), tzinfo=CAIRO_TZ).astimezone(timezone.utc)
    assert not weekly.weekly_due(w.context(now=saturday_midnight)), "before 00:10 Saturday"
    assert weekly.weekly_due(w.context(now=saturday_midnight + timedelta(minutes=15))), "after 00:10 Saturday"


def test_weekly_summaries_mvp_and_report() -> None:
    w = _weekly_setup(admin="user-admin")
    # Only this week (the test looks at one row per member); history is covered in test_round2.
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("WEEKLY_BACKFILL_WEEKS"), S.P_Config.NUMBER: nt.p_number(1)})
    result = weekly.run(w.context(now=NOW_SATURDAY))
    assert result.result == "OK"

    persons = {get_relation(r, S.P_WeeklySummary.MEMBER)[0]: r for r in _summaries(w, "Person")}
    assert set(persons) == {w.eyad, w.sara}, "inactive members get no summary"
    eyad = persons[w.eyad]
    assert get_number(eyad, S.P_WeeklySummary.PROBLEMS) == 3
    assert get_number(eyad, S.P_WeeklySummary.ACTIVE_DAYS) == 2
    assert get_number(eyad, S.P_WeeklySummary.CONTESTS) == 1
    assert get_number(eyad, S.P_WeeklySummary.POINTS_DELTA) == get_number(eyad, S.P_WeeklySummary.POINTS)
    assert get_checkbox(eyad, S.P_WeeklySummary.MVP) is True
    assert get_checkbox(persons[w.sara], S.P_WeeklySummary.MVP) is False
    assert get_text(eyad, S.P_WeeklySummary.SOURCE_KEY) == f"person:{w.eyad}:2026-10-10"

    assert len(_summaries(w, "Team")) == 1
    assert len(_summaries(w, "Club")) == 1
    club = _summaries(w, "Club")[0]
    assert get_number(club, S.P_WeeklySummary.PROBLEMS) == 3

    reports = w.notion.rows(S.DB_WEEKLY_REPORTS)
    assert len(reports) == 1
    report = reports[0]
    assert get_date(report, S.P_WeeklyReports.WEEK_START) == date(2026, 10, 10)
    assert get_text(report, S.P_WeeklyReports.MVP) == "Eyad"
    assert get_relation(eyad, S.P_WeeklySummary.REPORT) == [report["id"]]
    assert w.notion.blocks[report["id"]], "the report body is written"
    assert any(page == report["id"] for page, _ in w.notion.comments)
    mentions = [i["mention"]["user"]["id"] for _, rich in w.notion.comments for i in rich if i["type"] == "mention"]
    assert mentions == ["user-admin"]

    assert f"Weekly MVP:{w.eyad}:2026-10-10" in {get_text(r, S.P_Achievements.SOURCE_KEY) for r in w.notion.rows(S.DB_ACHIEVEMENTS)}
    assert config.state_date(w.context(now=NOW_SATURDAY).repo, config.STATE_LAST_WEEKLY) == date(2026, 10, 17)


def test_weekly_rerun_creates_nothing_new() -> None:
    w = _weekly_setup(admin="user-admin")
    weekly.run(w.context(now=NOW_SATURDAY))
    summary_count = len(w.notion.rows(S.DB_WEEKLY_SUMMARY))
    report_count = len(w.notion.rows(S.DB_WEEKLY_REPORTS))
    achievements = len(w.notion.rows(S.DB_ACHIEVEMENTS))

    w.notion.reset_writes()
    weekly.run(w.context(now=NOW_SATURDAY))
    assert len(w.notion.rows(S.DB_WEEKLY_SUMMARY)) == summary_count
    assert len(w.notion.rows(S.DB_WEEKLY_REPORTS)) == report_count
    assert w.notion.comments == [], "the admin is not mentioned a second time"
    assert len(w.notion.rows(S.DB_ACHIEVEMENTS)) == achievements
    assert w.notion.creates == []
    assert not weekly.weekly_due(w.context(now=NOW_SATURDAY + timedelta(hours=1)))


def test_daily_reports_a_warning_when_no_problems_can_be_picked() -> None:
    w = _setup()
    w.sources.problemset = []  # e.g. the Codeforces problemset could not be fetched
    result = daily.run(w.context())
    assert result.result == "Warning"
    assert "no unsolved problems" in result.details
    assert _practice_tasks(w, w.eyad) == []
