"""Job archive (manual monthly archive): CSV backup, trash of old daily rows, SEASON_START, Streak Start.
Offline (FakeNotion). NOW is 15 Oct 2026, so the current Cairo month starts 1 Oct 2026 and the default
month is 2026-09."""

from __future__ import annotations

import csv
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hq import main
from hq import notion as nt
from hq import schema as S
from hq.config import load
from hq.jobs import ERROR, OK, archive
from hq.jobs.archive import ArchiveOptions, cell
from hq.repo import get_date, get_title
from tests.fakes import NOW, build_world

CUTOFF = date(2026, 10, 1)
FILES = ("daily_log.csv", "tasks.csv", "weekly_summary.csv", "sync_log.csv")


def _run(w, out: Path, *, dry_run: bool = False, phase: str = "all", confirm: str | None = "ARCHIVE", month: str | None = None, now=NOW):
    ctx = w.context(dry_run=dry_run, now=now)
    ctx.archive = ArchiveOptions(month=month, out=out, phase=phase, confirm=confirm)
    return archive.run(ctx)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _config(w, key: str, value: int) -> None:
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title(key), S.P_Config.NUMBER: nt.p_number(value)})


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


def _task(w, title: str, status: str, *, due: date | None = None, created: date | None = None) -> str:
    props = {
        S.P_Tasks.TITLE: nt.p_title(title),
        S.P_Tasks.STATUS: nt.p_status(status),
        S.P_Tasks.ASSIGNEE: nt.p_relation([w.eyad]),
        S.P_Tasks.WHO: nt.p_select("Eyad"),
    }
    if due is not None:
        props[S.P_Tasks.DUE] = nt.p_date(due, with_time=False)
    if created is not None:
        props[S.P_Tasks.CREATED] = nt.p_date(created, with_time=False)
    return w.notion.seed(S.DB_TASKS, props)


def _weekly(w, week: date) -> str:
    return w.notion.seed(
        S.DB_WEEKLY_SUMMARY,
        {
            S.P_WeeklySummary.TITLE: nt.p_title(f"Week {week}"),
            S.P_WeeklySummary.WEEK_START: nt.p_date(week, with_time=False),
            S.P_WeeklySummary.MEMBER: nt.p_relation([w.eyad]),
            S.P_WeeklySummary.WHO: nt.p_select("Eyad"),
        },
    )


def _sync(w, started: datetime) -> str:
    return w.notion.seed(
        S.DB_SYNC_LOG,
        {S.P_SyncLog.TITLE: nt.p_title(f"import {started.date()}"), S.P_SyncLog.STARTED: nt.p_date(started), S.P_SyncLog.JOB: nt.p_select("import")},
    )


def _progress_rows(w) -> dict[str, str]:
    """One row of every table the archive job must never trash or write."""
    return {
        "problem": w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("Two Sum"), S.P_Problems.MEMBER: nt.p_relation([w.eyad])}),
        "queue": w.notion.seed(S.DB_LEARNING_QUEUE, {S.P_LearningQueue.TITLE: nt.p_title("Greedy"), S.P_LearningQueue.MEMBER: nt.p_relation([w.eyad])}),
        "goal": w.notion.seed(S.DB_GOALS, {S.P_Goals.TITLE: nt.p_title("Goal"), S.P_Goals.MEMBER: nt.p_relation([w.eyad])}),
        "achievement": w.notion.seed(S.DB_ACHIEVEMENTS, {S.P_Achievements.TITLE: nt.p_title("First"), S.P_Achievements.MEMBER: nt.p_relation([w.eyad])}),
        "rating": w.notion.seed(S.DB_RATING_HISTORY, {S.P_RatingHistory.TITLE: nt.p_title("1100 to 1150"), S.P_RatingHistory.MEMBER: nt.p_relation([w.eyad])}),
        "entry": w.notion.seed(S.DB_CONTEST_ENTRIES, {S.P_ContestEntries.TITLE: nt.p_title("CF-1985"), S.P_ContestEntries.MEMBER: nt.p_relation([w.eyad])}),
    }


@pytest.fixture
def world():
    return build_world()


def test_default_month_is_the_month_before_the_current_cairo_month(world) -> None:
    label, cutoff, _out = archive._plan(world.context(), ArchiveOptions())
    assert (label, cutoff) == ("2026-09", CUTOFF)
    later = world.context(now=datetime(2027, 1, 5, 12, tzinfo=timezone.utc))
    assert archive._plan(later, ArchiveOptions())[:2] == ("2026-12", date(2027, 1, 1)), "January archives December"


@pytest.mark.parametrize("month", ["2026-10", "2026-11", "2026-13", "26-09", "soon"])
def test_month_must_be_a_past_month(world, tmp_path, month) -> None:
    result = _run(world, tmp_path / "x", month=month)
    assert result.result == ERROR
    assert world.notion.trashed == []
    assert not (tmp_path / "x").exists()


def test_dry_run_writes_nothing(world, tmp_path) -> None:
    _daily(world, date(2026, 9, 3))
    _task(world, "old done", "Done", due=date(2026, 9, 3))
    world.notion.reset_writes()
    out = tmp_path / "out"
    result = _run(world, out, dry_run=True)
    assert result.result == OK
    assert not out.exists(), "no CSV in a dry run"
    assert world.notion.trashed == []
    assert world.notion.write_total() == 0
    assert world.notion.rows(S.DB_CONFIG) == [], "no SEASON_START row is written"
    assert "trashed: daily_log 1" in result.details, "the dry run reports what it would do"


@pytest.mark.parametrize("confirm", [None, "", "archive", "YES"])
def test_real_run_refuses_without_confirm_archive(world, tmp_path, confirm) -> None:
    daily = _daily(world, date(2026, 9, 3))
    world.notion.reset_writes()
    result = _run(world, tmp_path / "out", confirm=confirm)
    assert result.result == ERROR
    assert "confirm = ARCHIVE" in result.details
    assert world.notion.trashed == []
    assert not (tmp_path / "out").exists()
    assert daily in [r["id"] for r in world.notion.rows(S.DB_DAILY_LOG)]
    assert world.notion.write_total() == 0


def test_export_precedes_trash(world, tmp_path) -> None:
    _daily(world, date(2026, 9, 3))
    _task(world, "old done", "Done", due=date(2026, 9, 3))
    out = tmp_path / "out"
    seen: list[bool] = []
    original = world.notion.trash_page

    def checked(page_id: str):
        seen.append(all((out / name).is_file() for name in FILES))
        return original(page_id)

    world.notion.trash_page = checked
    result = _run(world, out)
    assert result.result == OK
    assert len(seen) == 2 and all(seen), "every CSV file existed before each trash call"


def test_export_failure_trashes_nothing(world, tmp_path, monkeypatch) -> None:
    daily = _daily(world, date(2026, 9, 3))

    def broken(path, header, rows):
        raise OSError("disk full (injected)")

    monkeypatch.setattr(archive, "_write_csv", broken)
    result = _run(world, tmp_path / "out")
    assert result.result == ERROR
    assert "nothing trashed" in result.details
    assert world.notion.trashed == []
    assert daily in [r["id"] for r in world.notion.rows(S.DB_DAILY_LOG)]


def test_scope_csv_and_progress_tables(world, tmp_path) -> None:
    old_day = _daily(world, date(2026, 9, 3))
    april_day = _daily(world, date(2026, 4, 1))
    current_day = _daily(world, date(2026, 10, 1))
    recent_day = _daily(world, date(2026, 10, 14))

    task_done = _task(world, "due last month, done", "Done", due=date(2026, 9, 20))
    task_missed = _task(world, "missed", "Missed", due=date(2026, 8, 2))
    task_skipped = _task(world, "skipped", "Skipped", due=date(2026, 9, 30))
    task_attended = _task(world, "contest attended", "Attended", due=date(2026, 9, 9))
    task_created = _task(world, "no due, created last month, done", "Done", created=date(2026, 9, 25))
    keep_open = _task(world, "open", "To Do", due=date(2026, 9, 3))
    keep_planned = _task(world, "planned old", "Planned", due=date(2026, 9, 3))
    keep_this_month = _task(world, "done this month", "Done", due=date(2026, 10, 3))
    keep_no_status = _task(world, "no status", "", due=date(2026, 9, 3))

    trash_week = _weekly(world, date(2026, 9, 27))
    keep_week = _weekly(world, date(2026, 10, 4))

    trash_sync = _sync(world, NOW - timedelta(days=10))
    keep_sync = _sync(world, NOW - timedelta(days=2))

    progress = _progress_rows(world)
    world.notion.reset_writes()

    out = tmp_path / "2026-09"
    result = _run(world, out)
    assert result.result == OK, result.details

    expected_trashed = {
        old_day,
        april_day,
        task_done,
        task_missed,
        task_skipped,
        task_attended,
        task_created,
        trash_week,
        trash_sync,
    }
    assert set(world.notion.trashed) == expected_trashed
    for keep in (current_day, recent_day, keep_open, keep_planned, keep_this_month, keep_no_status, keep_week, keep_sync):
        assert keep not in world.notion.trashed

    # Progress rows are still there, and nothing was written to them or to Problems.
    for page_id in progress.values():
        assert world.notion.page(page_id) is not None
    updated_ids = {page_id for page_id, _props in world.notion.updates}
    assert not updated_ids & set(progress.values())

    # CSV: BOM, Notion property names, Page ID first, relations and selects as text, ISO dates.
    for name in FILES:
        assert (out / name).read_bytes().startswith(b"\xef\xbb\xbf"), name
    daily_rows = _rows(out / "daily_log.csv")
    assert {r["Page ID"] for r in daily_rows} == {old_day, april_day}
    row = next(r for r in daily_rows if r["Page ID"] == old_day)
    assert row["Date"] == "2026-09-03"
    assert row["Member"] == world.eyad
    assert row["Who"] == "Eyad"
    assert row["Name"] == "Day 2026-09-03"
    assert list(daily_rows[0].keys())[0] == "Page ID"

    tasks = _rows(out / "tasks.csv")
    assert {r["Page ID"] for r in tasks} == {task_done, task_missed, task_skipped, task_attended, task_created}
    created_row = next(r for r in tasks if r["Page ID"] == task_created)
    assert created_row["Created"] == "2026-09-25"
    assert created_row["Status"] == "Done"
    assert [r["Week Start"] for r in _rows(out / "weekly_summary.csv")] == ["2026-09-27"]
    assert _rows(out / "sync_log.csv")[0]["Started"].startswith("2026-10-05")

    # Config SEASON_START and Members Streak Start.
    season = next(r for r in world.notion.rows(S.DB_CONFIG) if get_title(r, S.P_Config.TITLE) == "SEASON_START")
    assert get_date(season, S.P_Config.DATE) == CUTOFF
    assert get_date(world.notion.page(world.eyad), S.P_Members.STREAK_START) == CUTOFF
    assert get_date(world.notion.page(world.sara), S.P_Members.STREAK_START) == CUTOFF
    assert get_date(world.notion.page(world.old), S.P_Members.STREAK_START) is None, "inactive members are not changed"
    assert "capped: no" in result.details


def test_rerun_is_idempotent(world, tmp_path) -> None:
    _daily(world, date(2026, 9, 3))
    _task(world, "old done", "Done", due=date(2026, 9, 3))
    out = tmp_path / "out"
    _run(world, out)
    first = {name: _rows(out / name) for name in FILES}
    second = _run(world, out)
    assert second.result == OK
    assert len(world.notion.trashed) == 2, "the second run trashes nothing new"
    assert "trashed: daily_log 0, tasks 0, weekly_summary 0, sync_log 0 (total 0)" in second.details
    for name in FILES:
        assert _rows(out / name) == first[name], name


def test_rerun_refreshes_changed_rows_and_keeps_rows_no_longer_in_notion(world, tmp_path) -> None:
    kept = _daily(world, date(2026, 9, 3))
    gone = _daily(world, date(2026, 9, 4))
    out = tmp_path / "out"
    _run(world, out, phase="export")
    # Edited after the first export: the backup holds the latest text, still one row per page.
    world.notion.page(kept)["properties"][S.P_DailyLog.PROBLEMS] = nt.p_number(9)
    # Removed from Notion by hand: the backup keeps it.
    world.notion.tables[world.ids[S.DB_DAILY_LOG]] = [r for r in world.notion.rows(S.DB_DAILY_LOG) if r["id"] != gone]
    _run(world, out, phase="export")
    rows = {r["Page ID"]: r for r in _rows(out / "daily_log.csv")}
    assert set(rows) == {kept, gone}
    assert rows[kept]["Problems"] == "9"


def test_export_then_trash_phases(world, tmp_path) -> None:
    old = _daily(world, date(2026, 9, 3))
    out = tmp_path / "out"
    result = _run(world, out, phase="export")
    assert result.result == OK
    assert world.notion.trashed == [], "the export phase trashes nothing"
    assert (out / "daily_log.csv").is_file()
    trash = _run(world, out, phase="trash")
    assert trash.result == OK
    assert world.notion.trashed == [old]


def test_trash_phase_trashes_only_rows_in_the_csv(world, tmp_path) -> None:
    backed = _daily(world, date(2026, 9, 3))
    unbacked = _daily(world, date(2026, 9, 4))
    out = tmp_path / "out"
    _run(world, out, phase="export")
    # Simulate a row that is not in the committed CSV: drop it from the file.
    csv_path = out / "daily_log.csv"
    kept = [r for r in _rows(csv_path) if r["Page ID"] != unbacked]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(kept[0].keys()))
        writer.writeheader()
        writer.writerows(kept)
    result = _run(world, out, phase="trash")
    assert world.notion.trashed == [backed]
    assert "not trashed because not in the CSV backup: 1" in result.details
    assert unbacked in [r["id"] for r in world.notion.rows(S.DB_DAILY_LOG)]


def test_trash_phase_without_csv_trashes_nothing(world, tmp_path) -> None:
    _daily(world, date(2026, 9, 3))
    result = _run(world, tmp_path / "missing", phase="trash")
    assert world.notion.trashed == []
    assert "not trashed because not in the CSV backup: 1" in result.details


def test_capped_run_continues_over_several_runs(world, tmp_path) -> None:
    _config(world, "ARCHIVE_PER_RUN", 2)
    ids = [_daily(world, date(2026, 9, d)) for d in (1, 2, 3)]
    out = tmp_path / "out"
    first = _run(world, out)
    assert first.result == OK
    assert len(world.notion.trashed) == 2
    assert "capped: yes (ARCHIVE_PER_RUN 2)" in first.details
    second = _run(world, out)
    assert second.result == OK
    assert len(world.notion.trashed) == 3
    assert "capped: no" in second.details
    assert set(world.notion.trashed) == set(ids)
    assert len(_rows(out / "daily_log.csv")) == 3, "every row is in the backup"


def test_season_start_is_set_once_and_rewritten_not_duplicated(world, tmp_path) -> None:
    _run(world, tmp_path / "out")
    _run(world, tmp_path / "out")
    seasons = [r for r in world.notion.rows(S.DB_CONFIG) if get_title(r, S.P_Config.TITLE) == "SEASON_START"]
    assert len(seasons) == 1
    assert get_date(seasons[0], S.P_Config.DATE) == CUTOFF
    assert load(world.context().repo)["SEASON_START"] == CUTOFF


def test_cell_formats() -> None:
    assert cell(nt.p_title("Hi")) == "Hi"
    assert cell(nt.p_text("a, b")) == "a, b"
    assert cell(nt.p_number(None)) == ""
    assert cell(nt.p_number(3)) == "3"
    assert cell(nt.p_select("Eyad")) == "Eyad"
    assert cell(nt.p_select(None)) == ""
    assert cell(nt.p_status("Done")) == "Done"
    assert cell(nt.p_multi(["dp", "greedy"])) == "dp, greedy"
    assert cell(nt.p_relation(["a1", "b2"])) == "a1, b2"
    assert cell(nt.p_date(date(2026, 9, 3), with_time=False)) == "2026-09-03"
    assert cell(nt.p_date(None)) == ""
    assert cell({"type": "checkbox", "checkbox": True}) == "true"
    assert cell({"type": "formula", "formula": {"type": "number", "number": 4}}) == "4"
    assert cell({"type": "created_time", "created_time": "2026-09-25T10:00:00.000Z"}) == "2026-09-25T10:00:00.000Z"


def test_archive_is_manual_only() -> None:
    assert main.JOBS["archive"] is archive.run
    assert "archive" in main.CHOICES
    assert "archive" not in inspect.getsource(main.run_auto), "archive is never part of the scheduled auto run"


def test_workflow_orders_export_commit_and_trash_and_gates_on_confirm() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "hq.yml").read_text(encoding="utf-8")
    assert "archive]" in text, "archive is a choice of the manual job"
    assert "contents: write" in text, "the workflow commits the CSV backup"
    assert "confirm:" in text and "required: true" in text
    assert "dry_run:" in text and "default: true" in text
    assert "cron" in text and "archive" not in text.split("cron:")[1].split("workflow_dispatch")[0], "never scheduled"
    export_at = text.index("--phase export")
    commit_at = text.index("git commit")
    trash_at = text.index("--phase trash")
    assert export_at < commit_at < trash_at, "CSV export, then commit, then trash"
    assert "git push" in text[commit_at:trash_at], "the backup is pushed before anything is trashed"
    assert "set -euo pipefail" in text
