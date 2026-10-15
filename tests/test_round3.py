"""Round 3: topic practice off by default, upsolve window of 7 days, the LeetCode daily task,
cumulative points on the Daily Log, and row icons (creation, backfill, Daily Log state).
Offline: every platform call goes through the fakes or a FakeSession."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

from hq import config, icons
from hq import notion as nt
from hq import schema as S
from hq.jobs import OK, WARNING, contests, daily, execute, onboard, recompute, cairo_end_of_day
from hq.notion import Notion
from hq.repo import Repo, cairo_day_of, get_date, get_number, get_relation, get_select, get_status, get_text, get_title
from hq.sources import leetcode
from tests.fakes import NOW, build_world, contest, hours_from_now
from tests.helpers import FakeResponse, FakeSession, fixture_json

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 15)
UTC = timezone.utc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _question(day: date = TODAY) -> leetcode.DailyQuestion:
    return leetcode.DailyQuestion(day=day, title="Two Sum", slug="two-sum", difficulty="Easy", topic_names=("Array", "Hash Table"))


def _set_config(w, key: str, value: float) -> None:
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title(key), S.P_Config.NUMBER: nt.p_number(value)})


def _lc_tasks(w) -> list[dict]:
    return [t for t in w.notion.rows(S.DB_TASKS) if get_text(t, S.P_Tasks.SOURCE_KEY).startswith(S.LC_DAILY_SOURCE_PREFIX)]


def _solved_problem(w, member_id: str, key: str, *, link: str | None = None, day: datetime | None = None, rating: int = 1000) -> str:
    props: dict[str, Any] = {
        S.P_Problems.TITLE: nt.p_title(key),
        S.P_Problems.MEMBER: nt.p_relation([member_id]),
        S.P_Problems.PROBLEM_KEY: nt.p_text(key),
        S.P_Problems.STATUS: nt.p_status("Solved"),
        S.P_Problems.SOURCE_KEY: nt.p_text(f"{key}:{member_id}"),
        S.P_Problems.RATING: nt.p_number(rating),
        S.P_Problems.MODE: nt.p_select("Practice"),
    }
    if link:
        props[S.P_Problems.LINK] = nt.p_url(link)
    if day is not None:
        props[S.P_Problems.SOLVED_ON] = nt.p_date(day)
    return w.notion.seed(S.DB_PROBLEMS, props)


def _client(handler) -> tuple[Notion, FakeSession]:
    session = FakeSession(handler)
    return Notion("secret-test", session=session, min_interval=0, sleep=lambda _s: None), session


# ---------------------------------------------------------------------------
# config and schema
# ---------------------------------------------------------------------------
def test_round3_config_defaults_and_keys() -> None:
    assert config.DEFAULTS["AUTO_PRACTICE_TASKS"] == 0
    assert config.DEFAULTS["UPSOLVE_WINDOW_DAYS"] == 7
    assert config.DEFAULTS["LEETCODE_DAILY_TASKS"] == 1
    assert config.DEFAULTS["ICON_BACKFILL_PER_RUN"] == 100
    for key in ("AUTO_PRACTICE_TASKS", "LEETCODE_DAILY_TASKS", "ICON_BACKFILL_PER_RUN"):
        assert getattr(S.ConfigKey, key) == key
        assert key in config.DEFAULTS


def test_cumulative_points_property_name() -> None:
    assert S.P_DailyLog.CUMULATIVE_POINTS == "Cumulative Points"


def test_readme_documents_the_round3_settings() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for key in ("AUTO_PRACTICE_TASKS", "LEETCODE_DAILY_TASKS", "ICON_BACKFILL_PER_RUN"):
        assert f"`{key}`" in text, key
    assert "| `UPSOLVE_WINDOW_DAYS` | 7 |" in text


# ---------------------------------------------------------------------------
# 1. topic practice off by default; upsolve window 7 days
# ---------------------------------------------------------------------------
def test_topic_practice_tasks_are_off_by_default() -> None:
    w = build_world()
    w.sources.problemset = [{"key": "CF-1000B", "name": "Bee", "link": "https://x", "rating": 1000, "tags": ["greedy"]}]
    onboard.run(w.context())
    w.notion.reset_writes()
    result = daily.run(w.context())
    assert result.result == WARNING  # the LeetCode daily fetch is not faked here; the practice part is the point
    assert [t for t in w.notion.rows(S.DB_TASKS) if get_select(t, S.P_Tasks.TYPE) == "Topic Practice"] == []
    assert "topic practice tasks: off" in result.details


def test_upsolve_reminders_use_the_seven_day_window() -> None:
    w = build_world()
    w.sources.lc_daily = _question()
    onboard.run(w.context())
    for name, days_ago, ext in (("Round recent", 5, "CF-4001"), ("Round old", 10, "CF-4002")):
        cid = w.notion.seed(
            S.DB_CONTESTS,
            {
                S.P_Contests.TITLE: nt.p_title(name),
                S.P_Contests.START: nt.p_date(NOW - timedelta(days=days_ago)),
                S.P_Contests.DURATION_MIN: nt.p_number(120),
                S.P_Contests.PLATFORM: nt.p_select("Codeforces"),
                S.P_Contests.EXTERNAL_ID: nt.p_text(ext),
            },
        )
        w.notion.seed(
            S.DB_CONTEST_ENTRIES,
            {
                S.P_ContestEntries.TITLE: nt.p_title(name),
                S.P_ContestEntries.MEMBER: nt.p_relation([w.eyad]),
                S.P_ContestEntries.CONTEST: nt.p_relation([cid]),
                S.P_ContestEntries.STATUS: nt.p_status("Attended"),
                S.P_ContestEntries.TOTAL_PROBLEMS: nt.p_number(5),
                S.P_ContestEntries.SOLVED: nt.p_number(1),
                S.P_ContestEntries.UPSOLVED: nt.p_number(0),
            },
        )
    daily.run(w.context())
    upsolves = [get_title(t, S.P_Tasks.TITLE) for t in w.notion.rows(S.DB_TASKS) if get_select(t, S.P_Tasks.TYPE) == "Upsolve"]
    assert upsolves == ["🔁 Upsolve Round recent"]


# ---------------------------------------------------------------------------
# 2. LeetCode daily: source
# ---------------------------------------------------------------------------
def test_daily_question_reads_the_live_shape_and_sends_browser_headers() -> None:
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        seen.append((method, url, kwargs))
        return FakeResponse(200, fixture_json("leetcode/daily.json"))

    question = leetcode.fetch_daily_question_or_none(FakeSession(handler))
    assert question == leetcode.DailyQuestion(
        day=TODAY, title="Two Sum", slug="two-sum", difficulty="Easy", topic_names=("Array", "Hash Table")
    )
    assert question.problem_key == "LC-two-sum"
    assert question.link == "https://leetcode.com/problems/two-sum/"
    method, url, kwargs = seen[0]
    assert method == "POST"
    assert url == "https://leetcode.com/graphql"
    assert kwargs["json"]["query"] == "query { activeDailyCodingChallengeQuestion { date link question { title titleSlug difficulty topicTags { name } } } }"
    headers = kwargs["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["Referer"] == "https://leetcode.com"
    assert "Mozilla" in headers["User-Agent"]


@pytest.mark.parametrize(
    "handler",
    [
        lambda m, u, k: FakeResponse(500, {"message": "boom"}),
        lambda m, u, k: FakeResponse(200, {"errors": [{"message": "x"}]}),
        lambda m, u, k: FakeResponse(200, {"data": {"activeDailyCodingChallengeQuestion": None}}),
        lambda m, u, k: FakeResponse(200, {"data": {"activeDailyCodingChallengeQuestion": {"date": "not-a-date", "question": {"title": "T", "titleSlug": "t"}}}}),
        lambda m, u, k: FakeResponse(200, {"data": {"activeDailyCodingChallengeQuestion": {"date": "2026-10-15", "question": None}}}),
        lambda m, u, k: requests.ConnectionError("down"),
    ],
)
def test_daily_question_is_none_when_it_cannot_be_read(handler) -> None:
    assert leetcode.fetch_daily_question_or_none(FakeSession(handler)) is None


def test_unknown_daily_difficulty_is_dropped() -> None:
    payload = fixture_json("leetcode/daily.json")
    payload["data"]["activeDailyCodingChallengeQuestion"]["question"]["difficulty"] = "Expert"
    question = leetcode.fetch_daily_question_or_none(FakeSession(lambda m, u, k: FakeResponse(200, payload)))
    assert question is not None and question.difficulty is None


# ---------------------------------------------------------------------------
# 2. LeetCode daily: the daily job
# ---------------------------------------------------------------------------
def test_lc_daily_task_per_active_member_with_the_documented_fields() -> None:
    w = build_world()
    w.sources.lc_daily = _question()
    onboard.run(w.context())
    w.notion.reset_writes()
    result = daily.run(w.context())
    assert result.result == OK
    tasks = _lc_tasks(w)
    assert sorted(get_relation(t, S.P_Tasks.ASSIGNEE)[0] for t in tasks) == sorted([w.eyad, w.sara]), "active members only"
    task = next(t for t in tasks if get_relation(t, S.P_Tasks.ASSIGNEE) == [w.eyad])
    assert get_title(task, S.P_Tasks.TITLE) == "💡 LeetCode daily: Two Sum"
    assert get_select(task, S.P_Tasks.TYPE) == "Custom"
    assert get_status(task, S.P_Tasks.STATUS) == "To Do"
    assert get_select(task, S.P_Tasks.ORIGIN) == "Auto"
    assert get_select(task, S.P_Tasks.PRIORITY) == "Medium"
    assert get_select(task, S.P_Tasks.WHO) == "Eyad"
    assert get_text(task, S.P_Tasks.SOURCE_KEY) == f"lcdaily:2026-10-15:{w.eyad}"
    assert get_text(task, S.P_Tasks.LINKS) == "LC-two-sum | https://leetcode.com/problems/two-sum/"
    assert get_text(task, S.P_Tasks.NOTES) == "Easy · Array, Hash Table"
    assert get_date(task, S.P_Tasks.DUE) == cairo_end_of_day(TODAY)
    assert get_number(task, S.P_Tasks.TARGET) == 1
    assert task["icon"] == icons.ui("leetcode_daily")


def test_lc_daily_task_is_created_once_per_day() -> None:
    w = build_world()
    w.sources.lc_daily = _question()
    onboard.run(w.context())
    daily.run(w.context())
    before = len(_lc_tasks(w))
    daily.run(w.context())  # a rerun the same day
    assert len(_lc_tasks(w)) == before == 2


def test_yesterdays_open_lc_daily_task_is_skipped() -> None:
    w = build_world()
    w.sources.lc_daily = _question()
    onboard.run(w.context())
    old = w.notion.seed(
        S.DB_TASKS,
        {
            S.P_Tasks.TITLE: nt.p_title("💡 LeetCode daily: Old"),
            S.P_Tasks.ASSIGNEE: nt.p_relation([w.eyad]),
            S.P_Tasks.TYPE: nt.p_select("Custom"),
            S.P_Tasks.ORIGIN: nt.p_select("Auto"),
            S.P_Tasks.STATUS: nt.p_status("To Do"),
            S.P_Tasks.DUE: nt.p_date(cairo_end_of_day(date(2026, 10, 14))),
            S.P_Tasks.SOURCE_KEY: nt.p_text(f"lcdaily:2026-10-14:{w.eyad}"),
        },
    )
    daily.run(w.context())
    assert get_status(w.notion.page(old), S.P_Tasks.STATUS) == "Skipped"


def test_failed_lc_fetch_is_a_warning_with_no_tasks() -> None:
    w = build_world()
    onboard.run(w.context())  # w.sources.lc_daily stays None: the fetch failed
    result = daily.run(w.context())
    assert result.result == WARNING
    assert "could not be read" in result.details
    assert _lc_tasks(w) == []
    assert daily.daily_due(w.context()) is False, "the day's job still counts as run"


def test_lc_daily_switched_off_makes_no_request_and_no_tasks() -> None:
    w = build_world()
    _set_config(w, "LEETCODE_DAILY_TASKS", 0)
    w.sources.lc_daily = _question()
    onboard.run(w.context())
    result = daily.run(w.context())
    assert result.result == OK
    assert "lc_daily" not in w.sources.calls
    assert _lc_tasks(w) == []
    assert "LeetCode daily tasks: off" in result.details


def test_stale_lc_question_at_midday_is_a_warning_and_no_task() -> None:
    w = build_world()
    w.sources.lc_daily = _question(day=date(2026, 10, 14))  # UTC today is the 15th
    onboard.run(w.context())
    result = daily.run(w.context())
    assert result.result == WARNING
    assert "LeetCode daily is for 2026-10-14" in result.details
    assert _lc_tasks(w) == []
    assert daily.daily_due(w.context()) is False


def test_stale_lc_question_early_in_the_day_is_retried_later() -> None:
    early = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)  # 04:00 in Cairo, before LeetCode's rollover is expected
    w = build_world()
    w.sources.lc_daily = _question(day=date(2026, 10, 14))
    onboard.run(w.context(now=early))
    result = daily.run(w.context(now=early))
    assert result.result == OK, "expected rollover, not a warning"
    assert _lc_tasks(w) == []
    assert daily.daily_due(w.context(now=early)) is True, "the day is not marked as run, so the next run retries"

    later = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
    w.sources.lc_daily = _question()
    daily.run(w.context(now=later))
    assert len(_lc_tasks(w)) == 2
    assert daily.daily_due(w.context(now=later)) is False


def test_lc_daily_task_is_done_when_the_member_solves_it_by_key() -> None:
    w = build_world()
    w.sources.lc_daily = _question()
    onboard.run(w.context())
    daily.run(w.context())
    _solved_problem(w, w.eyad, "lc-two-sum")  # the key matches case-insensitively
    recompute.run(w.context())
    eyad_task = next(t for t in _lc_tasks(w) if get_relation(t, S.P_Tasks.ASSIGNEE) == [w.eyad])
    sara_task = next(t for t in _lc_tasks(w) if get_relation(t, S.P_Tasks.ASSIGNEE) == [w.sara])
    assert get_status(eyad_task, S.P_Tasks.STATUS) == "Done"
    assert get_number(eyad_task, S.P_Tasks.DONE_COUNT) == 1
    assert get_status(sara_task, S.P_Tasks.STATUS) == "To Do"
    assert get_number(sara_task, S.P_Tasks.DONE_COUNT) == 0


def test_lc_daily_task_is_done_when_the_member_solves_it_by_link() -> None:
    w = build_world()
    w.sources.lc_daily = _question()
    onboard.run(w.context())
    daily.run(w.context())
    _solved_problem(w, w.sara, "CF-9999Z", link="https://www.leetcode.com/problems/two-sum")  # a different key, same problem
    recompute.run(w.context())
    sara_task = next(t for t in _lc_tasks(w) if get_relation(t, S.P_Tasks.ASSIGNEE) == [w.sara])
    assert get_status(sara_task, S.P_Tasks.STATUS) == "Done"


# ---------------------------------------------------------------------------
# 3. cumulative points
# ---------------------------------------------------------------------------
def test_cumulative_points_is_the_running_total_and_is_written_only_when_it_changes() -> None:
    w = build_world()
    onboard.run(w.context())
    _solved_problem(w, w.eyad, "CF-1000B", day=datetime(2026, 10, 13, 10, 0, tzinfo=UTC), rating=1000)
    _solved_problem(w, w.eyad, "CF-1000C", day=datetime(2026, 10, 15, 9, 0, tzinfo=UTC), rating=1100)
    recompute.run(w.context())

    def rows_by_day() -> dict[date, dict]:
        return {
            cairo_day_of(get_date(r, S.P_DailyLog.DATE)): r
            for r in w.notion.rows(S.DB_DAILY_LOG)
            if get_relation(r, S.P_DailyLog.MEMBER) == [w.eyad]
        }

    rows = rows_by_day()
    running = 0.0
    for day in sorted(rows):
        running = round(running + (get_number(rows[day], S.P_DailyLog.POINTS) or 0), 2)
        assert get_number(rows[day], S.P_DailyLog.CUMULATIVE_POINTS) == pytest.approx(running), day
    assert get_number(rows[date(2026, 10, 13)], S.P_DailyLog.POINTS) > 0
    assert get_number(rows[date(2026, 10, 15)], S.P_DailyLog.CUMULATIVE_POINTS) == pytest.approx(
        get_number(w.notion.page(w.eyad), S.P_Members.TOTAL_POINTS)
    )

    w.notion.reset_writes()
    recompute.run(w.context())
    assert [props for _, props in w.notion.updates if S.P_DailyLog.CUMULATIVE_POINTS in props] == []


# ---------------------------------------------------------------------------
# 4. row icons: the catalogue and the mapping
# ---------------------------------------------------------------------------
def _catalog() -> dict[str, dict[str, str]]:
    return json.loads((ROOT / "hq" / "icons.json").read_text(encoding="utf-8"))


def test_icon_catalogue_has_an_emoji_fallback_for_every_key() -> None:
    cat = _catalog()
    assert set(cat["ui"]) <= set(cat["emoji_fallback"])
    assert set(cat["judges"]) <= set(cat["emoji_fallback"])


def test_judge_icons_use_logos_with_the_cf_gym_alias_and_fall_back_to_emoji() -> None:
    cat = _catalog()
    assert icons.judge("CF Gym") == {"type": "external", "external": {"url": cat["judges"]["Codeforces Gym"]}}
    assert icons.judge("AtCoder") == {"type": "external", "external": {"url": cat["judges"]["AtCoder"]}}
    assert icons.judge("Vjudge") == {"type": "emoji", "emoji": cat["emoji_fallback"]["Vjudge"]}
    assert icons.judge("Unknown judge") is None
    assert icons.judge(None) is None


@pytest.mark.parametrize(
    ("db", "values", "expected"),
    [
        (S.DB_PROBLEMS, {S.P_Problems.JUDGE: nt.p_select("LeetCode")}, "judge:LeetCode"),
        (S.DB_CONTESTS, {S.P_Contests.PLATFORM: nt.p_select("AtCoder")}, "judge:AtCoder"),
        (S.DB_TASKS, {S.P_Tasks.TYPE: nt.p_select("Contest")}, "contest"),
        (S.DB_TASKS, {S.P_Tasks.TYPE: nt.p_select("Topic Practice")}, "topic"),
        (S.DB_TASKS, {S.P_Tasks.TYPE: nt.p_select("Upsolve")}, "upsolve"),
        (S.DB_TASKS, {S.P_Tasks.TYPE: nt.p_select("Sheet")}, "sheet"),
        (S.DB_TASKS, {S.P_Tasks.TYPE: nt.p_select("Reading")}, "reading"),
        (S.DB_TASKS, {S.P_Tasks.TYPE: nt.p_select("Custom")}, "checklist"),
        (S.DB_CONTEST_ENTRIES, {}, "contest"),
        (S.DB_DAILY_LOG, {S.P_DailyLog.ACTIVE: nt.p_checkbox(True), S.P_DailyLog.FROZEN: nt.p_checkbox(False)}, "streak"),
        (S.DB_DAILY_LOG, {S.P_DailyLog.ACTIVE: nt.p_checkbox(False), S.P_DailyLog.FROZEN: nt.p_checkbox(False)}, "daily"),
        (S.DB_DAILY_LOG, {S.P_DailyLog.ACTIVE: nt.p_checkbox(False), S.P_DailyLog.FROZEN: nt.p_checkbox(True)}, "freeze"),
        (S.DB_ACHIEVEMENTS, {S.P_Achievements.TIER: nt.p_select("Bronze")}, "medal_bronze"),
        (S.DB_ACHIEVEMENTS, {S.P_Achievements.TIER: nt.p_select("Silver")}, "medal_silver"),
        (S.DB_ACHIEVEMENTS, {S.P_Achievements.TIER: nt.p_select("Gold")}, "medal_gold"),
        (S.DB_ACHIEVEMENTS, {S.P_Achievements.TIER: nt.p_select("Legendary")}, "crown"),
        (S.DB_WEEKLY_SUMMARY, {}, "weekly_summary"),
        (S.DB_WEEKLY_REPORTS, {}, "weekly_report"),
        (S.DB_RATING_HISTORY, {}, "rating"),
        (S.DB_GOALS, {}, "goal"),
        (S.DB_LEARNING_QUEUE, {}, "topic"),
        (S.DB_SYNC_LOG, {}, "sync_log"),
        (S.DB_MEMBERS, {}, "member"),
        (S.DB_TOPICS, {}, None),
    ],
)
def test_icon_for_maps_every_database_as_specified(db: str, values: dict, expected: str | None) -> None:
    got = icons.icon_for(db, values)
    if expected is None:
        assert got is None
    elif expected.startswith("judge:"):
        assert got == icons.judge(expected.split(":", 1)[1])
    else:
        assert got == icons.ui(expected)
        assert got is not None and got["type"] in ("external", "emoji")


def test_contest_task_gets_its_type_icon_not_the_platform_logo() -> None:
    w = build_world()
    w.sources.upcoming = [contest("Codeforces Round 1000 (Div. 2)", hours_from_now(20), ext="CF-2050")]
    contests.run(w.context())
    contest_row = next(c for c in w.notion.rows(S.DB_CONTESTS) if get_text(c, S.P_Contests.EXTERNAL_ID) == "CF-2050")
    assert contest_row["icon"] == icons.judge("Codeforces")
    tasks = [t for t in w.notion.rows(S.DB_TASKS) if get_select(t, S.P_Tasks.TYPE) == "Contest"]
    assert tasks
    assert all(t["icon"] == icons.ui("contest") for t in tasks)
    assert all(t["icon"] != icons.judge("Codeforces") for t in tasks)


def test_lc_daily_and_custom_tasks_are_told_apart_by_source_key() -> None:
    lc = {S.P_Tasks.TYPE: nt.p_select("Custom"), S.P_Tasks.SOURCE_KEY: nt.p_text("lcdaily:2026-10-15:x")}
    plain = {S.P_Tasks.TYPE: nt.p_select("Custom"), S.P_Tasks.SOURCE_KEY: nt.p_text("")}
    assert icons.icon_for(S.DB_TASKS, lc) == icons.ui("leetcode_daily")
    assert icons.icon_for(S.DB_TASKS, plain) == icons.ui("checklist")


def test_is_ours_recognises_only_this_modules_icons() -> None:
    assert icons.is_ours(icons.ui("streak"))
    assert icons.is_ours(icons.judge("AtCoder"))
    assert not icons.is_ours({"type": "emoji", "emoji": "🦄"})
    assert not icons.is_ours(None)


def test_rows_created_by_the_jobs_carry_icons() -> None:
    w = build_world()
    onboard.run(w.context())
    queue = w.notion.rows(S.DB_LEARNING_QUEUE)
    assert queue and all(r["icon"] == icons.ui("topic") for r in queue)
    assert icons.ui("member") in w.notion.child_icons, "personal dashboards get the member icon"


def test_sync_log_row_gets_its_icon_at_create() -> None:
    w = build_world()
    execute(w.context(), "daily", daily.run)  # no LeetCode question: a Warning, so a Sync Log row is written
    logs = w.notion.rows(S.DB_SYNC_LOG)
    assert logs and logs[-1]["icon"] == icons.ui("sync_log")


def test_repo_create_sends_and_caches_the_icon() -> None:
    w = build_world()
    repo = w.context().repo
    page = repo.create(S.DB_TASKS, {S.P_Tasks.TITLE: nt.p_title("x")}, icon=icons.ui("sheet"))
    assert page["icon"] == icons.ui("sheet")
    assert w.notion.page(page["id"])["icon"] == icons.ui("sheet")


# ---------------------------------------------------------------------------
# 4. row icons: backfill and the Daily Log state
# ---------------------------------------------------------------------------
def test_backfill_fills_empty_icons_only_and_respects_the_cap() -> None:
    w = build_world()
    _set_config(w, "ICON_BACKFILL_PER_RUN", 2)
    problems = [
        w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title(f"p{i}"), S.P_Problems.JUDGE: nt.p_select("Codeforces")})
        for i in range(3)
    ]
    user = w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("mine"), S.P_Problems.JUDGE: nt.p_select("Codeforces")})
    w.notion.update_page(user, icon={"type": "emoji", "emoji": "🦄"})
    w.notion.reset_writes()

    repo = Repo(w.notion, w.ids)
    assert icons.backfill(repo, config.load(repo)) == 2
    assert [w.notion.page(p)["icon"] is not None for p in problems] == [True, True, False]
    assert w.notion.page(user)["icon"] == {"type": "emoji", "emoji": "🦄"}, "a user's icon is never replaced"

    repo2 = Repo(w.notion, w.ids)
    assert icons.backfill(repo2, config.load(repo2)) == 2
    assert w.notion.page(problems[2])["icon"] == icons.judge("Codeforces")
    assert w.notion.page(user)["icon"] == {"type": "emoji", "emoji": "🦄"}


def test_backfill_dry_run_records_but_does_not_write() -> None:
    w = build_world()
    w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("p"), S.P_Problems.JUDGE: nt.p_select("AtCoder")})
    w.notion.reset_writes()
    repo = Repo(w.notion, w.ids, dry_run=True)
    assert icons.backfill(repo, config.load(repo)) >= 1
    assert w.notion.icon_updates == []
    assert any(line.startswith("icon ") for line in repo.write_log)


def test_daily_log_state_icon_follows_activity_and_keeps_a_user_icon() -> None:
    w = build_world()
    onboard.run(w.context())
    yesterday = date(2026, 10, 14)
    seeded = w.notion.seed(
        S.DB_DAILY_LOG,
        {
            S.P_DailyLog.TITLE: nt.p_title(f"Eyad · {yesterday.isoformat()}"),
            S.P_DailyLog.MEMBER: nt.p_relation([w.eyad]),
            S.P_DailyLog.DATE: nt.p_date(yesterday, with_time=False),
            S.P_DailyLog.ACTIVE: nt.p_checkbox(True),
            S.P_DailyLog.FROZEN: nt.p_checkbox(False),
            S.P_DailyLog.SOURCE_KEY: nt.p_text(f"{w.eyad}:{yesterday.isoformat()}"),
        },
    )
    w.notion.update_page(seeded, icon=icons.ui("streak"))  # our icon, but no solve: not active any more
    recompute.run(w.context())
    assert w.notion.page(seeded)["icon"] == icons.ui("daily")

    today_row = next(r for r in w.notion.rows(S.DB_DAILY_LOG) if get_text(r, S.P_DailyLog.SOURCE_KEY) == f"{w.eyad}:2026-10-15")
    w.notion.update_page(today_row["id"], icon={"type": "emoji", "emoji": "🦄"})
    recompute.run(w.context())
    assert w.notion.page(today_row["id"])["icon"] == {"type": "emoji", "emoji": "🦄"}, "a user's own icon on a Daily Log row is kept"
