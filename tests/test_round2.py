"""Round 2: Learning-only topic picking, upsolve window, real contest names, Total Problems from the problemset,
history depth and the weekly backfill, full-history import, Who/Team fill, manual-row
adoption, normalized dedupe, and personal dashboards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from hq import config
from hq import notion as nt
from hq import schema as S
from hq.jobs import daily, dashboards, imports, onboard, recompute, weekly
from hq.jobs import OK, WARNING
from hq.models import Contest
from hq.repo import get_checkbox, get_date, get_number, get_relation, get_select, get_status, get_text, get_title, get_url
from hq.sources import codeforces
from tests.fakes import NOW, build_world, cf_solve, seed_eyad_solves

HOUR = timedelta(hours=1)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _config(w, key: str, value: float) -> None:
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title(key), S.P_Config.NUMBER: nt.p_number(value)})


def _lq(w, topic: str, member: str):
    rows = [
        r
        for r in w.notion.rows(S.DB_LEARNING_QUEUE)
        if get_relation(r, S.P_LearningQueue.MEMBER) == [member] and get_relation(r, S.P_LearningQueue.TOPIC) == [w.topics[topic]]
    ]
    assert len(rows) == 1
    return rows[0]


def _set_lq(w, topic: str, member: str, **props) -> None:
    w.notion.update_page(_lq(w, topic, member)["id"], props)


def _practice_tasks(w, member: str):
    return [
        t
        for t in w.notion.rows(S.DB_TASKS)
        if get_select(t, S.P_Tasks.TYPE) == "Topic Practice" and member in get_relation(t, S.P_Tasks.ASSIGNEE)
    ]


def _problemset(w) -> None:
    w.sources.problemset = [
        {"key": "CF-1000B", "name": "Bee", "link": "https://x/1000B", "rating": 1000, "tags": ["greedy"]},
        {"key": "CF-2000A", "name": "Str", "link": "https://x/2000A", "rating": 1000, "tags": ["strings"]},
        {"key": "CF-2001A", "name": "Str wide", "link": "https://x/2001A", "rating": 1250, "tags": ["strings"]},
    ]


def _entry(w, contest_id: str, member: str, *, status: str = "Attended", total=None, who: str = "Eyad"):
    props = {
        S.P_ContestEntries.TITLE: nt.p_title(f"entry {contest_id}"),
        S.P_ContestEntries.MEMBER: nt.p_relation([member]),
        S.P_ContestEntries.CONTEST: nt.p_relation([contest_id]),
        S.P_ContestEntries.WHO: nt.p_select(who),
        S.P_ContestEntries.STATUS: nt.p_status(status),
        S.P_ContestEntries.DATE: nt.p_date(NOW - timedelta(days=1), with_time=False),
        S.P_ContestEntries.PARTICIPATION: nt.p_select("Rated Live"),
    }
    if total is not None:
        props[S.P_ContestEntries.TOTAL_PROBLEMS] = nt.p_number(total)
    return w.notion.seed(S.DB_CONTEST_ENTRIES, props)


def _contest(w, name: str, start: datetime, duration: int, ext: str) -> str:
    return w.notion.seed(
        S.DB_CONTESTS,
        {
            S.P_Contests.TITLE: nt.p_title(name),
            S.P_Contests.PLATFORM: nt.p_select("Codeforces"),
            S.P_Contests.KIND: nt.p_select("CF Div. 2"),
            S.P_Contests.START: nt.p_date(start),
            S.P_Contests.DURATION_MIN: nt.p_number(duration),
            S.P_Contests.PHASE: nt.p_select("Finished"),
            S.P_Contests.EXTERNAL_ID: nt.p_text(ext),
            S.P_Contests.TASKS_CREATED: nt.p_checkbox(True),
            S.P_Contests.ALERT_SENT: nt.p_checkbox(True),
        },
    )


# ---------------------------------------------------------------------------
# #1 Learning-only topic picking
# ---------------------------------------------------------------------------
def test_1_learning_rows_are_the_only_candidates_when_any_exist() -> None:
    w = build_world()
    _config(w, "AUTO_PRACTICE_TASKS", 1)
    _problemset(w)
    onboard.run(w.context())
    _set_lq(w, "strings", w.eyad, **{S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning")})
    _set_lq(w, "greedy", w.eyad, **{S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Practicing")})
    daily.run(w.context())
    tasks = _practice_tasks(w, w.eyad)
    assert len(tasks) == 1
    assert get_relation(tasks[0], S.P_Tasks.TOPIC) == [w.topics["strings"]], "the Practicing and Queued topics are not used"


def test_1_queued_fallback_is_not_used_when_learning_topics_have_no_problems() -> None:
    w = build_world()
    _config(w, "AUTO_PRACTICE_TASKS", 1)
    _problemset(w)
    onboard.run(w.context())  # greedy is Queued and has problems; strings is Learning and has none in its band
    _set_lq(w, "strings", w.eyad, **{S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning")})
    w.sources.problemset = [p for p in w.sources.problemset if "strings" not in p["tags"]]
    result = daily.run(w.context())
    assert _practice_tasks(w, w.eyad) == []
    assert result.result == WARNING
    assert "no unsolved problems in the band of any queued topic" in result.details


def test_1_learning_topic_widens_its_band_like_autotasks() -> None:
    w = build_world()
    _config(w, "AUTO_PRACTICE_TASKS", 1)
    _problemset(w)
    onboard.run(w.context())
    _set_lq(w, "strings", w.eyad, **{S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning")})
    w.sources.problemset = [p for p in w.sources.problemset if p["key"] == "CF-2001A"]  # rating 1250, band is 800-1199
    daily.run(w.context())
    tasks = _practice_tasks(w, w.eyad)
    assert len(tasks) == 1 and get_relation(tasks[0], S.P_Tasks.TOPIC) == [w.topics["strings"]]


def test_1_learning_row_at_competent_is_skipped() -> None:
    w = build_world()
    _problemset(w)
    onboard.run(w.context())
    _set_lq(
        w,
        "strings",
        w.eyad,
        **{
            S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning"),
            S.P_LearningQueue.COACH_OVERRIDE: nt.p_select("Competent"),
        },
    )
    daily.run(w.context())
    assert _practice_tasks(w, w.eyad) == []


def test_1_recompute_never_changes_a_learning_status() -> None:
    w = build_world()
    _problemset(w)
    onboard.run(w.context())
    _set_lq(w, "strings", w.eyad, **{S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning")})
    w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("Str"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
            S.P_Problems.PROBLEM_KEY: nt.p_text("CF-2000A"),
            S.P_Problems.STATUS: nt.p_status("Solved"),
            S.P_Problems.RATING: nt.p_number(1000),
            S.P_Problems.TOPICS: nt.p_relation([w.topics["strings"]]),
            S.P_Problems.SOLVED_ON: nt.p_date(NOW - timedelta(days=1)),
            S.P_Problems.MODE: nt.p_select("Practice"),
        },
    )
    recompute.run(w.context())
    assert get_status(_lq(w, "strings", w.eyad), S.P_LearningQueue.QUEUE_STATUS) == "Learning"


# ---------------------------------------------------------------------------
# #2 Upsolve window and cleanup
# ---------------------------------------------------------------------------
def test_2_upsolve_reminders_only_inside_the_window_and_stale_ones_are_skipped() -> None:
    w = build_world()
    onboard.run(w.context())
    recent = _contest(w, "Round recent", NOW - timedelta(days=3), 120, "CF-3001")
    old = _contest(w, "Round old", NOW - timedelta(days=20), 120, "CF-3002")
    _entry(w, recent, w.eyad)
    _entry(w, old, w.eyad)
    stale = w.notion.seed(
        S.DB_TASKS,
        {
            S.P_Tasks.TITLE: nt.p_title("junk upsolve"),
            S.P_Tasks.TYPE: nt.p_select("Upsolve"),
            S.P_Tasks.ORIGIN: nt.p_select("Auto"),
            S.P_Tasks.STATUS: nt.p_status("To Do"),
            S.P_Tasks.ASSIGNEE: nt.p_relation([w.eyad]),
            S.P_Tasks.CONTEST: nt.p_relation([old]),
            S.P_Tasks.SOURCE_KEY: nt.p_text("upsolve:junk"),
        },
    )
    result = daily.run(w.context())
    upsolves = [t for t in w.notion.rows(S.DB_TASKS) if get_select(t, S.P_Tasks.TYPE) == "Upsolve"]
    titles = [get_title(t, S.P_Tasks.TITLE) for t in upsolves if get_status(t, S.P_Tasks.STATUS) == "To Do"]
    assert titles == ["🔁 Upsolve Round recent"], "only the recent contest gets a reminder"
    assert get_status(w.notion.page(stale), S.P_Tasks.STATUS) == "Skipped"
    assert "stale upsolve reminders skipped" in result.details


def test_2_window_is_a_config_value() -> None:
    w = build_world()
    _config(w, "UPSOLVE_WINDOW_DAYS", 30)
    onboard.run(w.context())
    old = _contest(w, "Round old", NOW - timedelta(days=20), 120, "CF-3002")
    _entry(w, old, w.eyad)
    daily.run(w.context())
    upsolves = [t for t in w.notion.rows(S.DB_TASKS) if get_select(t, S.P_Tasks.TYPE) == "Upsolve"]
    assert len(upsolves) == 1


def test_2_defaults_are_in_config() -> None:
    assert config.DEFAULTS["UPSOLVE_WINDOW_DAYS"] == 7  # round 3: was 14
    assert config.DEFAULTS["DAILY_LOG_DAYS"] == 60
    assert config.DEFAULTS["WEEKLY_BACKFILL_WEEKS"] == 12
    assert config.DEFAULTS["DASHBOARDS_AUTO"] == 1
    assert config.DEFAULTS["IMPORT_BACKFILL_DAYS"] == 120


# ---------------------------------------------------------------------------
# #3 Real contest names
# ---------------------------------------------------------------------------
def test_3_fetch_all_contests_is_cached_per_session_and_split_by_gym_flag(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_cf(session, method, params=None):
        calls.append({"method": method, **(params or {})})
        return [{"id": 1985, "name": "Round 1985", "phase": "FINISHED", "startTimeSeconds": 1_700_000_000, "durationSeconds": 7200}]

    monkeypatch.setattr(codeforces, "_cf", fake_cf)
    session = requests.Session()
    regular = codeforces.fetch_all_contests(session, False)
    again = codeforces.fetch_all_contests(session, False)
    gym = codeforces.fetch_all_contests(session, True)
    assert [c.external_id for c in regular] == ["CF-1985"] and regular[0].duration_min == 120
    assert regular == again
    assert [c["gym"] for c in calls] == ["false", "true"], "one call per list, then cached"
    assert gym[0].external_id == "GYM-1985" and gym[0].platform == "CF Gym"


def test_3_new_gym_entry_gets_the_real_name_not_a_placeholder() -> None:
    w = build_world()
    real_start = NOW - timedelta(days=2)
    w.sources.contests = {
        "GYM-101911": Contest(
            platform="CF Gym",
            external_id="GYM-101911",
            name="2023 Gym Round A",
            start_utc=real_start,
            duration_min=180,
            link="https://codeforces.com/gym/101911",
        )
    }
    w.sources.cf["eyad_cf"] = [
        cf_solve("101911A", NOW - timedelta(days=2), rating=1200, tags=("greedy",), mode="Live Contest", contest="GYM-101911", judge="CF Gym", name="Gym A")
    ]
    onboard.run(w.context())
    imports.run(w.context())
    contests = [c for c in w.notion.rows(S.DB_CONTESTS) if get_text(c, S.P_Contests.EXTERNAL_ID) == "GYM-101911"]
    assert len(contests) == 1
    assert get_title(contests[0], S.P_Contests.TITLE) == "2023 Gym Round A"
    assert get_select(contests[0], S.P_Contests.KIND) == "Gym"
    assert get_number(contests[0], S.P_Contests.DURATION_MIN) == 180


def test_3_existing_placeholder_contest_and_entry_are_renamed() -> None:
    w = build_world()
    w.sources.contests = {
        "CF-1985": Contest(
            platform="Codeforces",
            external_id="CF-1985",
            name="Codeforces Round 1985 (Div. 2)",
            start_utc=NOW - timedelta(days=3),
            duration_min=120,
            link="https://codeforces.com/contests/1985",
        )
    }
    contest_id = w.notion.seed(
        S.DB_CONTESTS,
        {
            S.P_Contests.TITLE: nt.p_title("Codeforces contest 1985"),
            S.P_Contests.PLATFORM: nt.p_select("Codeforces"),
            S.P_Contests.EXTERNAL_ID: nt.p_text("CF-1985"),
            S.P_Contests.START: nt.p_date(NOW - timedelta(days=3)),
            S.P_Contests.DURATION_MIN: nt.p_number(90),
        },
    )
    entry = _entry(w, contest_id, w.eyad)
    imports.run(w.context())
    assert get_title(w.notion.page(contest_id), S.P_Contests.TITLE) == "Codeforces Round 1985 (Div. 2)"
    assert get_number(w.notion.page(contest_id), S.P_Contests.DURATION_MIN) == 120
    assert get_title(w.notion.page(entry), S.P_ContestEntries.TITLE) == "Codeforces Round 1985 (Div. 2) · Eyad"


# ---------------------------------------------------------------------------
# #4 Total Problems from the problemset (regular CF contests only)
# ---------------------------------------------------------------------------
def test_4_total_problems_filled_from_problemset_counts_fill_only_and_gym_skipped() -> None:
    w = build_world()
    onboard.run(w.context())
    regular = _entry(w, _contest(w, "Round A", NOW - timedelta(days=10), 120, "CF-4010"), w.eyad)
    other = _entry(w, _contest(w, "Round B", NOW - timedelta(days=11), 120, "CF-4020"), w.eyad)
    gym = _entry(w, _contest(w, "Gym X", NOW - timedelta(days=12), 180, "GYM-101911"), w.eyad)
    keep = _entry(w, _contest(w, "Round keep", NOW - timedelta(days=30), 120, "CF-4030"), w.eyad, total=7)
    w.sources.problem_counts = {4010: 6, 4020: 5, 4030: 9}
    imports.run(w.context())
    assert get_number(w.notion.page(regular), S.P_ContestEntries.TOTAL_PROBLEMS) == 6
    assert get_number(w.notion.page(other), S.P_ContestEntries.TOTAL_PROBLEMS) == 5
    assert get_number(w.notion.page(gym), S.P_ContestEntries.TOTAL_PROBLEMS) is None, "gym contests are left empty"
    assert "GYM-101911" not in w.sources.count_calls
    assert get_number(w.notion.page(keep), S.P_ContestEntries.TOTAL_PROBLEMS) == 7, "a value already set is never overwritten"


def test_4_problem_counts_come_from_one_cached_problemset_call(monkeypatch) -> None:
    calls: list[str] = []
    rows = {
        "problems": [
            {"contestId": 1985, "index": "A", "name": "a", "tags": []},
            {"contestId": 1985, "index": "B", "name": "b", "tags": []},
            {"contestId": 1990, "index": "A", "name": "c", "tags": []},
            {"index": "Z"},  # no contest id: not counted
        ]
    }

    def fake_cf(session, method, params=None):
        calls.append(method)
        return rows

    monkeypatch.setattr(codeforces, "_cf", fake_cf)
    session = requests.Session()
    assert codeforces.fetch_problem_counts(session) == {1985: 2, 1990: 1}
    assert codeforces.fetch_problem_counts(session) == {1985: 2, 1990: 1}
    assert codeforces.fetch_problemset(session)[0]["key"] == "CF-1985A"
    assert calls == ["problemset.problems"], "one problemset call per session, shared with the problemset fetch"


def test_4_failed_problemset_call_is_not_cached(monkeypatch) -> None:
    calls: list[str] = []

    def failing_cf(session, method, params=None):
        calls.append(method)
        return None

    monkeypatch.setattr(codeforces, "_cf", failing_cf)
    session = requests.Session()
    assert codeforces.fetch_problem_counts(session) == {}
    assert codeforces.fetch_problem_counts(session) == {}
    assert len(calls) == 2, "a failure is retried on the next call"


def test_4_live_sources_never_look_up_gym_contests(monkeypatch) -> None:
    from hq.main import LiveSources

    def must_not_call(session, method, params=None):
        raise AssertionError(f"unexpected call {method}")

    monkeypatch.setattr(codeforces, "_cf", must_not_call)
    sources = LiveSources(session=requests.Session())
    assert sources.cf_problem_count("GYM-101911") is None


# #5 History depth and the weekly backfill
# ---------------------------------------------------------------------------
def test_5_daily_log_window_follows_config() -> None:
    w = build_world()
    _config(w, "DAILY_LOG_DAYS", 14)
    seed_eyad_solves(w)
    onboard.run(w.context())
    imports.run(w.context())
    recompute.run(w.context())
    rows = [r for r in w.notion.rows(S.DB_DAILY_LOG) if get_relation(r, S.P_DailyLog.MEMBER) == [w.eyad]]
    assert len(rows) == 15


def test_5_weekly_backfill_creates_missing_history_without_reports_or_mvp() -> None:
    w = build_world()
    _config(w, "WEEKLY_BACKFILL_WEEKS", 3)
    w.sources.cf["eyad_cf"] = [
        cf_solve("1000B", datetime(2026, 9, 28, 12, tzinfo=timezone.utc), rating=1000, tags=("greedy",)),
        cf_solve("1001A", datetime(2026, 9, 29, 12, tzinfo=timezone.utc), rating=1000, tags=("greedy",)),
    ]
    _config(w, "IMPORT_BACKFILL_DAYS", 4000)
    onboard.run(w.context())
    imports.run(w.context())
    result = weekly.backfill_run(w.context())
    assert result.result == OK
    summaries = w.notion.rows(S.DB_WEEKLY_SUMMARY)
    eyad_week = [s for s in summaries if get_text(s, S.P_WeeklySummary.SOURCE_KEY) == f"person:{w.eyad}:2026-09-26"]
    assert len(eyad_week) == 1 and get_number(eyad_week[0], S.P_WeeklySummary.PROBLEMS) == 2
    assert any(get_text(s, S.P_WeeklySummary.SOURCE_KEY).startswith("team:") for s in summaries)
    assert any(get_text(s, S.P_WeeklySummary.SOURCE_KEY).startswith("club:") for s in summaries)
    assert w.notion.rows(S.DB_WEEKLY_REPORTS) == [], "no Weekly Reports page for backfilled weeks"
    assert not [a for a in w.notion.rows(S.DB_ACHIEVEMENTS) if get_select(a, S.P_Achievements.BADGE) == "Weekly MVP"]
    assert not any(get_checkbox(s, S.P_WeeklySummary.MVP) for s in summaries)

    second = weekly.backfill_run(w.context())
    assert second.items == 0, "a second backfill changes nothing"


def test_5_weekly_backfill_keeps_mvp_and_report_on_an_existing_row() -> None:
    w = build_world()
    report = w.notion.seed(S.DB_WEEKLY_REPORTS, {S.P_WeeklyReports.TITLE: nt.p_title("report")})
    row = w.notion.seed(
        S.DB_WEEKLY_SUMMARY,
        {
            S.P_WeeklySummary.TITLE: nt.p_title("Eyad · x"),
            S.P_WeeklySummary.SOURCE_KEY: nt.p_text(f"person:{w.eyad}:2026-09-26"),
            S.P_WeeklySummary.MVP: nt.p_checkbox(True),
            S.P_WeeklySummary.REPORT: nt.p_relation([report]),
            S.P_WeeklySummary.MEMBER: nt.p_relation([w.eyad]),
        },
    )
    _config(w, "WEEKLY_BACKFILL_WEEKS", 3)
    weekly.backfill_run(w.context())
    page = w.notion.page(row)
    assert get_checkbox(page, S.P_WeeklySummary.MVP) is True
    assert get_relation(page, S.P_WeeklySummary.REPORT) == [report]


# ---------------------------------------------------------------------------
# #6 Full-history import: 1000 solves, cap 400, three runs
# ---------------------------------------------------------------------------
def test_6_thousand_solves_with_cap_400_complete_in_three_runs_without_duplicates() -> None:
    w = build_world()
    _config(w, "IMPORT_BACKFILL_DAYS", 4000)
    _config(w, "IMPORT_MAX_CREATES", 400)
    w.sources.cf["eyad_cf"] = [
        cf_solve(f"{i + 1}A", NOW - timedelta(hours=i + 1), rating=1000, tags=("greedy",)) for i in range(1000)
    ]
    onboard.run(w.context())
    counts = []
    for _ in range(3):
        imports.run(w.context())
        counts.append(len([p for p in w.notion.rows(S.DB_PROBLEMS) if get_relation(p, S.P_Problems.MEMBER) == [w.eyad]]))
    assert counts == [400, 800, 1000]
    keys = [get_text(p, S.P_Problems.SOURCE_KEY) for p in w.notion.rows(S.DB_PROBLEMS) if get_relation(p, S.P_Problems.MEMBER) == [w.eyad]]
    assert len(set(keys)) == 1000, "no duplicates"
    assert w.notion.page(w.eyad)["properties"][S.P_Members.LAST_SYNC]["date"] is not None, "Last Sync moves only once complete"


# ---------------------------------------------------------------------------
# Who / Team fill (recompute)
# ---------------------------------------------------------------------------
def test_who_team_filled_for_manual_rows_and_only_when_different() -> None:
    w = build_world()
    problem = w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("Manual"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
            S.P_Problems.PROBLEM_KEY: nt.p_text("CF-9999A"),
            S.P_Problems.STATUS: nt.p_status("Solved"),
            S.P_Problems.SOLVED_ON: nt.p_date(NOW - timedelta(days=1)),
        },
    )
    entry = w.notion.seed(
        S.DB_CONTEST_ENTRIES,
        {S.P_ContestEntries.TITLE: nt.p_title("Team round"), S.P_ContestEntries.MODE: nt.p_select("Team"), S.P_ContestEntries.TEAM: nt.p_relation([w.team])},
    )
    goal = w.notion.seed(
        S.DB_GOALS,
        {S.P_Goals.TITLE: nt.p_title("Team goal"), S.P_Goals.SCOPE: nt.p_select("Team"), S.P_Goals.TEAM: nt.p_relation([w.team]), S.P_Goals.METRIC: nt.p_select("Problems Solved")},
    )
    recompute.run(w.context())
    assert get_select(w.notion.page(problem), S.P_Problems.WHO) == "Eyad"
    assert get_select(w.notion.page(problem), S.P_Problems.TEAM) == "Team Alpha"
    assert get_select(w.notion.page(entry), S.P_ContestEntries.WHO) == "Team Alpha"
    assert get_select(w.notion.page(goal), S.P_Goals.WHO) == "Team Alpha"
    w.notion.reset_writes()
    recompute.run(w.context())
    assert not [u for u in w.notion.updates if u[0] in (problem, entry, goal)], "a second run writes nothing for these rows"


# ---------------------------------------------------------------------------
# Manual Problems rows: adoption on import, normalized dedupe in recompute
# ---------------------------------------------------------------------------
def test_manual_row_is_adopted_by_the_import_and_user_values_are_kept() -> None:
    w = build_world()
    w.sources.cf["eyad_cf"] = [cf_solve("1000B", NOW - timedelta(days=2), rating=1000, tags=("greedy",))]
    manual = w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("typed by hand"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
            S.P_Problems.PROBLEM_KEY: nt.p_text("cf-1000b"),
            S.P_Problems.NOTES: nt.p_text("my note"),
            S.P_Problems.RATING: nt.p_number(1111),
        },
    )
    onboard.run(w.context())
    imports.run(w.context())
    rows = [p for p in w.notion.rows(S.DB_PROBLEMS) if get_relation(p, S.P_Problems.MEMBER) == [w.eyad]]
    assert len(rows) == 1, "no second row for the same solve"
    page = w.notion.page(manual)
    assert get_text(page, S.P_Problems.SOURCE_KEY) == f"CF-1000B:{w.eyad}"
    assert get_checkbox(page, S.P_Problems.IMPORTED) is False, "Imported stays unticked on an adopted row"
    assert get_number(page, S.P_Problems.RATING) == 1111, "the user's rating is kept"
    assert get_text(page, S.P_Problems.NOTES) == "my note"
    assert get_status(page, S.P_Problems.STATUS) == "Solved"
    assert get_date(page, S.P_Problems.SOLVED_ON) is not None, "an empty field is filled"


def test_manual_row_matched_by_link_in_the_contest_form() -> None:
    w = build_world()
    w.sources.cf["eyad_cf"] = [cf_solve("1000B", NOW - timedelta(days=2), rating=1000, tags=("greedy",))]
    manual = w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("by link"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
            S.P_Problems.LINK: nt.p_url("https://codeforces.com/contest/1000/problem/b/"),
        },
    )
    onboard.run(w.context())
    imports.run(w.context())
    assert len([p for p in w.notion.rows(S.DB_PROBLEMS) if get_relation(p, S.P_Problems.MEMBER) == [w.eyad]]) == 1
    assert get_text(w.notion.page(manual), S.P_Problems.SOURCE_KEY) == f"CF-1000B:{w.eyad}"


def test_recompute_counts_one_solve_for_rows_that_name_the_same_problem_and_warns() -> None:
    w = build_world()
    w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("first"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
            S.P_Problems.PROBLEM_KEY: nt.p_text("CF-1000B"),
            S.P_Problems.LINK: nt.p_url("https://codeforces.com/problemset/problem/1000/B"),
            S.P_Problems.STATUS: nt.p_status("Solved"),
            S.P_Problems.RATING: nt.p_number(1000),
            S.P_Problems.SOLVED_ON: nt.p_date(NOW - timedelta(days=3)),
        },
    )
    w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("second"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
            S.P_Problems.PROBLEM_KEY: nt.p_text(" cf-1000b "),
            S.P_Problems.LINK: nt.p_url("https://codeforces.com/contest/1000/problem/B/"),
            S.P_Problems.STATUS: nt.p_status("Solved"),
            S.P_Problems.RATING: nt.p_number(1000),
            S.P_Problems.SOLVED_ON: nt.p_date(NOW - timedelta(days=1)),
        },
    )
    result = recompute.run(w.context())
    assert result.result == WARNING
    assert "duplicate rows ignored (1)" in result.details
    assert get_number(w.notion.page(w.eyad), S.P_Members.PROBLEMS_TOTAL) == 1


# ---------------------------------------------------------------------------
# #7 Personal dashboards
# ---------------------------------------------------------------------------
def test_7_onboard_creates_one_dashboard_per_active_member_with_filtered_views() -> None:
    w = build_world()
    onboard.run(w.context())
    dashboards.run(w.context())  # round 3: dashboards are a job of their own, run after recompute and daily
    dashboard_pages = [p for p in w.notion.child_pages if p[0] == dashboards.DASHBOARD_PARENT_PAGE_ID]
    titles = sorted(t for _, t, _ in dashboard_pages)
    assert titles == ["👤 Eyad · Dashboard", "👤 Sara · Dashboard"], "inactive members get none"
    assert get_url(w.notion.page(w.eyad), S.P_Members.DASHBOARD), "the Dashboard URL is stored"
    eyad_views = [v for v in w.notion.views if v["name"] in ("Tasks (open)", "Problems", "Goals", "Achievements")]
    assert eyad_views and all(v["create_database"]["parent"]["page_id"] for v in eyad_views)
    problems = [v for v in w.notion.views if v["name"] == "Problems"]
    assert problems[0]["filter"] == {"property": "Who", "select": {"equals": "Eyad"}}
    assert problems[0]["sorts"] == [{"property": "Solved On", "direction": "descending"}]
    assert {v["name"] for v in w.notion.views} >= {"Tasks (open)", "Daily points", "Weekly points", "Rating history", "Learning Queue", "Problems", "Goals", "Achievements"}


def test_7_second_onboard_does_not_create_another_dashboard() -> None:
    w = build_world()
    onboard.run(w.context())
    dashboards.run(w.context())
    before = len(w.notion.child_pages)
    onboard.run(w.context())
    dashboards.run(w.context())
    assert len(w.notion.child_pages) == before


def test_7_disabled_by_config() -> None:
    w = build_world()
    _config(w, "DASHBOARDS_AUTO", 0)
    onboard.run(w.context())
    assert w.notion.child_pages == []


def test_7_rejected_chart_or_board_becomes_a_table_with_a_warning() -> None:
    w = build_world()
    w.notion.view_faults = {"chart", "board"}
    onboard.run(w.context())
    dashboards.run(w.context())
    board_or_chart = [v for v in w.notion.views if v["name"] in ("Learning Queue", "Daily points")]
    assert board_or_chart
    assert all(v["type"] == "table" for v in board_or_chart)


def test_7_view_server_error_still_stores_the_url_and_no_duplicate_page_is_created() -> None:
    w = build_world()
    w.notion.view_faults = {"*"}  # every view create fails with HTTP 500
    onboard.run(w.context())
    dashboards.run(w.context())
    eyad_pages = [p for p in w.notion.child_pages if p[1] == "👤 Eyad · Dashboard"]
    assert len(eyad_pages) == 1
    assert get_url(w.notion.page(w.eyad), S.P_Members.DASHBOARD), "the URL is written before the views"
    onboard.run(w.context())
    dashboards.run(w.context())
    assert len([p for p in w.notion.child_pages if p[1] == "👤 Eyad · Dashboard"]) == 1, "no duplicate dashboard"


def test_manual_row_adoption_sets_status_solved_and_keeps_user_values() -> None:
    w = build_world()
    w.sources.cf["eyad_cf"] = [cf_solve("1000B", NOW - timedelta(days=2), rating=1000, tags=("greedy",))]
    manual = w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("typed by hand"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
            S.P_Problems.PROBLEM_KEY: nt.p_text("CF-1000B"),
            S.P_Problems.STATUS: nt.p_status("To Do"),
            S.P_Problems.NOTES: nt.p_text("keep me"),
        },
    )
    onboard.run(w.context())
    imports.run(w.context())
    page = w.notion.page(manual)
    assert get_status(page, S.P_Problems.STATUS) == "Solved", "an accepted solve is a fact"
    assert get_text(page, S.P_Problems.NOTES) == "keep me"
    assert get_text(page, S.P_Problems.SOURCE_KEY) == f"CF-1000B:{w.eyad}"
    assert get_checkbox(page, S.P_Problems.IMPORTED) is False
