"""Job ``contests``: sync upcoming contests and create contest tasks.

* Contests are upserted by "External ID" (e.g. "CF-2050"). Re-running never
  creates a duplicate row. After the first create, Name, Kind, Link, Rated and
  Platform are fill-only (a coach's edit is kept); Start, Duration and Phase are
  kept in sync with the source.
* Phase is recomputed from the start time and duration for every row that is
  not Finished.
* Every run, each contest that starts within CONTEST_TASK_LEAD_HOURS (and has
  not finished) gets one Task per subscribed member, deduplicated by Source Key
  "contest:<External ID>:<member page id>". "Tasks Created" is ticked for display
  only; it no longer gates task creation (F15), so a subscription added later
  still gets its task.
* A Codeforces or AtCoder contest task that is still Planned / To Do 6 hours after
  the contest ended, with no attended Contest Entry for that member, becomes
  "Missed". LeetCode tasks are left alone: their participation is not detectable.

Each contest is processed on its own; one failure is reported and the others run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import OK, WARNING, Context, JobResult
from hq.logic import contest_kind
from hq.models import Contest
from hq.repo import get_checkbox, get_date, get_number, get_relation, get_select, get_status, get_text, get_title, get_url, member_task_props
from hq.schema import ConfigKey as K

log = logging.getLogger("hq.jobs.contests")

JOB = "contests"
MISSED_AFTER = timedelta(hours=6)
MISSABLE_PLATFORMS = frozenset({"Codeforces", "AtCoder"})
ATTENDED_STATUSES = frozenset({"Attended", "Upsolving", "Upsolved"})
OPEN_TASK_STATUSES = frozenset({"Planned", "To Do"})


def phase_for(start: datetime, duration_min: int, now: datetime) -> str:
    if now < start:
        return "Upcoming"
    if now < start + timedelta(minutes=duration_min):
        return "Live"
    return "Finished"


def contest_props(c: Contest, phase: str, kind: str) -> dict:
    return {
        S.P_Contests.TITLE: nt.p_title(c.name),
        S.P_Contests.PLATFORM: nt.p_select(c.platform),
        S.P_Contests.KIND: nt.p_select(kind),
        S.P_Contests.START: nt.p_date(c.start_utc),
        S.P_Contests.DURATION_MIN: nt.p_number(c.duration_min),
        S.P_Contests.LINK: nt.p_url(c.link),
        S.P_Contests.PHASE: nt.p_select(phase),
        S.P_Contests.RATED: nt.p_checkbox(c.rated),
        S.P_Contests.EXTERNAL_ID: nt.p_text(c.external_id),
    }


def run(ctx: Context) -> JobResult:
    repo = ctx.repo
    lead = timedelta(hours=num(ctx.cfg, K.CONTEST_TASK_LEAD_HOURS))
    created = updated = phase_changed = tasks_made = ticked = missed = 0
    failures: list[str] = []
    seen: set[str] = set()

    for c in ctx.sources.upcoming_contests():
        if not c.external_id or c.external_id in seen:
            continue
        seen.add(c.external_id)
        try:
            was_created, changed = _upsert_contest(ctx, c)
            created += int(was_created)
            updated += int(changed)
        except Exception as exc:  # noqa: BLE001 - one contest must not stop the others
            log.exception("contest %s failed", c.external_id)
            failures.append(f"FAILED {c.external_id} {c.name}: {type(exc).__name__}: {exc}")

    # Rows the source no longer lists: only their phase can still change.
    for page in repo.rows(S.DB_CONTESTS):
        if get_text(page, S.P_Contests.EXTERNAL_ID) in seen:
            continue
        if get_select(page, S.P_Contests.PHASE) == "Finished":
            continue
        start = get_date(page, S.P_Contests.START)
        duration = get_number(page, S.P_Contests.DURATION_MIN)
        if not isinstance(start, datetime) or duration is None:
            continue
        try:
            phase = phase_for(start, int(duration), ctx.now)
            if repo.sync(S.DB_CONTESTS, page, {S.P_Contests.PHASE: nt.p_select(phase)}):
                phase_changed += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("phase update failed for %s", get_title(page, S.P_Contests.TITLE))
            failures.append(f"FAILED phase {get_title(page, S.P_Contests.TITLE)}: {type(exc).__name__}: {exc}")

    members = ctx.active_members()
    for page in repo.rows(S.DB_CONTESTS):
        if get_select(page, S.P_Contests.PHASE) == "Finished":
            continue
        start = get_date(page, S.P_Contests.START)
        if not isinstance(start, datetime) or start - ctx.now > lead:
            continue
        name = get_title(page, S.P_Contests.TITLE)
        try:
            tasks_made += _create_contest_tasks(ctx, page, members)
            # With --member only one member was processed, so the contest must
            # stay open for the others. The tick is for display only (F15).
            if ctx.member_filter is None and not get_checkbox(page, S.P_Contests.TASKS_CREATED):
                repo.update(S.DB_CONTESTS, page, {S.P_Contests.TASKS_CREATED: nt.p_checkbox(True)})
                ticked += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("contest tasks failed for %s", name)
            failures.append(f"FAILED tasks for {name}: {type(exc).__name__}: {exc}")

    try:
        missed = _mark_missed_tasks(ctx)
    except Exception as exc:  # noqa: BLE001
        log.exception("missed-task check failed")
        failures.append(f"FAILED missed-task check: {type(exc).__name__}: {exc}")

    details = [
        f"contests: {created} created, {updated} updated, {phase_changed} phase changes",
        f"contest tasks created: {tasks_made} (contests ticked: {ticked})",
        f"contest tasks marked Missed: {missed}",
        *failures,
    ]
    result = WARNING if failures else OK
    return JobResult(
        job=JOB,
        result=result,
        items=created + updated + phase_changed + tasks_made + missed,
        details="\n".join(details),
    )


def _upsert_contest(ctx: Context, c: Contest) -> tuple[bool, bool]:
    """Create or update one contest. Returns (created, changed)."""
    repo = ctx.repo
    phase = phase_for(c.start_utc, c.duration_min, ctx.now)
    props = contest_props(c, phase, contest_kind.classify(c.platform, c.name))
    page = repo.find(S.DB_CONTESTS, S.P_Contests.EXTERNAL_ID, c.external_id)
    if page is None:
        repo.create(
            S.DB_CONTESTS,
            {**props, S.P_Contests.TASKS_CREATED: nt.p_checkbox(False), S.P_Contests.ALERT_SENT: nt.p_checkbox(False)},
        )
        return True, False
    # Identity fields are fill-only after the first create (F14).
    fill_only_names = (S.P_Contests.TITLE, S.P_Contests.PLATFORM, S.P_Contests.KIND, S.P_Contests.LINK, S.P_Contests.RATED)
    synced = {k: v for k, v in props.items() if k not in fill_only_names}
    filled = {k: v for k, v in props.items() if k in fill_only_names}
    changed = bool(repo.sync(S.DB_CONTESTS, page, synced))
    changed = bool(repo.sync(S.DB_CONTESTS, page, filled, fill_only=True)) or changed
    return False, changed


def _create_contest_tasks(ctx: Context, contest_page: dict, members: list) -> int:
    repo = ctx.repo
    external_id = get_text(contest_page, S.P_Contests.EXTERNAL_ID)
    name = get_title(contest_page, S.P_Contests.TITLE)
    link = get_url(contest_page, S.P_Contests.LINK) or ""
    kind = get_select(contest_page, S.P_Contests.KIND) or ""
    start = get_date(contest_page, S.P_Contests.START)
    made = 0
    for member in members:
        if not contest_kind.subscription_matches(kind, member.subscriptions):
            continue
        key = f"contest:{external_id}:{member.id}"
        if repo.find(S.DB_TASKS, S.P_Tasks.SOURCE_KEY, key) is not None:
            continue
        props = member_task_props(
            member,
            title=f"🏆 {name}",
            task_type="Contest",
            status="Planned",
            due=start,
            origin="Auto",
            source_key=key,
        )
        props[S.P_Tasks.CONTEST] = nt.p_relation([contest_page["id"]])
        props[S.P_Tasks.LINKS] = nt.p_text(link)
        repo.create(S.DB_TASKS, props)
        made += 1
    return made


def _mark_missed_tasks(ctx: Context) -> int:
    """Contest tasks still open 6 h after a Codeforces/AtCoder contest ended, with no
    attended Contest Entry for that member, become Missed (F10)."""
    repo = ctx.repo
    attended = set()
    for entry in repo.rows(S.DB_CONTEST_ENTRIES):
        if get_status(entry, S.P_ContestEntries.STATUS) not in ATTENDED_STATUSES:
            continue
        for contest_id in get_relation(entry, S.P_ContestEntries.CONTEST):
            for member_id in get_relation(entry, S.P_ContestEntries.MEMBER):
                attended.add((contest_id, member_id))
    marked = 0
    for task in repo.rows(S.DB_TASKS):
        if get_select(task, S.P_Tasks.TYPE) != "Contest" or get_status(task, S.P_Tasks.STATUS) not in OPEN_TASK_STATUSES:
            continue
        contest = repo.by_id(S.DB_CONTESTS, (get_relation(task, S.P_Tasks.CONTEST) or [None])[0])
        if contest is None or get_select(contest, S.P_Contests.PLATFORM) not in MISSABLE_PLATFORMS:
            continue
        start = get_date(contest, S.P_Contests.START)
        duration = get_number(contest, S.P_Contests.DURATION_MIN)
        if not isinstance(start, datetime) or duration is None:
            continue
        if ctx.now < start + timedelta(minutes=int(duration)) + MISSED_AFTER:
            continue
        assignees = get_relation(task, S.P_Tasks.ASSIGNEE)
        if any((contest["id"], member_id) in attended for member_id in assignees):
            continue
        repo.sync(S.DB_TASKS, task, {S.P_Tasks.STATUS: nt.p_status("Missed")})
        marked += 1
    return marked
