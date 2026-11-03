"""Job ``archive``: a MANUAL monthly archive that frees Notion space. It is never scheduled: it runs
only when the owner starts it (GitHub Actions workflow_dispatch, job "archive", or the CLI).

For a month M (default: the calendar month before the current Cairo month; ``--month YYYY-MM``):

* Export: every row in scope goes to ``<out>/<file>.csv`` with ALL its properties (notes, links, key
  idea included). Files: daily_log.csv, tasks.csv, weekly_summary.csv, sync_log.csv. The default ``<out>``
  is ``archive/<M>/`` in the repo root. UTF-8 with BOM. The first column is "Page ID" (added, so reruns
  can tell rows apart); then one column per Notion property, named as in Notion (title included).
  Relations and multi-selects are comma-separated, selects and people are names, dates are ISO strings.
  A rerun updates the rows already in the file (no duplicates) and keeps rows no longer in Notion.
* Trash (Notion's 30-day trash, the same ``Repo.trash`` the cleanup job uses), at most ARCHIVE_PER_RUN pages:
  - Daily Log rows dated before the first day of the current Cairo month;
  - Tasks whose Due (or Created, when there is no Due) is before that day AND whose Status is Done,
    Attended, Skipped or Missed (never an open task);
  - Weekly Summary rows whose Week Start is before that day;
  - Sync Log rows started more than 7 days ago.
* Then, only when nothing failed: Config SEASON_START = the first day of the current Cairo month, and the
  Members "Streak Start" of every active member = the same day.

Nothing else is touched. Problems, Contest Entries, Rating History, Achievements, Learning Queue & Mastery,
Goals, Members totals, Teams, Config (except SEASON_START), Topics, Levels, Contests, Resources and Code
Templates are never trashed.

Phases (``--phase``; the workflow runs them one after another, with a git commit in between):
* ``export``: writes the CSV files and trashes nothing.
* ``trash``: trashes only rows whose Page ID is in the CSV files on disk, then sets the two dates above.
* ``all`` (default, for local runs): export, then trash.

Safety:
* A run that is not a dry run needs ``confirm == "ARCHIVE"``; otherwise it is refused before any write.
* ``--dry-run`` reads Notion and writes nothing: no files, no trash, no Config or Members change.
* An export failure stops the run before any trash. No row is trashed unless its Page ID is in the CSV.
* The cap ARCHIVE_PER_RUN limits trashed pages per run; the result says ``capped: yes|no``.

Not verified against the live Notion API: the trash call (PATCH /pages with in_trash), the Members
"Streak Start" date property (it must be created in Notion by hand), and the Tasks status names above
(taken from the other jobs: "Done" is the complete status, "Attended" is set on contest tasks).

FULL RESET (``--full``, workflow input full_reset): everyone restarts from 0 from the reset date (the current
Cairo day). Same phases, same CSV-first order, same cap ARCHIVE_PER_RUN, same idempotency. A real run needs
``confirm == "RESET"`` (any other confirm value is refused before any write). Output: ``archive/<reset-date>/``.
* Export (every table, all properties, all rows): Problems, Daily Log, Weekly Summary, Tasks (all statuses),
  Sync Log, Contest Entries, Rating History, Learning Queue & Mastery, Goals, Achievements, Weekly Reports,
  Members, Teams. Config, Topics, Levels, Contests, Resources and Code Templates are not exported.
* Verify (before any trash, and again on the trash phase, from the CSV on disk): every live Notion row is in its
  CSV. A file row that is no longer in Notion is allowed: it was backed up before it left Notion (this is what
  a rerun after a capped run sees). A missing file or a missing row stops the run with nothing trashed.
* Trash (capped, only rows whose Page ID is in the CSV): Problems, Daily Log, Weekly Summary, Sync Log, Contest
  Entries, Rating History, Achievements, Weekly Reports, and Tasks whose Status is Done, Skipped, Missed,
  Attended, Upsolved or Virtual Done. Open Tasks (To Do, Planned, In Progress, no status) are kept.
  Never trashed: Learning Queue & Mastery, Goals, Members, Teams, Config, Topics, Levels, Contests, Resources.
* Learning Queue & Mastery: a row with Queue Status Mastered goes back to Practicing (only after every trash
  is done, so a capped run does not reset it early). Its other stored fields are left as they are.
* Members (active only, after the trash, when nothing failed): Last Sync = the reset date (so imports read
  from two days before it and do not re-create old solves), Streak Start = the reset date. Member Season
  Start is not changed (the monthly archive does not change it either). Config SEASON_START = the reset date.
* Not verified live: the Members property names used here, the Tasks status names "Upsolved" and
  "Virtual Done", and whether Last Sync and Streak Start accept writes through the API (the monthly archive
  already writes Streak Start, the import job already writes Last Sync).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from hq import config
from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import ERROR, OK, WARNING, Context, JobResult, as_utc
from hq.repo import TITLE_PROPS, cairo_day_of, get_date, get_status
from hq.schema import ConfigKey as K
from hq.timeutil import cairo_date

log = logging.getLogger("hq.jobs.archive")

JOB = "archive"
CONFIRM_WORD = "ARCHIVE"
PHASES = ("all", "export", "trash")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PAGE_ID_COLUMN = "Page ID"
SYNC_LOG_DAYS = 7
CLOSED_TASK_STATUSES = frozenset({"Done", "Attended", "Skipped", "Missed"})
FULL_RESET_WORD = "RESET"
FULL_TASK_TRASH_STATUSES = frozenset({"Done", "Skipped", "Missed", "Attended", "Upsolved", "Virtual Done"})
ERROR_CHARS = 200
_MONTH = re.compile(r"(\d{4})-(\d{2})")


@dataclass(frozen=True)
class ArchiveOptions:
    """CLI options of the archive job. ``month`` is YYYY-MM; ``out`` a directory; ``phase`` one of PHASES."""

    month: str | None = None
    out: Path | None = None
    phase: str = "all"
    confirm: str | None = None
    full: bool = False  # FULL RESET (see the module docstring); needs confirm == "RESET" for a real run


@dataclass(frozen=True)
class Marks:
    cutoff: date  # first day of the current Cairo month
    now: datetime  # aware UTC


def _utc(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _created_day(row: dict[str, Any]) -> date | None:
    """The Created day of a Tasks row: its Created property (a date, or a created_time), else the page's own created time."""
    day = cairo_day_of(get_date(row, S.P_Tasks.CREATED))
    if day is not None:
        return day
    raw = nt.prop(row, S.P_Tasks.CREATED) or {}
    for value in (raw.get("created_time"), row.get("created_time")):
        if value:
            return cairo_date(_utc(value))
    return None


def _daily_ok(row: dict[str, Any], m: Marks) -> bool:
    day = cairo_day_of(get_date(row, S.P_DailyLog.DATE))
    return day is not None and day < m.cutoff


def _task_ok(row: dict[str, Any], m: Marks) -> bool:
    if get_status(row, S.P_Tasks.STATUS) not in CLOSED_TASK_STATUSES:
        return False
    day = cairo_day_of(get_date(row, S.P_Tasks.DUE)) or _created_day(row)
    return day is not None and day < m.cutoff


def _weekly_ok(row: dict[str, Any], m: Marks) -> bool:
    day = cairo_day_of(get_date(row, S.P_WeeklySummary.WEEK_START))
    return day is not None and day < m.cutoff


def _sync_ok(row: dict[str, Any], m: Marks) -> bool:
    value = get_date(row, S.P_SyncLog.STARTED)
    if value is None:
        return False
    if isinstance(value, datetime):
        return as_utc(value) < m.now - timedelta(days=SYNC_LOG_DAYS)
    return value < cairo_date(m.now) - timedelta(days=SYNC_LOG_DAYS)


@dataclass(frozen=True)
class Table:
    db: str
    file: str
    in_scope: Callable[[dict[str, Any], Marks], bool]


TABLES: tuple[Table, ...] = (
    Table(S.DB_DAILY_LOG, "daily_log.csv", _daily_ok),
    Table(S.DB_TASKS, "tasks.csv", _task_ok),
    Table(S.DB_WEEKLY_SUMMARY, "weekly_summary.csv", _weekly_ok),
    Table(S.DB_SYNC_LOG, "sync_log.csv", _sync_ok),
)


# ---------------------------------------------------------------------------
# CSV cells
# ---------------------------------------------------------------------------
def _formula(raw: dict[str, Any]) -> str:
    kind = raw.get("type")
    if kind == "date":
        return str((raw.get("date") or {}).get("start") or "")
    value = raw.get(kind) if kind else None
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def cell(raw: Any) -> str:
    """One Notion property value as CSV text (the formats are in the module docstring)."""
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        return str(raw)
    if "title" in raw:
        return nt.r_title(raw)
    if "rich_text" in raw:
        return nt.r_text(raw)
    if "number" in raw:
        return "" if raw["number"] is None else str(raw["number"])
    if "select" in raw or "status" in raw:
        option = raw.get("select") or raw.get("status") or {}
        return str(option.get("name") or "")
    if "multi_select" in raw:
        return ", ".join(str(o.get("name") or "") for o in raw["multi_select"] or [])
    if "date" in raw:
        span = raw["date"] or {}
        start = span.get("start") or ""
        end = span.get("end")
        return f"{start}/{end}" if start and end else str(start)
    if "checkbox" in raw:
        return "true" if raw["checkbox"] else "false"
    for key in ("url", "email", "phone_number", "created_time", "last_edited_time"):
        if key in raw:
            return str(raw[key] or "")
    if "relation" in raw:
        return ", ".join(str(r.get("id") or "") for r in raw["relation"] or [])
    if "people" in raw:
        return ", ".join(str(p.get("name") or p.get("id") or "") for p in raw["people"] or [])
    if "created_by" in raw or "last_edited_by" in raw:
        user = raw.get("created_by") or raw.get("last_edited_by") or {}
        return str(user.get("name") or user.get("id") or "")
    if "files" in raw:
        names = []
        for item in raw["files"] or []:
            ext = item.get("external") or item.get("file") or {}
            names.append(str(item.get("name") or ext.get("url") or ""))
        return ", ".join(names)
    if "formula" in raw:
        return _formula(raw["formula"] or {})
    if "unique_id" in raw:
        uid = raw["unique_id"] or {}
        prefix = uid.get("prefix") or ""
        number = uid.get("number")
        return f"{prefix}-{number}" if prefix and number is not None else str(number or "")
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)


def _live_title(db: str, schema: dict[str, Any]) -> str:
    for name, meta in schema.items():
        if isinstance(meta, dict) and meta.get("type") == "title":
            return name
    return TITLE_PROPS.get(db, "")


def _columns(db: str, schema: dict[str, Any], pages: list[dict[str, Any]]) -> tuple[list[str], str]:
    """Notion property names in schema order (the title included), plus any extra name a row carries."""
    live = _live_title(db, schema)
    columns = list(schema)
    if live and live not in columns:
        columns.insert(0, live)
    configured = TITLE_PROPS.get(db)
    hidden = {configured} if configured and configured != live else set()
    for page in pages:
        for key in page.get("properties") or {}:
            if key not in columns and key not in hidden:
                columns.append(key)
    return columns, live


def export_row(page: dict[str, Any], columns: list[str], db: str, live_title: str) -> dict[str, str]:
    props = page.get("properties") or {}
    row = {PAGE_ID_COLUMN: str(page["id"])}
    for column in columns:
        key = TITLE_PROPS.get(db, column) if column == live_title else column
        row[column] = cell(props.get(key))
    return row


def _read_csv(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Existing rows keyed by Page ID (a row without one gets a positional key), and the header."""
    if not path.is_file():
        return {}, []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        rows: dict[str, dict[str, str]] = {}
        for index, row in enumerate(reader):
            key = (row.get(PAGE_ID_COLUMN) or "").strip() or f"#row{index}"
            rows[key] = {k: (v or "") for k, v in row.items() if k is not None}
    return rows, header


def _write_csv(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    """Write through a temporary file, then rename, so a failed write never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def _backed_up_ids(path: Path) -> set[str]:
    rows, _ = _read_csv(path)
    return {key for key in rows if not key.startswith("#row")}


def _export_table(
    repo: Any, table: Table, pages: list[dict[str, Any]], schema: dict[str, Any], out_dir: Path, dry_run: bool
) -> tuple[int, int]:
    """Write one table's CSV: the rows in scope now, merged with the rows the file already holds.
    Returns (rows in the file, rows new to the file)."""
    path = out_dir / table.file
    existing, old_header = _read_csv(path)
    columns, live = _columns(table.db, schema, pages)
    merged = dict(existing)
    new = 0
    for page in pages:
        pid = str(page["id"])
        if pid not in merged:
            new += 1
        merged[pid] = export_row(page, columns, table.db, live)
    header = [PAGE_ID_COLUMN] + [c for c in columns if c != PAGE_ID_COLUMN]
    header += [c for c in old_header if c not in header]
    if dry_run:
        repo.write_log.append(f"export {path}: {len(merged)} row(s), {new} new")
    else:
        _write_csv(path, header, list(merged.values()))
    return len(merged), new


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:ERROR_CHARS]}"


def _plan(ctx: Context, opts: ArchiveOptions) -> tuple[str, date, Path]:
    """(month label, cutoff = first day of the current Cairo month, output directory). Raises ValueError."""
    today = ctx.today
    cutoff = date(today.year, today.month, 1)
    if opts.month is None:
        previous = cutoff - timedelta(days=1)
        year, month = previous.year, previous.month
    else:
        found = _MONTH.fullmatch(opts.month.strip())
        if not found:
            raise ValueError("--month must look like YYYY-MM")
        year, month = int(found.group(1)), int(found.group(2))
        if not 1 <= month <= 12:
            raise ValueError("--month has no such month")
        if date(year, month, 1) >= cutoff:
            raise ValueError(f"{year:04d}-{month:02d} is not before the current Cairo month ({cutoff:%Y-%m}); only past months are archived")
    label = f"{year:04d}-{month:02d}"
    out = Path(opts.out) if opts.out is not None else REPO_ROOT / "archive" / label
    return label, cutoff, out


# ---------------------------------------------------------------------------
# FULL RESET (--full): see the module docstring
# ---------------------------------------------------------------------------
def _every(row: dict[str, Any]) -> bool:
    return True


def _full_task_closed(row: dict[str, Any]) -> bool:
    return get_status(row, S.P_Tasks.STATUS) in FULL_TASK_TRASH_STATUSES


@dataclass(frozen=True)
class FullTable:
    db: str
    file: str
    trash: Callable[[dict[str, Any]], bool] | None  # None: the reset never trashes this table


FULL_TABLES: tuple[FullTable, ...] = (
    FullTable(S.DB_PROBLEMS, "problems.csv", _every),
    FullTable(S.DB_DAILY_LOG, "daily_log.csv", _every),
    FullTable(S.DB_WEEKLY_SUMMARY, "weekly_summary.csv", _every),
    FullTable(S.DB_TASKS, "tasks.csv", _full_task_closed),
    FullTable(S.DB_SYNC_LOG, "sync_log.csv", _every),
    FullTable(S.DB_CONTEST_ENTRIES, "contest_entries.csv", _every),
    FullTable(S.DB_RATING_HISTORY, "rating_history.csv", _every),
    FullTable(S.DB_LEARNING_QUEUE, "learning_queue.csv", None),
    FullTable(S.DB_GOALS, "goals.csv", None),
    FullTable(S.DB_ACHIEVEMENTS, "achievements.csv", _every),
    FullTable(S.DB_WEEKLY_REPORTS, "weekly_reports.csv", _every),
    FullTable(S.DB_MEMBERS, "members.csv", None),
    FullTable(S.DB_TEAMS, "teams.csv", None),
)


def _verify_backup(out_dir: Path, scope: list[tuple[FullTable, list[dict[str, Any]], dict[str, Any]]]) -> tuple[dict[str, tuple[int, int]], list[str], dict[str, set[str]]]:
    """Check the CSV files on disk against the live rows. Returns ({file: (Notion rows, CSV rows)}, problems, {file: backed-up Page IDs}).
    A live row missing from its file is a problem. A file row that is no longer in Notion is not: it was backed up
    before it left Notion (a rerun after a capped trash sees these)."""
    counts: dict[str, tuple[int, int]] = {}
    problems: list[str] = []
    backed: dict[str, set[str]] = {}
    for table, pages, _schema in scope:
        path = out_dir / table.file
        if not path.is_file():
            problems.append(f"{table.file}: the CSV does not exist")
            counts[table.file] = (len(pages), 0)
            backed[table.file] = set()
            continue
        rows, _header = _read_csv(path)
        ids = {key for key in rows if not key.startswith("#row")}
        unkeyed = len(rows) - len(ids)
        live_ids = {str(page["id"]) for page in pages}
        missing = live_ids - ids
        counts[table.file] = (len(pages), len(rows))
        backed[table.file] = ids
        if unkeyed:
            problems.append(f"{table.file}: {unkeyed} row(s) without a Page ID")
        if missing:
            problems.append(f"{table.file}: {len(missing)} live Notion row(s) not in the CSV (Notion {len(pages)}, CSV {len(rows)})")
    return counts, problems, backed


def _run_full(ctx: Context, opts: ArchiveOptions) -> JobResult:
    if not ctx.dry_run and opts.confirm != FULL_RESET_WORD:
        return JobResult(
            job=JOB,
            result=ERROR,
            items=0,
            details="full reset refused: a real full reset needs full_reset = true, confirm = RESET and dry_run = false. Nothing was written or trashed.",
            skipped=True,
        )
    reset_day = ctx.today
    out_dir = Path(opts.out) if opts.out is not None else REPO_ROOT / "archive" / reset_day.isoformat()
    repo = ctx.repo
    try:
        scope = []
        for table in FULL_TABLES:
            schema = repo.data_source_properties(table.db)
            pages = list(repo.rows(table.db))
            scope.append((table, pages, schema))
    except Exception as exc:  # noqa: BLE001 - nothing has been written yet
        log.warning("full reset stopped while reading Notion: %s", _short(exc))
        return JobResult(job=JOB, result=ERROR, items=0, details=f"full reset stopped, nothing written or trashed: {_short(exc)}")

    exported: dict[str, tuple[int, int]] = {}
    if opts.phase in ("all", "export"):
        try:
            for table, pages, schema in scope:
                if not ctx.dry_run:
                    out_dir.mkdir(parents=True, exist_ok=True)
                exported[table.file] = _export_table(repo, table, pages, schema, out_dir, ctx.dry_run)
        except Exception as exc:  # noqa: BLE001 - an export failure means nothing is trashed
            log.warning("full reset export failed, nothing trashed: %s", _short(exc))
            return JobResult(job=JOB, result=ERROR, items=0, details=f"full export failed, nothing trashed: {_short(exc)}")

    counts: dict[str, tuple[int, int]] = {}
    backed: dict[str, set[str]] = {}
    if not ctx.dry_run:
        counts, problems, backed = _verify_backup(out_dir, scope)
        if problems:
            lines = [f"full reset stopped: the CSV backup does not match Notion; nothing trashed or changed. Folder: {out_dir}"]
            lines += [f"FAILED {line}" for line in problems]
            return JobResult(job=JOB, result=ERROR, items=sum(new for _rows, new in exported.values()), details="\n".join(lines))

    trashed: dict[str, int] = {}
    kept_open_tasks = 0
    not_backed_up = 0
    cap = max(0, int(num(ctx.cfg, K.ARCHIVE_PER_RUN)))
    capped = False
    failures: list[str] = []
    mastered_reset = 0
    queue_skipped = False
    set_dates = False
    dated = 0
    if opts.phase in ("all", "trash"):
        candidates = []
        for table, pages, _schema in scope:
            if table.trash is None:
                continue
            eligible = [p for p in pages if table.trash(p)]
            if table.db == S.DB_TASKS:
                kept_open_tasks = len(pages) - len(eligible)
            if ctx.dry_run:
                allowed = eligible
            else:
                allowed = [p for p in eligible if str(p["id"]) in backed.get(table.file, set())]
                not_backed_up += len(eligible) - len(allowed)
            candidates += [(table, page) for page in allowed]
        capped = len(candidates) > cap
        for table, page in candidates[:cap]:
            try:
                repo.trash(table.db, page)
                trashed[table.db] = trashed.get(table.db, 0) + 1
            except Exception as exc:  # noqa: BLE001 - one row must not stop the others
                failures.append(f"{table.db} row {page.get('id')}: trash failed ({_short(exc)})")
        # Learning Queue: only after every progress row is gone, so a capped run does not reset it early.
        queue_file = next(t.file for t in FULL_TABLES if t.db == S.DB_LEARNING_QUEUE)
        queue_pages = next(pages for table, pages, _schema in scope if table.db == S.DB_LEARNING_QUEUE)
        if capped or failures:
            queue_skipped = True
        else:
            for page in queue_pages:
                if get_status(page, S.P_LearningQueue.QUEUE_STATUS) != "Mastered":
                    continue
                if not ctx.dry_run and str(page["id"]) not in backed.get(queue_file, set()):
                    continue
                try:
                    if repo.sync(S.DB_LEARNING_QUEUE, page, {S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Practicing")}):
                        mastered_reset += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"Learning Queue row {page.get('id')}: not reset ({_short(exc)})")
        set_dates = not failures
        if set_dates:
            try:
                config.set_date(repo, K.SEASON_START, reset_day, "Set by the full reset: the reset date; season views start here.")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"SEASON_START not set ({_short(exc)})")
            for member in repo.members():
                if not member.active:
                    continue
                try:
                    repo.sync(
                        S.DB_MEMBERS,
                        member.page,
                        {
                            S.P_Members.LAST_SYNC: nt.p_date(reset_day, with_time=False),
                            S.P_Members.STREAK_START: nt.p_date(reset_day, with_time=False),
                        },
                    )
                    dated += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"Last Sync / Streak Start for {member.name} not set ({_short(exc)})")

    lines = [f"full reset: reset date {reset_day.isoformat()} (Cairo); folder {out_dir}"]
    if opts.phase in ("all", "export"):
        lines.append("export: " + ", ".join(f"{name} {rows} row(s), {new} new" for name, (rows, new) in exported.items()))
    if ctx.dry_run:
        lines.append("count check: skipped in a dry run (no CSV is written)")
    else:
        lines.append("count check (Notion/CSV): " + ", ".join(f"{name} {n}/{c}" for name, (n, c) in counts.items()) + " OK")
    if opts.phase in ("all", "trash"):
        lines.append(
            "trashed: " + ", ".join(f"{t.db} {trashed.get(t.db, 0)}" for t in FULL_TABLES if t.trash is not None)
            + f" (total {sum(trashed.values())})"
        )
        lines.append(f"tasks kept (open, no status): {kept_open_tasks}")
        lines.append(f"not trashed because not in the CSV backup: {not_backed_up}")
        lines.append(f"capped: {'yes' if capped else 'no'} (ARCHIVE_PER_RUN {cap})")
        if queue_skipped:
            lines.append("Learning Queue Mastered -> Practicing: skipped (the trash is not finished; run again)")
        else:
            lines.append(f"Learning Queue Mastered -> Practicing: {mastered_reset} row(s)")
        lines.append(f"SEASON_START set: {'yes' if set_dates else 'no'}; Last Sync and Streak Start set for {dated} active member(s)")
    lines += [f"FAILED {line}" for line in failures]
    items = sum(new for _rows, new in exported.values()) + sum(trashed.values()) + mastered_reset
    return JobResult(job=JOB, result=WARNING if failures else OK, items=items, details="\n".join(lines))


def run(ctx: Context) -> JobResult:
    opts = ctx.archive if isinstance(ctx.archive, ArchiveOptions) else ArchiveOptions()
    # skipped=True: a refused run writes no Sync Log row either; the exit code is still 1 (result Error).
    if opts.phase not in PHASES:
        return JobResult(job=JOB, result=ERROR, items=0, details=f"archive refused: --phase must be one of {', '.join(PHASES)}", skipped=True)
    if opts.full:
        return _run_full(ctx, opts)
    if not ctx.dry_run and opts.confirm != CONFIRM_WORD:
        return JobResult(
            job=JOB,
            result=ERROR,
            items=0,
            details="archive refused: a real run needs confirm = ARCHIVE (the workflow asks for it). Nothing was written or trashed.",
            skipped=True,
        )
    try:
        label, cutoff, out_dir = _plan(ctx, opts)
    except ValueError as exc:
        return JobResult(job=JOB, result=ERROR, items=0, details=f"archive refused: {exc}. Nothing was written or trashed.", skipped=True)

    repo = ctx.repo
    marks = Marks(cutoff=cutoff, now=as_utc(ctx.now))
    try:
        scope = []
        for table in TABLES:
            schema = repo.data_source_properties(table.db)
            pages = [p for p in repo.rows(table.db) if table.in_scope(p, marks)]
            scope.append((table, pages, schema))
    except Exception as exc:  # noqa: BLE001 - nothing has been written yet
        log.warning("archive stopped while reading Notion: %s", _short(exc))
        return JobResult(job=JOB, result=ERROR, items=0, details=f"archive stopped, nothing written or trashed: {_short(exc)}")

    failures: list[str] = []
    exported: dict[str, tuple[int, int]] = {}
    if opts.phase in ("all", "export"):
        try:
            for table, pages, schema in scope:
                if not ctx.dry_run:
                    out_dir.mkdir(parents=True, exist_ok=True)
                exported[table.file] = _export_table(repo, table, pages, schema, out_dir, ctx.dry_run)
        except Exception as exc:  # noqa: BLE001 - an export failure means nothing is trashed
            log.warning("archive export failed, nothing trashed: %s", _short(exc))
            return JobResult(job=JOB, result=ERROR, items=0, details=f"export failed, nothing trashed: {_short(exc)}")

    trashed: dict[str, int] = {}
    not_backed_up = 0
    cap = max(0, int(num(ctx.cfg, K.ARCHIVE_PER_RUN)))
    capped = False
    set_dates = False
    streak_set = 0
    if opts.phase in ("all", "trash"):
        candidates = []
        for table, pages, _schema in scope:
            if ctx.dry_run:
                allowed = pages
            else:
                backed = _backed_up_ids(out_dir / table.file)
                allowed = [p for p in pages if str(p["id"]) in backed]
                not_backed_up += len(pages) - len(allowed)
            candidates += [(table, page) for page in allowed]
        capped = len(candidates) > cap
        for table, page in candidates[:cap]:
            try:
                repo.trash(table.db, page)
                trashed[table.db] = trashed.get(table.db, 0) + 1
            except Exception as exc:  # noqa: BLE001 - one row must not stop the others
                failures.append(f"{table.db} row {page.get('id')}: trash failed ({_short(exc)})")
        set_dates = not failures
        if set_dates:
            try:
                config.set_date(repo, K.SEASON_START, cutoff, "Set by the archive job: first day of the current month; season views start here.")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"SEASON_START not set ({_short(exc)})")
            for member in repo.members():
                if not member.active:
                    continue
                try:
                    repo.sync(S.DB_MEMBERS, member.page, {S.P_Members.STREAK_START: nt.p_date(cutoff, with_time=False)})
                    streak_set += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"Streak Start for {member.name} not set ({_short(exc)})")

    lines = [f"month: {label}; cutoff (first day of the current Cairo month): {cutoff.isoformat()}"]
    if opts.phase in ("all", "export"):
        lines.append("export: " + ", ".join(f"{name} {rows} row(s), {new} new" for name, (rows, new) in exported.items()) + f" -> {out_dir}")
    if opts.phase in ("all", "trash"):
        lines.append(
            f"trashed: daily_log {trashed.get(S.DB_DAILY_LOG, 0)}, tasks {trashed.get(S.DB_TASKS, 0)}, "
            f"weekly_summary {trashed.get(S.DB_WEEKLY_SUMMARY, 0)}, sync_log {trashed.get(S.DB_SYNC_LOG, 0)} "
            f"(total {sum(trashed.values())})"
        )
        lines.append(f"not trashed because not in the CSV backup: {not_backed_up}")
        lines.append(f"capped: {'yes' if capped else 'no'} (ARCHIVE_PER_RUN {cap})")
        lines.append(f"SEASON_START set: {'yes' if set_dates else 'no'}; Streak Start set for {streak_set} active member(s)")
    lines += [f"FAILED {line}" for line in failures]
    items = sum(new for _rows, new in exported.values()) + sum(trashed.values())
    return JobResult(job=JOB, result=WARNING if failures else OK, items=items, details="\n".join(lines))
