"""Tests for the independent-review fixes (round 1). One group per finding, named F<n>."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hq import notion as nt
from hq import schema as S
from hq._net import RateLimiter
from hq.jobs import OK, WARNING, JobResult, alerts, contests, daily, execute, imports, onboard, recompute, weekly
from hq.models import Solve
from hq.notion import Notion, NotionError
from hq.repo import Repo, get_checkbox, get_date, get_number, get_relation, get_select, get_status, get_text, get_title
from hq.sources import atcoder, codeforces
from hq.timeutil import CAIRO_TZ
from tests.fakes import NOW, NOW_SATURDAY, build_world, contest, enable_practice, hours_from_now, seed_eyad_solves
from tests.helpers import FakeResponse, FakeSession

ROOT = Path(__file__).resolve().parent.parent


def _chain(w, now=NOW):
    onboard.run(w.context(now=now))
    imports.run(w.context(now=now))
    return recompute.run(w.context(now=now))


def _tasks_of(w, member_id, task_type=None):
    return [
        t
        for t in w.notion.rows(S.DB_TASKS)
        if member_id in get_relation(t, S.P_Tasks.ASSIGNEE)
        and (task_type is None or get_select(t, S.P_Tasks.TYPE) == task_type)
    ]


# ---------------------------------------------------------------------------
# F1: no blind retry of a failed page create; adopt by key; dedupe on read
# ---------------------------------------------------------------------------
def _notion_with(responses: list[FakeResponse], sleeps: list[float]) -> tuple[Notion, list[str]]:
    calls: list[str] = []

    def handler(method, url, kwargs):
        calls.append(f"{method} {url}")
        return responses.pop(0)

    notion = Notion("secret-test", session=FakeSession(handler), min_interval=0, sleep=sleeps.append)
    return notion, calls


def test_F1_post_pages_on_500_is_not_retried() -> None:
    sleeps: list[float] = []
    notion, calls = _notion_with([FakeResponse(500, text="boom")], sleeps)
    with pytest.raises(NotionError):
        notion.create_page("ds-1", {})
    assert len(calls) == 1, "a 500 on POST /pages must not be repeated (the row may exist)"
    assert sleeps == []


def test_F1_post_pages_network_error_is_not_retried() -> None:
    import requests

    sleeps: list[float] = []

    def handler(method, url, kwargs):
        raise requests.ConnectionError("timeout")

    calls: list[str] = []
    notion = Notion("secret-test", session=FakeSession(lambda m, u, k: calls.append(u) or handler(m, u, k)), min_interval=0, sleep=sleeps.append)
    with pytest.raises(NotionError):
        notion.create_page("ds-1", {})
    assert len(calls) == 1


def test_F1_post_pages_429_public_api_request_blocked_is_never_retried() -> None:
    sleeps: list[float] = []
    blocked = FakeResponse(429, payload={"object": "error", "code": "public_api_request_blocked"}, headers={"Retry-After": "0"})
    notion, calls = _notion_with([blocked, FakeResponse(200, payload={"id": "x"})], sleeps)
    with pytest.raises(NotionError, match="public_api_request_blocked"):
        notion.create_page("ds-1", {})
    assert len(calls) == 1


def test_F1_post_pages_plain_429_is_retried_after_retry_after() -> None:
    sleeps: list[float] = []
    limited = FakeResponse(429, payload={"code": "rate_limited"}, headers={"Retry-After": "2"})
    notion, calls = _notion_with([limited, FakeResponse(200, payload={"id": "row-1"})], sleeps)
    assert notion.create_page("ds-1", {})["id"] == "row-1"
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_F1_get_still_retries_server_errors() -> None:
    sleeps: list[float] = []
    notion, calls = _notion_with([FakeResponse(500, text="x"), FakeResponse(200, payload={"object": "page"})], sleeps)
    assert notion.get_page("page-1")["object"] == "page"
    assert len(calls) == 2, "reads are idempotent and keep the retry"


def test_F1_repo_create_adopts_a_row_stored_before_the_failure() -> None:
    w = build_world()
    w.notion.create_faults = ["commit_then_fail"]
    repo = Repo(w.notion, w.ids)
    page = repo.create(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("x"), S.P_Problems.SOURCE_KEY: nt.p_text("CF-9Z:abc")})
    rows = [r for r in w.notion.rows(S.DB_PROBLEMS) if get_text(r, S.P_Problems.SOURCE_KEY) == "CF-9Z:abc"]
    assert len(rows) == 1, "the row was stored once; the adopt path must not create a second one"
    assert page["id"] == rows[0]["id"]


def test_F1_repo_create_retries_once_when_nothing_was_stored() -> None:
    w = build_world()
    w.notion.create_faults = ["fail"]  # first attempt stores nothing and fails
    repo = Repo(w.notion, w.ids)
    repo.create(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("y"), S.P_Problems.SOURCE_KEY: nt.p_text("CF-8Z:abc")})
    rows = [r for r in w.notion.rows(S.DB_PROBLEMS) if get_text(r, S.P_Problems.SOURCE_KEY) == "CF-8Z:abc"]
    assert len(rows) == 1


def test_F1_repo_create_without_a_key_is_not_retried() -> None:
    w = build_world()
    w.notion.create_faults = ["fail"]
    repo = Repo(w.notion, w.ids)
    with pytest.raises(NotionError):
        repo.create(S.DB_LEARNING_QUEUE, {S.P_LearningQueue.TITLE: nt.p_title("no key")})
    assert w.notion.rows(S.DB_LEARNING_QUEUE) == []


def test_F1_recompute_ignores_duplicate_problem_rows_and_warns() -> None:
    w = build_world()
    seed_eyad_solves(w)
    imports_result = _chain(w)
    assert imports_result.result in (OK, WARNING)
    solved_props = {
        S.P_Problems.TITLE: nt.p_title("Dup"),
        S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
        S.P_Problems.PROBLEM_KEY: nt.p_text("CF-1985C"),
        S.P_Problems.SOURCE_KEY: nt.p_text(f"CF-1985C:{w.eyad}"),
        S.P_Problems.STATUS: nt.p_status("Solved"),
        S.P_Problems.RATING: nt.p_number(1200),
        S.P_Problems.MODE: nt.p_select("Live Contest"),
        S.P_Problems.SOLVED_ON: nt.p_date(NOW - timedelta(days=2)),
    }
    w.notion.seed(S.DB_PROBLEMS, solved_props)
    result = recompute.run(w.context())
    assert result.result == WARNING
    assert "duplicate rows ignored" in result.details
    assert "CF-1985C:" in result.details
    assert get_number(w.notion.page(w.eyad), S.P_Members.PROBLEMS_TOTAL) == 3, "the duplicate is not counted"


def test_F1_recompute_counts_one_contest_entry_per_contest_and_member() -> None:
    w = build_world()
    contest_id = w.notion.seed(S.DB_CONTESTS, {S.P_Contests.TITLE: nt.p_title("Round X"), S.P_Contests.EXTERNAL_ID: nt.p_text("CF-7777")})
    for index in range(2):
        w.notion.seed(
            S.DB_CONTEST_ENTRIES,
            {
                S.P_ContestEntries.TITLE: nt.p_title(f"Round X · Eyad {index}"),
                S.P_ContestEntries.MEMBER: nt.p_relation([w.eyad]),
                S.P_ContestEntries.CONTEST: nt.p_relation([contest_id]),
                S.P_ContestEntries.STATUS: nt.p_status("Attended"),
                S.P_ContestEntries.DATE: nt.p_date(date(2026, 10, 13), with_time=False),
            },
        )
    result = recompute.run(w.context())
    assert result.result == WARNING
    day = [r for r in w.notion.rows(S.DB_DAILY_LOG) if get_text(r, S.P_DailyLog.SOURCE_KEY) == f"{w.eyad}:2026-10-13"][0]
    assert get_number(day, S.P_DailyLog.CONTESTS) == 1


# ---------------------------------------------------------------------------
# F2: one member / contest failing does not stop the others; state still set
# ---------------------------------------------------------------------------
def test_F2_daily_member_failure_is_isolated_and_state_is_set(monkeypatch) -> None:
    w = build_world()
    enable_practice(w)
    w.sources.problemset = [{"key": "CF-1000B", "name": "Bee", "link": "https://x", "rating": 1000, "tags": ["greedy"]}]
    onboard.run(w.context())
    original = daily._create_upsolve_tasks

    def boom(ctx, member, today):
        if member.name == "Eyad":
            raise RuntimeError("upsolve exploded")
        return original(ctx, member, today)

    monkeypatch.setattr(daily, "_create_upsolve_tasks", boom)
    result = daily.run(w.context())
    assert result.result == WARNING
    assert "FAILED Eyad" in result.details
    assert len(_tasks_of(w, w.sara, "Topic Practice")) == 1, "the other member still gets today's task"
    assert daily.daily_due(w.context()) is False, "STATE_LAST_DAILY is set even though one member failed"


def test_F2_onboard_member_failure_is_isolated(monkeypatch) -> None:
    w = build_world()
    original = onboard._onboard_member

    def boom(ctx, member, topics, pairs):
        if member.name == "Eyad":
            raise RuntimeError("queue exploded")
        return original(ctx, member, topics, pairs)

    monkeypatch.setattr(onboard, "_onboard_member", boom)
    result = onboard.run(w.context())
    assert result.result == WARNING
    assert [r for r in w.notion.rows(S.DB_LEARNING_QUEUE) if get_relation(r, S.P_LearningQueue.MEMBER) == [w.sara]]


def test_F2_contest_failure_is_isolated(monkeypatch) -> None:
    w = build_world()
    w.sources.upcoming = [
        contest("Codeforces Round A (Div. 2)", hours_from_now(30), ext="CF-AAA1"),
        contest("Codeforces Round B (Div. 2)", hours_from_now(31), ext="CF-BBB1"),
    ]
    original = contests._upsert_contest

    def boom(ctx, c):
        if c.external_id == "CF-AAA1":
            raise RuntimeError("bad contest")
        return original(ctx, c)

    monkeypatch.setattr(contests, "_upsert_contest", boom)
    result = contests.run(w.context())
    assert result.result == WARNING
    ids = {get_text(r, S.P_Contests.EXTERNAL_ID) for r in w.notion.rows(S.DB_CONTESTS)}
    assert ids == {"CF-BBB1"}


def test_F2_alert_failure_is_isolated(monkeypatch) -> None:
    w = build_world()
    w.sources.upcoming = [
        contest("Codeforces Round C (Div. 2)", NOW + timedelta(minutes=15), ext="CF-CCC1"),
        contest("Codeforces Round D (Div. 2)", NOW + timedelta(minutes=16), ext="CF-DDD1"),
    ]
    contests.run(w.context())
    original = alerts._alert_contest

    def boom(ctx, page, minutes, tasks, members):
        if get_text(page, S.P_Contests.EXTERNAL_ID) == "CF-CCC1":
            raise RuntimeError("alert exploded")
        return original(ctx, page, minutes, tasks, members)

    monkeypatch.setattr(alerts, "_alert_contest", boom)
    result = alerts.run(w.context())
    assert result.result == WARNING
    assert len(w.notion.comments) == 1, "the second contest still gets its reminder"


# ---------------------------------------------------------------------------
# F3: a bad CF handle is skipped and reported; a failed batch retries per handle
# ---------------------------------------------------------------------------
def test_F3_bad_cf_handle_is_skipped_and_reported() -> None:
    w = build_world()
    seed_eyad_solves(w)
    w.sources.failing = {"sara_cf"}  # also makes Sara's solve call fail: a source failure, not a bad handle
    w.sources.cf_info.pop("sara_cf", None)
    result = imports.run(w.context())
    assert result.result == WARNING
    assert "bad CF handles (skipped): sara_cf" in result.details
    assert get_number(w.notion.page(w.eyad), S.P_Members.CF_RATING) == 1150, "good handles are still updated"


def test_F3_failed_batch_is_retried_per_handle(monkeypatch) -> None:
    w = build_world()
    seed_eyad_solves(w)
    w.sources.failing = {"sara_cf"}
    seen: list[list[str]] = []
    original = w.sources.cf_user_info

    def recording(handles, *, strict=False):
        seen.append(list(handles))
        return original(handles, strict=strict)

    monkeypatch.setattr(w.sources, "cf_user_info", recording)
    imports.run(w.context())
    assert seen[0] == ["eyad_cf", "sara_cf"], "one batch call first"
    assert ["eyad_cf"] in seen and ["sara_cf"] in seen, "then each handle on its own"


# ---------------------------------------------------------------------------
# F4: a failed source is not silent; Last Sync is not moved for that member
# ---------------------------------------------------------------------------
def test_F4_failed_source_keeps_last_sync_and_warns() -> None:
    w = build_world()
    seed_eyad_solves(w)
    w.sources.failing = {"eyad_cf"}
    result = imports.run(w.context())
    assert result.result == WARNING
    assert "Eyad: Codeforces solves, Codeforces rating history failed; Last Sync kept" in result.details
    assert get_date(w.notion.page(w.eyad), S.P_Members.LAST_SYNC) is None


def test_F4_successful_import_moves_last_sync() -> None:
    w = build_world()
    seed_eyad_solves(w)
    assert imports.run(w.context()).result == OK
    assert w.notion.page(w.eyad)["properties"][S.P_Members.LAST_SYNC]["date"] is not None


def _cf_session(status: int, body: dict | None):
    return FakeSession(lambda m, u, k: FakeResponse(status, payload=body))


def test_F4_source_level_or_none_distinguishes_failure_from_no_solves(monkeypatch) -> None:
    monkeypatch.setattr(codeforces, "_limiter", RateLimiter(0))
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    failed = _cf_session(500, None)
    assert codeforces.fetch_solves_or_none(failed, "h", since) is None
    assert codeforces.fetch_solves(failed, "h", since) == []
    empty = _cf_session(200, {"status": "OK", "result": []})
    assert codeforces.fetch_solves_or_none(empty, "h", since) == []


# ---------------------------------------------------------------------------
# F5: Sync Log only for work done or problems
# ---------------------------------------------------------------------------
def test_F5_quiet_ok_job_writes_no_sync_log_row() -> None:
    w = build_world()
    execute(w.context(now=NOW), "contests", lambda ctx: JobResult(job="contests", result=OK, items=0, details="nothing"))
    assert w.notion.rows(S.DB_SYNC_LOG) == []


def test_F5_warning_job_still_writes_a_row() -> None:
    w = build_world()
    execute(w.context(now=NOW), "alerts", lambda ctx: JobResult(job="alerts", result=WARNING, items=0, details="oops"))
    assert len(w.notion.rows(S.DB_SYNC_LOG)) == 1


def test_F5_real_path_second_run_adds_no_sync_log_rows() -> None:
    w = build_world()
    seed_eyad_solves(w)
    config_state_now = NOW
    from hq import config

    config.set_state(w.context().repo, config.STATE_LAST_WEEKLY, "2026-10-10")
    from hq import main as cli

    cli.run_auto(w.context(now=config_state_now))
    before = len(w.notion.rows(S.DB_SYNC_LOG))
    results = cli.run_auto(w.context(now=config_state_now + timedelta(minutes=5)))
    assert [r.job for r in results] == ["onboard", "contests", "alerts", "dashboards", "cleanup"], "imports and daily are not due"
    assert len(w.notion.rows(S.DB_SYNC_LOG)) == before, "a quiet run with nothing to report writes no row"


# ---------------------------------------------------------------------------
# F6: the daily picker moves past Competent topics and empty topics
# ---------------------------------------------------------------------------
def _set_queue(w, topic_key: str, member_id: str, **props) -> None:
    for row in w.notion.rows(S.DB_LEARNING_QUEUE):
        if get_relation(row, S.P_LearningQueue.MEMBER) == [member_id] and get_relation(row, S.P_LearningQueue.TOPIC) == [w.topics[topic_key]]:
            w.notion.update_page(row["id"], props)
            return
    raise AssertionError(topic_key)


def _strings_problem(w) -> None:
    w.sources.problemset.append({"key": "CF-3000A", "name": "Str", "link": "https://x/3000/A", "rating": 900, "tags": ["strings"]})


def test_F6_competent_topic_is_skipped_for_the_next_topic(monkeypatch) -> None:
    w = build_world()
    enable_practice(w)
    w.sources.problemset = [{"key": "CF-1000B", "name": "Bee", "link": "https://x", "rating": 1000, "tags": ["greedy"]}]
    _strings_problem(w)
    onboard.run(w.context())
    _set_queue(w, "dp", w.eyad, **{S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning"), S.P_LearningQueue.PRIORITY: nt.p_select("Very Important"), S.P_LearningQueue.AUTO_TIER: nt.p_select("Competent")})
    _set_queue(w, "strings", w.eyad, **{S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning"), S.P_LearningQueue.PRIORITY: nt.p_select("Important"), S.P_LearningQueue.AUTO_TIER: nt.p_select("Unseen")})
    daily.run(w.context())
    task = _tasks_of(w, w.eyad, "Topic Practice")[0]
    assert get_relation(task, S.P_Tasks.TOPIC) == [w.topics["strings"]]


def test_F6_coach_override_wins_over_auto_tier() -> None:
    w = build_world()
    w.sources.problemset = [{"key": "CF-1000B", "name": "Bee", "link": "https://x", "rating": 1000, "tags": ["greedy"]}]
    _strings_problem(w)
    onboard.run(w.context())
    _set_queue(w, "strings", w.eyad, **{S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning"), S.P_LearningQueue.AUTO_TIER: nt.p_select("Unseen"), S.P_LearningQueue.COACH_OVERRIDE: nt.p_select("Master")})
    daily.run(w.context())
    # Learning rows are the only candidates (change #1). The one Learning row is Master by
    # the coach, so it is skipped and no other topic is used: no practice task today.
    assert _tasks_of(w, w.eyad, "Topic Practice") == []


def test_F6_topic_with_no_problems_creates_no_task_when_learning_rows_exist() -> None:
    w = build_world()
    w.sources.problemset = [{"key": "CF-1000B", "name": "Bee", "link": "https://x", "rating": 1000, "tags": ["greedy"]}]
    onboard.run(w.context())
    _set_queue(w, "strings", w.eyad, **{S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Learning"), S.P_LearningQueue.PRIORITY: nt.p_select("Very Important")})
    daily.run(w.context())  # no strings problems exist; with a Learning row, queued topics are not used (change #1)
    assert _tasks_of(w, w.eyad, "Topic Practice") == []


# ---------------------------------------------------------------------------
# F8: Rating @ Solve for every judge, filled only where empty
# ---------------------------------------------------------------------------
def _atcoder_solve(when: datetime) -> Solve:
    return Solve(
        judge="AtCoder",
        problem_key="AT-abc100_a",
        name="ABC100 A",
        link="https://atcoder.jp/contests/abc100/tasks/abc100_a",
        rating=900,
        difficulty=None,
        tags=(),
        solved_at_utc=when,
        mode="Practice",
        contest_external_id=None,
        attempts=1,
    )


def test_F8_rating_at_solve_for_atcoder_uses_the_ref_rating_at_import_time() -> None:
    w = build_world()
    w.notion.update_page(w.eyad, {S.P_Members.ATCODER_HANDLE: nt.p_text("eyad_at")})
    w.sources.at["eyad_at"] = [_atcoder_solve(NOW - timedelta(days=1))]
    imports.run(w.context())
    problem = [p for p in w.notion.rows(S.DB_PROBLEMS) if get_text(p, S.P_Problems.PROBLEM_KEY) == "AT-abc100_a"][0]
    assert get_number(problem, S.P_Problems.RATING_AT_SOLVE) == 800, "Eyad has no CF rating yet: the L1 band low is the Ref Rating"


def test_F8_rating_at_solve_is_filled_when_it_was_blank() -> None:
    w = build_world()
    w.notion.update_page(w.eyad, {S.P_Members.ATCODER_HANDLE: nt.p_text("eyad_at")})
    w.sources.at["eyad_at"] = [_atcoder_solve(NOW - timedelta(days=1))]
    imports.run(w.context())
    problem = [p for p in w.notion.rows(S.DB_PROBLEMS) if get_text(p, S.P_Problems.PROBLEM_KEY) == "AT-abc100_a"][0]
    w.notion.update_page(problem["id"], {S.P_Problems.RATING_AT_SOLVE: nt.p_number(None)})
    imports.run(w.context(now=NOW + timedelta(minutes=30)))
    problem = [p for p in w.notion.rows(S.DB_PROBLEMS) if get_text(p, S.P_Problems.PROBLEM_KEY) == "AT-abc100_a"][0]
    assert get_number(problem, S.P_Problems.RATING_AT_SOLVE) == 800


def test_F8_cf_solve_uses_rating_history_when_present() -> None:
    w = build_world()
    seed_eyad_solves(w)
    imports.run(w.context())
    problem = [p for p in w.notion.rows(S.DB_PROBLEMS) if get_text(p, S.P_Problems.PROBLEM_KEY) == "CF-1985C"][0]
    assert get_number(problem, S.P_Problems.RATING_AT_SOLVE) == 1150


# ---------------------------------------------------------------------------
# F9: Weekly Summary Who = team name / "Club"
# ---------------------------------------------------------------------------
def test_F9_team_and_club_summary_rows_carry_their_who() -> None:
    from tests.test_jobs_daily_weekly import _weekly_setup

    w = _weekly_setup()
    weekly.run(w.context(now=NOW_SATURDAY))
    team_rows = [r for r in w.notion.rows(S.DB_WEEKLY_SUMMARY) if get_select(r, S.P_WeeklySummary.SCOPE) == "Team"]
    club_rows = [r for r in w.notion.rows(S.DB_WEEKLY_SUMMARY) if get_select(r, S.P_WeeklySummary.SCOPE) == "Club"]
    assert get_select(team_rows[0], S.P_WeeklySummary.WHO) == "Team Alpha"
    assert get_select(club_rows[0], S.P_WeeklySummary.WHO) == "Club"


# ---------------------------------------------------------------------------
# F10: stale contest tasks become Missed; stale goals too
# ---------------------------------------------------------------------------
def _ended_contest_task(w, platform: str, ended_hours_ago: float, *, attended: bool = False) -> str:
    start = NOW - timedelta(hours=ended_hours_ago) - timedelta(minutes=120)
    contest_id = w.notion.seed(
        S.DB_CONTESTS,
        {
            S.P_Contests.TITLE: nt.p_title(f"{platform} old round"),
            S.P_Contests.PLATFORM: nt.p_select(platform),
            S.P_Contests.START: nt.p_date(start),
            S.P_Contests.DURATION_MIN: nt.p_number(120),
            S.P_Contests.EXTERNAL_ID: nt.p_text(f"X-{platform}-{ended_hours_ago}"),
        },
    )
    task_id = w.notion.seed(
        S.DB_TASKS,
        {
            S.P_Tasks.TITLE: nt.p_title("🏆 old"),
            S.P_Tasks.ASSIGNEE: nt.p_relation([w.eyad]),
            S.P_Tasks.TYPE: nt.p_select("Contest"),
            S.P_Tasks.STATUS: nt.p_status("Planned"),
            S.P_Tasks.CONTEST: nt.p_relation([contest_id]),
        },
    )
    if attended:
        w.notion.seed(
            S.DB_CONTEST_ENTRIES,
            {
                S.P_ContestEntries.TITLE: nt.p_title("old · Eyad"),
                S.P_ContestEntries.MEMBER: nt.p_relation([w.eyad]),
                S.P_ContestEntries.CONTEST: nt.p_relation([contest_id]),
                S.P_ContestEntries.STATUS: nt.p_status("Attended"),
            },
        )
    return task_id


def test_F10_codeforces_task_missed_six_hours_after_end() -> None:
    w = build_world()
    task_id = _ended_contest_task(w, "Codeforces", ended_hours_ago=7)
    contests.run(w.context())
    assert get_status(w.notion.page(task_id), S.P_Tasks.STATUS) == "Missed"


def test_F10_task_is_not_missed_before_six_hours() -> None:
    w = build_world()
    task_id = _ended_contest_task(w, "Codeforces", ended_hours_ago=3)
    contests.run(w.context())
    assert get_status(w.notion.page(task_id), S.P_Tasks.STATUS) == "Planned"


def test_F10_leetcode_task_is_left_alone() -> None:
    w = build_world()
    task_id = _ended_contest_task(w, "LeetCode", ended_hours_ago=7)
    contests.run(w.context())
    assert get_status(w.notion.page(task_id), S.P_Tasks.STATUS) == "Planned"


def test_F10_attended_task_is_not_missed() -> None:
    w = build_world()
    task_id = _ended_contest_task(w, "AtCoder", ended_hours_ago=7, attended=True)
    contests.run(w.context())
    assert get_status(w.notion.page(task_id), S.P_Tasks.STATUS) == "Planned"


def test_F10_active_goal_past_deadline_below_target_is_missed() -> None:
    w = build_world()
    late = w.notion.seed(
        S.DB_GOALS,
        {
            S.P_Goals.TITLE: nt.p_title("Late goal"),
            S.P_Goals.SCOPE: nt.p_select("Person"),
            S.P_Goals.METRIC: nt.p_select("Problems Solved"),
            S.P_Goals.MEMBER: nt.p_relation([w.eyad]),
            S.P_Goals.TARGET: nt.p_number(10),
            S.P_Goals.DEADLINE: nt.p_date(date(2026, 10, 1), with_time=False),
            S.P_Goals.STATUS: nt.p_status("Active"),
        },
    )
    done = w.notion.seed(
        S.DB_GOALS,
        {
            S.P_Goals.TITLE: nt.p_title("Done goal"),
            S.P_Goals.SCOPE: nt.p_select("Person"),
            S.P_Goals.METRIC: nt.p_select("Problems Solved"),
            S.P_Goals.MEMBER: nt.p_relation([w.eyad]),
            S.P_Goals.TARGET: nt.p_number(0),
            S.P_Goals.DEADLINE: nt.p_date(date(2026, 10, 1), with_time=False),
            S.P_Goals.STATUS: nt.p_status("Active"),
        },
    )
    seed_eyad_solves(w)
    _chain(w)
    assert get_status(w.notion.page(late), S.P_Goals.STATUS) == "Missed"
    assert get_status(w.notion.page(done), S.P_Goals.STATUS) == "Achieved"


# ---------------------------------------------------------------------------
# F11: the report body goes in at create time; filled in on rerun when empty
# ---------------------------------------------------------------------------
def test_F11_report_body_is_passed_as_children_at_create() -> None:
    from tests.test_jobs_daily_weekly import _weekly_setup

    w = _weekly_setup()
    weekly.run(w.context(now=NOW_SATURDAY))
    report = w.notion.rows(S.DB_WEEKLY_REPORTS)[0]
    assert w.notion.blocks[report["id"]], "the body is there"
    assert [pid for pid, _ in w.notion.appends if pid == report["id"]] == [], "and it was not appended afterwards"


def test_F11_existing_report_without_children_gets_the_body_on_rerun() -> None:
    from tests.test_jobs_daily_weekly import _weekly_setup

    w = _weekly_setup()
    weekly.run(w.context(now=NOW_SATURDAY))
    report = w.notion.rows(S.DB_WEEKLY_REPORTS)[0]
    w.notion.blocks[report["id"]] = []  # the body was lost
    w.notion.appends.clear()
    weekly.run(w.context(now=NOW_SATURDAY))
    assert [pid for pid, _ in w.notion.appends if pid == report["id"]] == [report["id"]]
    assert w.notion.blocks[report["id"]]


# ---------------------------------------------------------------------------
# F12: rating history key includes the date
# ---------------------------------------------------------------------------
def test_F12_rating_history_key_includes_the_cairo_date() -> None:
    w = build_world()
    seed_eyad_solves(w)
    imports.run(w.context())
    key = get_text(w.notion.rows(S.DB_RATING_HISTORY)[0], S.P_RatingHistory.SOURCE_KEY)
    assert key == f"codeforces:Codeforces Round 1985 (Div. 2):2026-10-12:{w.eyad}"


def test_F12_same_contest_name_on_different_dates_gives_two_rows() -> None:
    from hq.models import RatingChange

    w = build_world()
    seed_eyad_solves(w)
    w.sources.cf_hist["eyad_cf"] = [
        RatingChange("Codeforces", "Educational Round", NOW - timedelta(days=30), 1000, 1020, 300),
        RatingChange("Codeforces", "Educational Round", NOW - timedelta(days=5), 1020, 1050, 200),
    ]
    imports.run(w.context())
    assert len(w.notion.rows(S.DB_RATING_HISTORY)) == 2


# ---------------------------------------------------------------------------
# F14: identity fields are fill-only after the first create; times stay synced
# ---------------------------------------------------------------------------
def test_F14_coach_renamed_contest_keeps_its_name_but_start_is_synced() -> None:
    w = build_world()
    w.sources.upcoming = [contest("Codeforces Round 500 (Div. 2)", hours_from_now(30), ext="CF-5000")]
    contests.run(w.context())
    page = w.notion.rows(S.DB_CONTESTS)[0]
    w.notion.update_page(page["id"], {S.P_Contests.TITLE: nt.p_title("Coach's name")})
    w.sources.upcoming = [contest("Codeforces Round 500 (Div. 2) renamed", hours_from_now(31), ext="CF-5000")]
    contests.run(w.context())
    page = w.notion.rows(S.DB_CONTESTS)[0]
    assert get_text(page, S.P_Contests.EXTERNAL_ID) == "CF-5000"
    assert page["properties"][S.P_Contests.TITLE]["title"][0]["text"]["content"] == "Coach's name"
    assert get_date(page, S.P_Contests.START) == NOW + timedelta(hours=31)


# ---------------------------------------------------------------------------
# F15: contest tasks are created whenever missing, not once
# ---------------------------------------------------------------------------
def test_F15_task_is_created_for_a_new_subscriber_after_the_contest_was_ticked() -> None:
    w = build_world()
    w.sources.upcoming = [contest("Codeforces Round 600 (Div. 2)", hours_from_now(20), ext="CF-6000")]
    contests.run(w.context())
    assert get_checkbox(w.notion.rows(S.DB_CONTESTS)[0], S.P_Contests.TASKS_CREATED) is True
    newcomer = w.notion.seed(
        S.DB_MEMBERS,
        {
            S.P_Members.TITLE: nt.p_title("Newcomer"),
            S.P_Members.ACTIVE: nt.p_checkbox(True),
            S.P_Members.CONTEST_SUBSCRIPTIONS: nt.p_multi(["CF Div. 2"]),
        },
    )
    contests.run(w.context())
    assert len([t for t in w.notion.rows(S.DB_TASKS) if get_relation(t, S.P_Tasks.ASSIGNEE) == [newcomer]]) == 1


# ---------------------------------------------------------------------------
# F16: upsolve reminder key per member
# ---------------------------------------------------------------------------
def test_F16_upsolve_source_key_is_per_member_and_entry_without_member_is_skipped() -> None:
    w = build_world()
    contest_id = w.notion.seed(
        S.DB_CONTESTS,
        {
            S.P_Contests.TITLE: nt.p_title("Old round"),
            S.P_Contests.START: nt.p_date(NOW - timedelta(days=3)),
            S.P_Contests.DURATION_MIN: nt.p_number(120),
            S.P_Contests.EXTERNAL_ID: nt.p_text("CF-1600"),
        },
    )
    entry = w.notion.seed(
        S.DB_CONTEST_ENTRIES,
        {
            S.P_ContestEntries.TITLE: nt.p_title("Old round · Eyad"),
            S.P_ContestEntries.MEMBER: nt.p_relation([w.eyad]),
            S.P_ContestEntries.CONTEST: nt.p_relation([contest_id]),
            S.P_ContestEntries.STATUS: nt.p_status("Attended"),
        },
    )
    w.notion.seed(
        S.DB_CONTEST_ENTRIES,
        {
            S.P_ContestEntries.TITLE: nt.p_title("Team entry without a member"),
            S.P_ContestEntries.CONTEST: nt.p_relation([contest_id]),
            S.P_ContestEntries.STATUS: nt.p_status("Attended"),
        },
    )
    daily.run(w.context())
    reminders = [t for t in w.notion.rows(S.DB_TASKS) if get_select(t, S.P_Tasks.TYPE) == "Upsolve"]
    assert len(reminders) == 1
    assert get_text(reminders[0], S.P_Tasks.SOURCE_KEY) == f"upsolve:{entry}:{w.eyad}"


# ---------------------------------------------------------------------------
# F17: Last Sync date becomes a Cairo day start, not UTC midnight
# ---------------------------------------------------------------------------
def test_F17_since_uses_cairo_midnight_for_a_date_last_sync() -> None:
    w = build_world()
    w.notion.update_page(w.eyad, {S.P_Members.LAST_SYNC: nt.p_date(date(2026, 10, 15), with_time=False)})
    member = [m for m in Repo(w.notion, w.ids).members() if m.id == w.eyad][0]
    since = imports.since_for(member, NOW, 120)
    expected = datetime(2026, 10, 13, 0, 0, tzinfo=CAIRO_TZ).astimezone(timezone.utc)
    assert since == expected
    assert since != datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# F19: per-run create cap; a capped member keeps its Last Sync and finishes later
# ---------------------------------------------------------------------------
def test_F19_create_cap_limits_creates_and_next_run_finishes() -> None:
    w = build_world()
    seed_eyad_solves(w)
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("IMPORT_MAX_CREATES"), S.P_Config.NUMBER: nt.p_number(2)})
    first = imports.run(w.context())
    assert first.result == WARNING
    assert "create limit reached" in first.details
    assert len(w.notion.rows(S.DB_PROBLEMS)) == 2
    assert get_date(w.notion.page(w.eyad), S.P_Members.LAST_SYNC) is None, "Last Sync waits for the full window"

    config_row = [r for r in w.notion.rows(S.DB_CONFIG) if get_title(r, S.P_Config.TITLE) == "IMPORT_MAX_CREATES"][0]
    w.notion.update_page(config_row["id"], {S.P_Config.NUMBER: nt.p_number(400)})
    second = imports.run(w.context(now=NOW + timedelta(minutes=30)))
    assert len(w.notion.rows(S.DB_PROBLEMS)) == 3
    assert w.notion.page(w.eyad)["properties"][S.P_Members.LAST_SYNC]["date"] is not None
    assert second.result == OK


# ---------------------------------------------------------------------------
# F21: AtCoder reference files fetched once per session, not once per member
# ---------------------------------------------------------------------------
def test_F21_atcoder_reference_files_are_fetched_once_per_session(monkeypatch) -> None:
    monkeypatch.setattr(atcoder, "_limiter", RateLimiter(0))
    counts = {"problems": 0, "models": 0, "contests": 0}

    def handler(method, url, kwargs):
        if "problems.json" in url:
            counts["problems"] += 1
            return FakeResponse(200, payload=[])
        if "problem-models.json" in url:
            counts["models"] += 1
            return FakeResponse(200, payload={})
        if "contests.json" in url:
            counts["contests"] += 1
            return FakeResponse(200, payload=[])
        return FakeResponse(200, payload=[])  # the submissions page: empty history

    session = FakeSession(handler)
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for user in ("alice", "bob", "carol"):
        assert atcoder.fetch_solves_or_none(session, user, since, 0) == []
    assert counts == {"problems": 1, "models": 1, "contests": 1}


# ---------------------------------------------------------------------------
# F25: README points (the fix list asks for these to be stated)
# ---------------------------------------------------------------------------
def test_F25_readme_states_the_required_points() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "last successful sync" in text
    assert "flat" in text and "build/notion-ids.json" in text
    assert "MODE_UPSOLVE" in text and "Points" in text and "Effective Rating" in text
    assert "IMPORT_MAX_CREATES" in text
