"""Orchestration jobs (B3). Every job takes a ``Context`` and returns a ``JobResult``.

Jobs:
  onboard   - learning-queue rows, default targets and subscriptions for new members
  contests  - sync upcoming contests, create contest tasks
  alerts    - contest reminders (one comment with mentions, optional email)
  imports   - pull accepted solves and ratings from CF / AtCoder / LeetCode
  recompute - daily logs, streaks, member totals, mastery, goals, achievements, teams
  daily     - daily practice tasks, skipped-task cleanup, upsolve reminders
  weekly    - weekly summaries, MVP, weekly report page
  cleanup   - after a member row is deleted: trash its leftover rows and dashboard, remove the name from selects

``execute`` runs one job, turns exceptions into an "Error" result, and writes one
Sync Log row. A failing job never stops the others.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any

from hq import icons
from hq import notion as nt
from hq import schema as S
from hq.repo import Member, Repo
from hq.timeutil import CAIRO_TZ, cairo_date

log = logging.getLogger("hq.jobs")

OK = "OK"
WARNING = "Warning"
ERROR = "Error"
SKIPPED = "Skipped"

# Sync Log "Job" select options differ from the CLI job names in one place.
LOG_JOB_NAMES: dict[str, str] = {"imports": "import", "backfill-weekly": "weekly"}
MAX_DETAILS = 1900


@dataclass
class JobResult:
    job: str
    result: str = OK
    items: int = 0
    details: str = ""
    skipped: bool = field(default=False)


@dataclass
class Context:
    """Everything a job needs. Built once per CLI run (or per test)."""

    repo: Repo
    sources: Any
    now: datetime
    cfg: dict[str, Any]
    mail: Callable[[str, str, str], bool] | None = None
    force: bool = False
    member_filter: str | None = None
    _problemset: list[dict[str, Any]] | None = None

    @property
    def dry_run(self) -> bool:
        return self.repo.dry_run

    @property
    def today(self) -> date:
        """The Africa/Cairo calendar day of ``now``."""
        return cairo_date(self.now)

    def members(self) -> list[Member]:
        """Members, restricted to ``member_filter`` (case-insensitive name) when set."""
        found = self.repo.members()
        if self.member_filter is None:
            return found
        wanted = self.member_filter.strip().lower()
        return [m for m in found if m.name.strip().lower() == wanted]

    def active_members(self) -> list[Member]:
        return [m for m in self.members() if m.active]

    def problemset(self) -> list[dict[str, Any]]:
        """The Codeforces problemset, fetched at most once per run."""
        if self._problemset is None:
            self._problemset = list(self.sources.cf_problemset())
        return self._problemset


def cairo_end_of_day(day: date) -> datetime:
    """23:59 on ``day`` in Africa/Cairo, as an aware UTC datetime."""
    return datetime(day.year, day.month, day.day, 23, 59, tzinfo=CAIRO_TZ).astimezone(timezone.utc)


def cairo_midnight(day: date) -> datetime:
    """00:00 on ``day`` in Africa/Cairo, as an aware UTC datetime."""
    return datetime.combine(day, time(0, 0), tzinfo=CAIRO_TZ).astimezone(timezone.utc)


def as_utc(value: datetime | date) -> datetime:
    """A date becomes midnight UTC; a naive datetime is taken as UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return datetime.combine(value, time(0, 0), tzinfo=timezone.utc)


def write_sync_log(ctx: Context, job: str, started: datetime, result: JobResult) -> None:
    log_job = LOG_JOB_NAMES.get(job, job)
    props = {
        S.P_SyncLog.TITLE: nt.p_title(f"{log_job} {started.replace(microsecond=0).isoformat()}"),
        S.P_SyncLog.STARTED: nt.p_date(started),
        S.P_SyncLog.JOB: nt.p_select(log_job),
        S.P_SyncLog.RESULT: nt.p_select(result.result),
        S.P_SyncLog.ITEMS: nt.p_number(result.items),
        S.P_SyncLog.DETAILS: nt.p_text(result.details[:MAX_DETAILS]),
    }
    try:
        ctx.repo.create(S.DB_SYNC_LOG, props, icon=icons.icon_for(S.DB_SYNC_LOG, props))
    except Exception:  # noqa: BLE001 - a log write must never fail the run
        log.exception("could not write the Sync Log row for %s", job)


def execute(ctx: Context, job: str, fn: Callable[[Context], JobResult]) -> JobResult:
    """Run one job. Errors become an "Error" result. A Sync Log row is written only when the job changed items or did not end OK."""
    started = ctx.now
    try:
        result = fn(ctx)
    except Exception as exc:  # noqa: BLE001 - one job must not stop the others
        log.exception("job %s failed", job)
        result = JobResult(job=job, result=ERROR, items=0, details=f"{type(exc).__name__}: {exc}")
    # Log only what matters: a job that did something, or did not end OK (F5).
    # A quiet OK run with nothing to report writes no row.
    if not result.skipped and (result.items > 0 or result.result != OK):
        write_sync_log(ctx, job, started, result)
    return result
