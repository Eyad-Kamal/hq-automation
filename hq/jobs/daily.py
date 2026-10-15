"""Job ``daily``: once per Cairo day. Runs only when STATE_LAST_DAILY is before today.

1. Overdue auto practice tasks (Origin Auto, Type Topic Practice, still open,
   due before today) become Skipped. Yesterday's are included, and so are any
   older ones left behind by a missed run.
2. Each active member gets today's Daily Log row (if missing) and one auto
   practice task "🎯 <topic>: <n> problems". The topic comes from the member's
   learning queue. If the member has any "Learning" row, only those rows are used
   (highest priority, then the earliest deadline, then topic order); otherwise
   Practicing rows, then the first Queued core topic of the member's level. Problems come from the Codeforces problemset, filtered
   by the topic's CF tags and rating band, excluding problems already solved.
   The pick is deterministic: seed = hash(member id, date). Source Key
   "auto:<date>:<member id>" makes the task unique per day.
3. Upsolve reminders: a contest entry (Attended/Upsolving) whose contest ended at
   least a day ago and whose Upsolve % is below 100 gets one "🔁 Upsolve" task.
   Source Key "upsolve:<entry id>:<member id>".
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import Any

from hq import config
from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import OK, WARNING, Context, JobResult, cairo_end_of_day, cairo_midnight
from hq.logic import autotasks
from hq.jobs.recompute import ensure_daily_log
from hq.repo import Member, Repo, Topic, cairo_day_of, get_date, get_number, get_relation, get_select, get_status, get_text, get_title, member_task_props
from hq.schema import ConfigKey as K
from hq.logic.problem_ref import norm_key
from hq.timeutil import cairo_date

log = logging.getLogger("hq.jobs.daily")

JOB = "daily"
OPEN_PRACTICE_STATUSES = frozenset({"To Do", "Planned", "In Progress"})
PRIORITY_ORDER = {"Very Important": 0, "Important": 1, "Optional": 2}
UPSOLVE_ENTRY_STATUSES = frozenset({"Attended", "Upsolving"})
COMPETENT_OR_MORE = frozenset({"Competent", "Proficient", "Master"})


def daily_due(ctx: Context) -> bool:
    last = config.state_date(ctx.repo, config.STATE_LAST_DAILY)
    return last is None or last < ctx.today


def run(ctx: Context) -> JobResult:
    repo = ctx.repo
    today = ctx.today
    topics = {t.id: t for t in repo.topics()}
    lq_rows = repo.rows(S.DB_LEARNING_QUEUE)
    skipped = _skip_overdue_practice(repo, today)
    stale_upsolve = _expire_stale_upsolve_tasks(ctx)
    practice = upsolve = 0
    notes: list[str] = []
    failures: list[str] = []
    members = ctx.active_members()
    for member in members:
        # One member's failure must not stop the others (F2).
        try:
            ensure_daily_log(ctx, member, today)
            made, note = _create_practice_task(ctx, member, today, topics, lq_rows)
            practice += made
            if note:
                notes.append(note)
            upsolve += _create_upsolve_tasks(ctx, member, today)
        except Exception as exc:  # noqa: BLE001
            log.exception("daily failed for %s", member.name)
            failures.append(f"FAILED {member.name}: {type(exc).__name__}: {exc}")
    # Set even when some members failed: the day's job ran, and the failures are reported.
    config.set_state(repo, config.STATE_LAST_DAILY, today.isoformat())
    details = [
        f"overdue practice tasks skipped: {skipped}",
        f"practice tasks created: {practice} for {len(members)} member(s)",
        f"upsolve reminders created: {upsolve}",
        f"stale upsolve reminders skipped (contest ended before the window): {stale_upsolve}",
        *notes,
        *failures,
    ]
    # A member with no task (no topic, or no problems picked) or a failure is a Warning.
    result = WARNING if (notes or failures) else OK
    return JobResult(job=JOB, result=result, items=practice + upsolve + skipped + stale_upsolve, details="\n".join(details))


def _skip_overdue_practice(repo: Repo, today: date) -> int:
    skipped = 0
    for task in repo.rows(S.DB_TASKS):
        if get_select(task, S.P_Tasks.TYPE) != "Topic Practice" or get_select(task, S.P_Tasks.ORIGIN) != "Auto":
            continue
        if get_status(task, S.P_Tasks.STATUS) not in OPEN_PRACTICE_STATUSES:
            continue
        due = get_date(task, S.P_Tasks.DUE)
        if not isinstance(due, datetime) or cairo_date(due) >= today:
            continue
        repo.sync(S.DB_TASKS, task, {S.P_Tasks.STATUS: nt.p_status("Skipped")})
        skipped += 1
    return skipped


def _candidate_topics(member: Member, lq_rows: list[dict], topics: dict[str, Topic]) -> list[Topic]:
    """Topics to practise, best first.

    * If the member has ANY queue row with Queue Status "Learning" (the topics the
      member picked to study), only those rows are candidates, ordered by Priority,
      then Deadline, then topic Order. A Learning row at Competent or higher is
      skipped. Recompute never changes a Learning status.
    * Otherwise (no Learning rows): Practicing rows first (same ordering), then the
      Queued core topics of the member's level, by Order.
    A queue row whose tier (Coach Override, else Auto Tier) is Competent, Proficient or
    Master is skipped: that topic is done for now (F6).
    """
    own_rows: list[tuple[dict, Topic | None]] = []
    for row in lq_rows:
        if member.id not in get_relation(row, S.P_LearningQueue.MEMBER):
            continue
        topic_ids = get_relation(row, S.P_LearningQueue.TOPIC)
        topic = topics.get(topic_ids[0]) if topic_ids else None
        own_rows.append((row, topic))
    has_learning = any(get_status(row, S.P_LearningQueue.QUEUE_STATUS) == "Learning" for row, _ in own_rows)

    active: list[tuple[tuple[int, date, int], Topic]] = []
    queued: list[tuple[int, Topic]] = []
    for row, topic in own_rows:
        if topic is None or not topic.has_band:
            continue
        tier = get_select(row, S.P_LearningQueue.COACH_OVERRIDE) or get_select(row, S.P_LearningQueue.AUTO_TIER)
        if tier in COMPETENT_OR_MORE:
            continue
        status = get_status(row, S.P_LearningQueue.QUEUE_STATUS)
        if has_learning:
            if status != "Learning":
                continue
        elif status == "Queued":
            if topic.core and topic.level_no == member.level_no:
                queued.append((topic.order, topic))
            continue
        elif status != "Practicing":
            continue
        priority = PRIORITY_ORDER.get(get_select(row, S.P_LearningQueue.PRIORITY) or "", 3)
        deadline = get_date(row, S.P_LearningQueue.DEADLINE)
        if isinstance(deadline, datetime):
            deadline = cairo_date(deadline)
        active.append(((priority, deadline or date.max, topic.order), topic))
    ordered = [t for _, t in sorted(active, key=lambda item: item[0])]
    if not has_learning:
        ordered += [t for _, t in sorted(queued, key=lambda item: item[0]) if t not in ordered]
    return ordered


def _solved_keys(repo: Repo, member: Member) -> set[str]:
    return {
        norm_key(get_text(p, S.P_Problems.PROBLEM_KEY))
        for p in repo.rows(S.DB_PROBLEMS)
        if get_status(p, S.P_Problems.STATUS) == "Solved" and member.id in get_relation(p, S.P_Problems.MEMBER)
    }


def pick_seed(member_id: str, day: date) -> int:
    """Stable across processes (Python's hash() is randomised per run)."""
    digest = hashlib.sha256(f"{member_id}|{day.isoformat()}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _bullet(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": nt.rich_text(text)}}


def _create_practice_task(
    ctx: Context,
    member: Member,
    today: date,
    topics: dict[str, Topic],
    lq_rows: list[dict],
) -> tuple[int, str]:
    """Create today's practice task for a member. Returns (created, warning text).

    Topics are tried in order (see ``_candidate_topics``); the first one with
    unsolved problems in its band wins, so one empty topic does not block the member.
    """
    repo = ctx.repo
    key = f"auto:{today.isoformat()}:{member.id}"
    if repo.find(S.DB_TASKS, S.P_Tasks.SOURCE_KEY, key) is not None:
        return 0, ""
    candidates = _candidate_topics(member, lq_rows, topics)
    if not candidates:
        return 0, f"{member.name}: no topic to practise (run the onboard job first, or all topics are Competent)"
    count = int(num(ctx.cfg, K.AUTO_TASK_PROBLEMS))
    solved = _solved_keys(repo, member)
    for topic in candidates:
        picks = autotasks.pick_problems(
            ctx.problemset(),
            solved,
            topic.cf_tags,
            int(topic.band_low),  # type: ignore[arg-type]
            int(topic.band_high),  # type: ignore[arg-type]
            count,
            pick_seed(member.id, today),
        )
        if picks:
            break
    else:
        return 0, f"{member.name}: no unsolved problems in the band of any queued topic"
    props = member_task_props(
        member,
        title=f"🎯 {topic.name}: {len(picks)} problems",
        task_type="Topic Practice",
        status="To Do",
        due=cairo_end_of_day(today),
        origin="Auto",
        source_key=key,
    )
    props[S.P_Tasks.TOPIC] = nt.p_relation([topic.id])
    props[S.P_Tasks.TARGET] = nt.p_number(len(picks))
    props[S.P_Tasks.LINKS] = nt.p_text("\n".join(f"{p['key']} | {p['link']}" for p in picks))
    children = [_bullet(f"{p['key']} — {p['name']} (rating {p['rating']}) — {p['link']}") for p in picks]
    repo.create(S.DB_TASKS, props, children=children)
    return 1, ""


def _upsolve_pct(entry: dict) -> int:
    total = get_number(entry, S.P_ContestEntries.TOTAL_PROBLEMS)
    if not total:
        return 0
    done = (get_number(entry, S.P_ContestEntries.SOLVED) or 0) + (get_number(entry, S.P_ContestEntries.UPSOLVED) or 0)
    return round(done / total * 100)


def _contest_end_of(contest: dict | None, entry: dict | None) -> datetime | None:
    """When a contest ended (start + duration). Without a contest time, the end of the
    entry's Cairo day (midnight after it), so a date-only entry still has a usable end.
    None when neither is known."""
    if contest is not None:
        start = get_date(contest, S.P_Contests.START)
        duration = get_number(contest, S.P_Contests.DURATION_MIN)
        if isinstance(start, datetime) and duration is not None:
            return start + timedelta(minutes=int(duration))
    if entry is None:
        return None
    day = cairo_day_of(get_date(entry, S.P_ContestEntries.DATE))
    return cairo_midnight(day + timedelta(days=1)) if day is not None else None


def _entry_contest(repo: Repo, entry: dict) -> dict | None:
    return repo.by_id(S.DB_CONTESTS, (get_relation(entry, S.P_ContestEntries.CONTEST) or [None])[0])


def _ended_in_window(ctx: Context, entry: dict, cutoff: datetime, window_start: datetime, today: date) -> bool:
    """True when the entry's contest ended in [window_start, cutoff]. Without a contest
    time the entry date must be yesterday or earlier and not older than the window."""
    contest = _entry_contest(ctx.repo, entry)
    if contest is not None and isinstance(get_date(contest, S.P_Contests.START), datetime):
        end = _contest_end_of(contest, entry)
        return end is not None and window_start <= end <= cutoff
    day = cairo_day_of(get_date(entry, S.P_ContestEntries.DATE))
    return day is not None and cairo_date(window_start) <= day <= today - timedelta(days=1)


def _expire_stale_upsolve_tasks(ctx: Context) -> int:
    """Open auto Upsolve tasks whose contest ended more than UPSOLVE_WINDOW_DAYS ago
    become Skipped (cleans up reminders created before the window existed)."""
    repo = ctx.repo
    window = timedelta(days=num(ctx.cfg, K.UPSOLVE_WINDOW_DAYS))
    expired = 0
    for task in repo.rows(S.DB_TASKS):
        if get_select(task, S.P_Tasks.TYPE) != "Upsolve" or get_select(task, S.P_Tasks.ORIGIN) != "Auto":
            continue
        if get_status(task, S.P_Tasks.STATUS) not in OPEN_PRACTICE_STATUSES:
            continue
        contest = repo.by_id(S.DB_CONTESTS, (get_relation(task, S.P_Tasks.CONTEST) or [None])[0])
        entry = repo.by_id(S.DB_CONTEST_ENTRIES, (get_relation(task, S.P_Tasks.CONTEST_ENTRY) or [None])[0])
        end = _contest_end_of(contest, entry)
        if end is None or end >= ctx.now - window:
            continue
        repo.sync(S.DB_TASKS, task, {S.P_Tasks.STATUS: nt.p_status("Skipped")})
        expired += 1
    return expired


def _create_upsolve_tasks(ctx: Context, member: Member, today: date) -> int:
    repo = ctx.repo
    cutoff = ctx.now - timedelta(days=1)
    window_start = ctx.now - timedelta(days=num(ctx.cfg, K.UPSOLVE_WINDOW_DAYS))
    made = 0
    for entry in repo.rows(S.DB_CONTEST_ENTRIES):
        if member.id not in get_relation(entry, S.P_ContestEntries.MEMBER):
            continue
        if get_status(entry, S.P_ContestEntries.STATUS) not in UPSOLVE_ENTRY_STATUSES:
            continue
        if not _ended_in_window(ctx, entry, cutoff, window_start, today):
            continue
        if _upsolve_pct(entry) >= 100:
            continue
        key = f"upsolve:{entry['id']}:{member.id}"
        if repo.find(S.DB_TASKS, S.P_Tasks.SOURCE_KEY, key) is not None:
            continue
        contest_ids = get_relation(entry, S.P_ContestEntries.CONTEST)
        contest = repo.by_id(S.DB_CONTESTS, contest_ids[0]) if contest_ids else None
        name = get_title(contest, S.P_Contests.TITLE) if contest else get_title(entry, S.P_ContestEntries.TITLE)
        props = member_task_props(
            member,
            title=f"🔁 Upsolve {name}",
            task_type="Upsolve",
            status="To Do",
            due=cairo_end_of_day(today),
            origin="Auto",
            source_key=key,
        )
        props[S.P_Tasks.CONTEST_ENTRY] = nt.p_relation([entry["id"]])
        if contest is not None:
            props[S.P_Tasks.CONTEST] = nt.p_relation([contest["id"]])
        repo.create(S.DB_TASKS, props)
        made += 1
    return made
