"""Job archive, FULL RESET mode (``--full``): every table to CSV, counts verified, then trash the progress data.
Offline (FakeNotion). NOW is 15 Oct 2026 (Cairo: the same day), so the reset date is 2026-10-15."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from hq import main
from hq import notion as nt
from hq import schema as S
from hq.jobs import ERROR, OK, archive
from hq.jobs.archive import ArchiveOptions
from hq.repo import get_date, get_status, get_title
from hq.timeutil import cairo_date
from tests.fakes import NOW, build_world

RESET_DAY = date(2026, 10, 15)
EXPORT_FILES = (
    "problems.csv",
    "daily_log.csv",
    "weekly_summary.csv",
    "tasks.csv",
    "sync_log.csv",
    "contest_entries.csv",
    "rating_history.csv",
    "learning_queue.csv",
    "goals.csv",
    "achievements.csv",
    "weekly_reports.csv",
    "members.csv",
    "teams.csv",
)


def _run(w, out: Path, *, dry_run: bool = False, phase: str = "all", confirm: str | None = "RESET", now=NOW):
    ctx = w.context(dry_run=dry_run, now=now)
    ctx.archive = ArchiveOptions(out=out, phase=phase, confirm=confirm, full=True)
    return archive.run(ctx)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _config(w, key: str, value: int) -> None:
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title(key), S.P_Config.NUMBER: nt.p_number(value)})


def _task(w, title: str, status: str | None) -> str:
    return w.notion.seed(
        S.DB_TASKS,
        {
            S.P_Tasks.TITLE: nt.p_title(title),
            S.P_Tasks.STATUS: nt.p_status(status),
            S.P_Tasks.ASSIGNEE: nt.p_relation([w.eyad]),
            S.P_Tasks.WHO: nt.p_select("Eyad"),
            S.P_Tasks.DUE: nt.p_date(date(2026, 9, 3), with_time=False),
        },
    )


def _daily(w, day: date) -> str:
    return w.notion.seed(
        S.DB_DAILY_LOG,
        {
            S.P_DailyLog.TITLE: nt.p_title(f"Day {day}"),
            S.P_DailyLog.DATE: nt.p_date(day, with_time=False),
            S.P_DailyLog.MEMBER: nt.p_relation([w.eyad]),
            S.P_DailyLog.WHO: nt.p_select("Eyad"),
            S.P_DailyLog.PROBLEMS: nt.p_number(2),
        },
    )


def _queue(w, title: str, status: str) -> str:
    return w.notion.seed(
        S.DB_LEARNING_QUEUE,
        {
            S.P_LearningQueue.TITLE: nt.p_title(title),
            S.P_LearningQueue.MEMBER: nt.p_relation([w.eyad]),
            S.P_LearningQueue.QUEUE_STATUS: nt.p_status(status),
        },
    )


def _seed_club(w) -> dict[str, str]:
    """One row of every table a full reset touches, plus the tables it must leave alone."""
    ids = {
        "problem": w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("Two Sum"), S.P_Problems.MEMBER: nt.p_relation([w.eyad])}),
        "daily": _daily(w, date(2026, 10, 3)),
        "weekly": w.notion.seed(
            S.DB_WEEKLY_SUMMARY,
            {S.P_WeeklySummary.TITLE: nt.p_title("Week"), S.P_WeeklySummary.WEEK_START: nt.p_date(date(2026, 10, 5), with_time=False), S.P_WeeklySummary.MEMBER: nt.p_relation([w.eyad])},
        ),
        "sync": w.notion.seed(S.DB_SYNC_LOG, {S.P_SyncLog.TITLE: nt.p_title("import"), S.P_SyncLog.STARTED: nt.p_date(NOW - timedelta(days=30))}),
        "entry": w.notion.seed(S.DB_CONTEST_ENTRIES, {S.P_ContestEntries.TITLE: nt.p_title("CF-1985"), S.P_ContestEntries.MEMBER: nt.p_relation([w.eyad])}),
        "rating": w.notion.seed(S.DB_RATING_HISTORY, {S.P_RatingHistory.TITLE: nt.p_title("1100 to 1150"), S.P_RatingHistory.MEMBER: nt.p_relation([w.eyad])}),
        "achievement": w.notion.seed(S.DB_ACHIEVEMENTS, {S.P_Achievements.TITLE: nt.p_title("First"), S.P_Achievements.MEMBER: nt.p_relation([w.eyad])}),
        "report": w.notion.seed(S.DB_WEEKLY_REPORTS, {S.P_WeeklyReports.TITLE: nt.p_title("Report"), S.P_WeeklyReports.WEEK_START: nt.p_date(date(2026, 10, 5), with_time=False)}),
        "goal": w.notion.seed(S.DB_GOALS, {S.P_Goals.TITLE: nt.p_title("Goal"), S.P_Goals.MEMBER: nt.p_relation([w.eyad])}),
        "contest": w.notion.seed(S.DB_CONTESTS, {S.P_Contests.TITLE: nt.p_title("CF Round")}),
        "resource": w.notion.seed(S.DB_RESOURCES, {S.P_Resources.TITLE: nt.p_title("Book")}),
        "queue_mastered": _queue(w, "Greedy", "Mastered"),
        "queue_practicing": _queue(w, "DP", "Practicing"),
        "task_done": _task(w, "done", "Done"),
        "task_skipped": _task(w, "skipped", "Skipped"),
        "task_missed": _task(w, "missed", "Missed"),
        "task_attended": _task(w, "attended", "Attended"),
        "task_upsolved": _task(w, "upsolved", "Upsolved"),
        "task_virtual": _task(w, "virtual", "Virtual Done"),
        "task_todo": _task(w, "todo", "To Do"),
        "task_planned": _task(w, "planned", "Planned"),
        "task_progress": _task(w, "in progress", "In Progress"),
        "task_nostatus": _task(w, "no status", None),
    }
    return ids


TRASHED_KEYS = {
    "problem",
    "daily",
    "weekly",
    "sync",
    "entry",
    "rating",
    "achievement",
    "report",
    "task_done",
    "task_skipped",
    "task_missed",
    "task_attended",
    "task_upsolved",
    "task_virtual",
}
KEPT_KEYS = {"queue_mastered", "queue_practicing", "goal", "contest", "resource", "task_todo", "task_planned", "task_progress", "task_nostatus"}


@pytest.fixture
def world():
    return build_world()


def test_full_export_covers_every_table(world, tmp_path) -> None:
    ids = _seed_club(world)
    out = tmp_path / "reset"
    result = _run(world, out, phase="export")
    assert result.result == OK, result.details
    assert world.notion.trashed == [], "the export phase trashes nothing"
    for name in EXPORT_FILES:
        assert (out / name).is_file(), name
    assert not (out / "config.csv").exists() and not (out / "topics.csv").exists()
    assert not (out / "levels.csv").exists() and not (out / "contests.csv").exists()
    assert not (out / "resources.csv").exists() and not (out / "code_templates.csv").exists()

    tasks = {r["Page ID"]: r for r in _rows(out / "tasks.csv")}
    assert set(tasks) == {v for k, v in ids.items() if k.startswith("task_")}, "every status is exported"
    members = {r["Page ID"]: r for r in _rows(out / "members.csv")}
    assert world.eyad in members and world.old in members, "inactive members are exported too"
    assert "Active" in members[world.eyad] and "Name" in members[world.eyad]
    assert {r["Page ID"] for r in _rows(out / "teams.csv")} == {world.team}
    assert {r["Page ID"] for r in _rows(out / "learning_queue.csv")} == {ids["queue_mastered"], ids["queue_practicing"]}


def test_full_dry_run_writes_nothing(world, tmp_path) -> None:
    ids = _seed_club(world)
    world.notion.reset_writes()
    out = tmp_path / "reset"
    result = _run(world, out, dry_run=True, confirm=None)
    assert result.result == OK, result.details
    assert not out.exists(), "no CSV in a dry run"
    assert world.notion.trashed == []
    assert world.notion.write_total() == 0
    assert world.notion.rows(S.DB_CONFIG) == [], "no SEASON_START row is written"
    assert "count check: skipped in a dry run" in result.details
    assert "trashed: " in result.details and "Problems 1" in result.details and "Daily Log 1" in result.details
    assert ids["problem"] in [r["id"] for r in world.notion.rows(S.DB_PROBLEMS)]


@pytest.mark.parametrize("confirm", [None, "", "ARCHIVE", "reset", "YES"])
def test_full_real_run_needs_confirm_reset(world, tmp_path, confirm) -> None:
    ids = _seed_club(world)
    world.notion.reset_writes()
    out = tmp_path / "reset"
    result = _run(world, out, confirm=confirm)
    assert result.result == ERROR
    assert "confirm = RESET" in result.details or "full_reset" in result.details
    assert world.notion.trashed == []
    assert not out.exists()
    assert world.notion.write_total() == 0
    assert ids["problem"] in [r["id"] for r in world.notion.rows(S.DB_PROBLEMS)]


def test_full_trash_scope(world, tmp_path) -> None:
    ids = _seed_club(world)
    _config(world, "ARCHIVE_PER_RUN", 500)
    out = tmp_path / "reset"
    result = _run(world, out)
    assert result.result == OK, result.details

    expected = {ids[k] for k in TRASHED_KEYS}
    assert set(world.notion.trashed) == expected
    for key in KEPT_KEYS:
        assert ids[key] not in world.notion.trashed, key
    assert "capped: no" in result.details
    assert "tasks kept (open, no status): 4" in result.details

    # Teams, Members, Learning Queue rows, Goals, Contests, Resources, Config, Topics, Levels: never trashed.
    for page_id in (world.team, world.eyad, world.sara, world.old, world.levels["L1"], world.topics["greedy"], ids["contest"], ids["resource"]):
        assert page_id not in world.notion.trashed
    assert world.notion.page(world.team) is not None


def test_counts_are_verified_before_any_trash(world, tmp_path) -> None:
    ids = _seed_club(world)
    out = tmp_path / "reset"
    _run(world, out, phase="export")
    csv_path = out / "problems.csv"
    kept = [r for r in _rows(csv_path) if r["Page ID"] != ids["problem"]]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(kept[0].keys()) if kept else ["Page ID"])
        writer.writeheader()
        writer.writerows(kept)
    result = _run(world, out, phase="trash")
    assert result.result == ERROR
    assert "nothing trashed or changed" in result.details
    assert "problems.csv: 1 live Notion row(s) not in the CSV" in result.details
    assert world.notion.trashed == []
    assert world.notion.rows(S.DB_DAILY_LOG), "no progress row was trashed"


def test_trash_phase_refuses_when_a_csv_is_missing(world, tmp_path) -> None:
    _seed_club(world)
    result = _run(world, tmp_path / "missing", phase="trash")
    assert result.result == ERROR
    assert "the CSV does not exist" in result.details
    assert world.notion.trashed == []


def test_export_failure_trashes_nothing(world, tmp_path, monkeypatch) -> None:
    _seed_club(world)

    def broken(path, header, rows):
        raise OSError("disk full (injected)")

    monkeypatch.setattr(archive, "_write_csv", broken)
    result = _run(world, tmp_path / "reset")
    assert result.result == ERROR
    assert "nothing trashed" in result.details
    assert world.notion.trashed == []


def test_learning_queue_mastered_goes_back_to_practicing(world, tmp_path) -> None:
    ids = _seed_club(world)
    result = _run(world, tmp_path / "reset")
    assert result.result == OK, result.details
    assert "Learning Queue Mastered -> Practicing: 1 row(s)" in result.details
    queue = {r["id"]: r for r in world.notion.rows(S.DB_LEARNING_QUEUE)}
    assert get_status(queue[ids["queue_mastered"]], S.P_LearningQueue.QUEUE_STATUS) == "Practicing"
    assert get_status(queue[ids["queue_practicing"]], S.P_LearningQueue.QUEUE_STATUS) == "Practicing"
    assert ids["queue_mastered"] not in world.notion.trashed


def test_members_last_sync_streak_start_and_season_start(world, tmp_path) -> None:
    ids = _seed_club(world)
    result = _run(world, tmp_path / "reset")
    assert result.result == OK, result.details
    assert "Last Sync and Streak Start set for 2 active member(s)" in result.details
    for member in (world.eyad, world.sara):
        page = world.notion.page(member)
        assert get_date(page, S.P_Members.LAST_SYNC) == RESET_DAY
        assert get_date(page, S.P_Members.STREAK_START) == RESET_DAY
        assert get_date(page, S.P_Members.SEASON_START) is None, "member Season Start is not changed (the monthly archive does not change it either)"
    old = world.notion.page(world.old)
    assert get_date(old, S.P_Members.LAST_SYNC) is None and get_date(old, S.P_Members.STREAK_START) is None
    season = next(r for r in world.notion.rows(S.DB_CONFIG) if get_title(r, S.P_Config.TITLE) == "SEASON_START")
    assert get_date(season, S.P_Config.DATE) == RESET_DAY
    assert cairo_date(NOW) == RESET_DAY
    assert ids["task_todo"] in [r["id"] for r in world.notion.rows(S.DB_TASKS)]


def test_full_reset_resumes_across_capped_runs(world, tmp_path) -> None:
    _config(world, "ARCHIVE_PER_RUN", 2)
    days = [_daily(world, date(2026, 9, d)) for d in (1, 2, 3, 4, 5)]
    _queue(world, "Greedy", "Mastered")
    out = tmp_path / "reset"
    first = _run(world, out)
    assert first.result == OK
    assert len(world.notion.trashed) == 2
    assert "capped: yes (ARCHIVE_PER_RUN 2)" in first.details
    assert "Learning Queue Mastered -> Practicing: skipped" in first.details, "the queue reset waits for the last batch"
    second = _run(world, out)
    assert len(world.notion.trashed) == 4
    assert "capped: yes" in second.details
    third = _run(world, out)
    assert third.result == OK
    assert "capped: no" in third.details
    assert "Learning Queue Mastered -> Practicing: 1 row(s)" in third.details
    assert set(world.notion.trashed) >= set(days)
    assert len(_rows(out / "daily_log.csv")) == 5, "the backup keeps every trashed row"


def test_full_rerun_does_not_duplicate_the_backup(world, tmp_path) -> None:
    _seed_club(world)
    out = tmp_path / "reset"
    _run(world, out)
    first = {name: _rows(out / name) for name in EXPORT_FILES}
    second = _run(world, out)
    assert second.result == OK, second.details
    assert len(world.notion.trashed) == len(TRASHED_KEYS), "the second run trashes nothing new"
    for name in EXPORT_FILES:
        assert len(_rows(out / name)) == len(first[name]), name


def test_monthly_archive_still_needs_confirm_archive(world, tmp_path) -> None:
    _seed_club(world)
    ctx = world.context()
    ctx.archive = ArchiveOptions(out=tmp_path / "monthly", phase="all", confirm="RESET", full=False)
    result = archive.run(ctx)
    assert result.result == ERROR
    assert "confirm = ARCHIVE" in result.details
    assert world.notion.trashed == []


def test_full_flag_reaches_the_job() -> None:
    args = main.parse_args(["archive", "--full", "--confirm", "RESET"])
    assert args.full is True and args.confirm == "RESET"
    assert main.parse_args(["archive", "--confirm", "ARCHIVE"]).full is False


def test_workflow_has_full_reset_input_and_order() -> None:
    from pathlib import Path as _Path

    text = (_Path(__file__).resolve().parents[1] / ".github" / "workflows" / "hq.yml").read_text(encoding="utf-8")
    assert "full_reset:" in text and "default: false" in text
    assert 'ARCHIVE_FULL: ${{ inputs.full_reset }}' in text
    full_at = text.index("--full --phase export")
    commit_at = text.index('git commit -m "Full reset: CSV backup of every table"')
    trash_at = text.index("--full --phase trash")
    assert full_at < commit_at < trash_at
    assert 'ARCHIVE_CONFIRM" != "RESET"' in text, "a real full reset needs confirm RESET"
    assert "full_reset" not in text.split("cron:")[1].split("workflow_dispatch")[0]


def _sync(w, title: str, started: datetime) -> str:
    return w.notion.seed(S.DB_SYNC_LOG, {S.P_SyncLog.TITLE: nt.p_title(title), S.P_SyncLog.STARTED: nt.p_date(started)})


def test_sync_row_of_this_run_after_the_export_is_not_missing(world, tmp_path) -> None:
    """The export runs at NOW. Afterwards a Sync Log row with this run's timestamp is created (the run's own row).
    The trash phase verifies and passes, and that row is never trashed."""
    ids = _seed_club(world)
    out = tmp_path / "reset"
    assert _run(world, out, phase="export").result == OK
    own = _sync(world, f"archive {NOW.isoformat()}", NOW)
    result = _run(world, out, phase="trash", now=NOW + timedelta(hours=1))
    assert result.result == OK, result.details
    assert "FAILED" not in result.details
    assert "Sync Log rows started by this reset run or after the export snapshot (not checked, never trashed): 1" in result.details
    assert own not in world.notion.trashed
    assert ids["sync"] in world.notion.trashed, "older Sync Log rows are still trashed"


def test_other_missing_row_still_fails_with_nothing_trashed(world, tmp_path) -> None:
    """Same as above, plus a row that existed before the export and is missing from its CSV: the run stops, nothing trashed."""
    ids = _seed_club(world)
    out = tmp_path / "reset"
    assert _run(world, out, phase="export").result == OK
    own = _sync(world, f"archive {NOW.isoformat()}", NOW)
    missing = _sync(world, "import old", NOW - timedelta(days=2))
    result = _run(world, out, phase="trash", now=NOW + timedelta(hours=1))
    assert result.result == ERROR
    assert "nothing trashed or changed" in result.details
    assert "sync_log.csv: 1 live Notion row(s) not in the CSV" in result.details
    assert world.notion.trashed == []
    assert own not in world.notion.trashed and missing not in world.notion.trashed and ids["sync"] not in world.notion.trashed


def test_run_own_sync_row_is_never_trashed(world, tmp_path) -> None:
    """A Sync Log row started at the export snapshot time is in the CSV, but it is the export run's own row: the trash
    phase must not trash it, while older Sync Log rows are trashed as before."""
    ids = _seed_club(world)
    own = _sync(world, f"archive {NOW.isoformat()}", NOW)
    out = tmp_path / "reset"
    assert _run(world, out, phase="export").result == OK
    result = _run(world, out, phase="trash", now=NOW + timedelta(hours=1))
    assert result.result == OK, result.details
    assert own not in world.notion.trashed
    assert ids["sync"] in world.notion.trashed
    assert "trashed: " in result.details and "capped: no" in result.details


def test_missing_stamp_keeps_the_check_strict_for_older_rows(world, tmp_path) -> None:
    """Without the export stamp (an old folder), a row started before this run is still checked strictly."""
    _seed_club(world)
    out = tmp_path / "reset"
    assert _run(world, out, phase="export").result == OK
    (out / archive.EXPORT_STAMP).unlink()
    _sync(world, f"archive {NOW.isoformat()}", NOW)
    result = _run(world, out, phase="trash", now=NOW + timedelta(hours=1))
    assert result.result == ERROR
    assert "sync_log.csv: 1 live Notion row(s) not in the CSV" in result.details
    assert world.notion.trashed == []


def test_own_row_at_the_stamp_minute_is_exempt_at_minute_precision(world, tmp_path) -> None:
    """The export stamp keeps seconds (run start 10:30:45), but Notion stores Started to the minute (10:30:00).
    The reset run's own row, stored at the stamp's minute, is earlier than the stamp by seconds: it must still be
    exempt, and the run must never trash it."""
    ids = _seed_club(world)
    out = tmp_path / "reset"
    stamp_time = NOW.replace(second=45, microsecond=0)
    assert _run(world, out, phase="export", now=stamp_time).result == OK
    assert (out / archive.EXPORT_STAMP).read_text(encoding="utf-8").strip() == stamp_time.isoformat()
    own = _sync(world, "archive same minute", NOW.replace(second=0, microsecond=0))
    result = _run(world, out, phase="trash", now=NOW + timedelta(hours=1))
    assert result.result == OK, result.details
    assert "FAILED" not in result.details
    assert own not in world.notion.trashed
    assert ids["sync"] in world.notion.trashed


def test_row_from_an_earlier_minute_still_fails_with_nothing_trashed(world, tmp_path) -> None:
    """A Sync Log row from a clearly earlier run (one minute before the stamp's minute) is not exempt: the check fails
    and nothing is trashed, not even the own row."""
    _seed_club(world)
    out = tmp_path / "reset"
    stamp_time = NOW.replace(second=45, microsecond=0)
    assert _run(world, out, phase="export", now=stamp_time).result == OK
    own = _sync(world, "archive same minute", NOW.replace(second=0, microsecond=0))
    earlier = _sync(world, "earlier run", NOW.replace(second=0, microsecond=0) - timedelta(minutes=1))
    result = _run(world, out, phase="trash", now=NOW + timedelta(hours=1))
    assert result.result == ERROR
    assert "nothing trashed or changed" in result.details
    assert "sync_log.csv: 1 live Notion row(s) not in the CSV" in result.details
    assert world.notion.trashed == []
    assert own not in world.notion.trashed and earlier not in world.notion.trashed


def test_trash_phase_never_trashes_the_run_own_row(world, tmp_path) -> None:
    """With the stamp at a minute with seconds, the own row (same minute) and the rows after it are exempt in the
    trash phase; trashing only touches the older rows that were backed up."""
    ids = _seed_club(world)
    out = tmp_path / "reset"
    stamp_time = NOW.replace(second=30, microsecond=0)
    assert _run(world, out, phase="export", now=stamp_time).result == OK
    own = _sync(world, "archive same minute", NOW.replace(second=0, microsecond=0))
    later = _sync(world, "later run", NOW.replace(second=59, microsecond=0) + timedelta(minutes=5))
    result = _run(world, out, phase="trash", now=NOW + timedelta(hours=1))
    assert result.result == OK, result.details
    assert own not in world.notion.trashed and later not in world.notion.trashed
    assert ids["sync"] in world.notion.trashed
