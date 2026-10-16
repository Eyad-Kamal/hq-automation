"""Job ``onboard``: set up every active member. Cheap enough to run on every tick.

For each active member:

* One Learning Queue row per Topic that does not exist yet (checked per
  (member, topic) pair, so new Topics are picked up later too). Name
  "<topic> · <member>", Queue Status "Queued", Priority "Important" for core
  topics of the member's level and "Optional" otherwise.
* Joined (today, Cairo) when empty.
* Daily Target (DEFAULT_DAILY_TARGET) when empty.
* Contest Subscriptions by level when empty. A member without a level gets L1.

Only blank fields are filled. A value the coach set is never overwritten.

Then, when DASHBOARDS_AUTO is 1, every active member without a Dashboard URL gets a
personal dashboard page (see ``hq.jobs.dashboards``).

Title cleanup: Tasks and Weekly Reports titles used to start with an emoji. The row icon shows
it now, so the emoji is removed from up to TITLE_FIX_PER_RUN titles per run (idempotent).
"""

from __future__ import annotations

import logging

from hq import icons
from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import OK, WARNING, Context, JobResult
from hq.repo import get_relation, get_title
from hq.schema import ConfigKey as K

log = logging.getLogger("hq.jobs.onboard")
JOB = "onboard"

# Emoji that used to start the titles of auto tasks and weekly reports (removed by the title cleanup).
OLD_TITLE_PREFIXES: tuple[str, ...] = ("🏆 ", "🎯 ", "💡 ", "🔁 ", "📰 ")

DEFAULT_SUBSCRIPTIONS: dict[int, list[str]] = {
    1: ["CF Div. 4", "CF Div. 3", "AtCoder ABC"],
    2: ["CF Div. 3", "CF Div. 2", "CF Educational", "AtCoder ABC"],
    3: ["CF Div. 2", "CF Educational", "CF Div. 1+2", "AtCoder ABC", "AtCoder ARC"],
    4: ["CF Div. 1+2", "CF Div. 2", "CF Div. 1", "CF Educational", "AtCoder ARC", "AtCoder AGC"],
}


def run(ctx: Context) -> JobResult:
    repo = ctx.repo
    writes_before = repo.writes
    topics = repo.topics()
    pairs = {
        (member_id, topic_id)
        for row in repo.rows(S.DB_LEARNING_QUEUE)
        for member_id in get_relation(row, S.P_LearningQueue.MEMBER)
        for topic_id in get_relation(row, S.P_LearningQueue.TOPIC)
    }
    members = ctx.active_members()
    queue_rows = 0
    failures: list[str] = []
    for member in members:
        # One member's failure must not stop the others (F2).
        try:
            queue_rows += _onboard_member(ctx, member, topics, pairs)
        except Exception as exc:  # noqa: BLE001
            log.exception("onboard failed for %s", member.name)
            failures.append(f"{member.name}: {type(exc).__name__}: {exc}")

    # Row icons: up to ICON_BACKFILL_PER_RUN empty icons per run, round-robin over the databases.
    # Icon writes count as items, so the Sync Log shows them (and the errors behind any failures).
    icon_line = "row icons: not run"
    try:
        icon_report = icons.backfill(repo, ctx.cfg)
        by_db = ", ".join(f"{db} {n}" for db, n in icon_report.set_by_db.items() if n)
        icon_line = f"icons backfilled: {icon_report.total}" + (f" ({by_db})" if by_db else "")
        left = {db: n for db, n in icon_report.remaining.items() if n}
        icon_line += "; still without an icon: " + (", ".join(f"{db} {n}" for db, n in left.items()) or "none")
        failures.extend(f"icon write {err}" for err in icon_report.errors)
    except Exception as exc:  # noqa: BLE001 - icons are cosmetic; they must not fail the job
        log.exception("row icon backfill failed")
        failures.append(f"icon backfill: {type(exc).__name__}: {str(exc)[:200]}")
    title_line = "emoji title prefixes removed: not run"
    try:
        title_line = f"emoji title prefixes removed: {_strip_old_title_prefixes(ctx)}"
    except Exception as exc:  # noqa: BLE001 - titles are cosmetic; they must not fail the job
        log.exception("title prefix cleanup failed")
        failures.append(f"title prefix cleanup: {type(exc).__name__}: {str(exc)[:200]}")
    details = [
        f"members checked: {len(members)}",
        f"learning queue rows created: {queue_rows}",
        icon_line,
        title_line,
        *(f"FAILED {line}" for line in failures),
    ]
    result = WARNING if failures else OK
    return JobResult(job=JOB, result=result, items=repo.writes - writes_before, details="\n".join(details))


def _strip_old_title_prefixes(ctx: Context) -> int:
    """Remove an old emoji prefix from Tasks and Weekly Reports titles: at most TITLE_FIX_PER_RUN
    renames per run. A renamed title no longer starts with a prefix, so a later run changes nothing.
    Returns the number of titles renamed."""
    repo = ctx.repo
    budget = int(num(ctx.cfg, K.TITLE_FIX_PER_RUN))
    renamed = 0
    for db, prop in ((S.DB_TASKS, S.P_Tasks.TITLE), (S.DB_WEEKLY_REPORTS, S.P_WeeklyReports.TITLE)):
        for page in repo.rows(db):
            if renamed >= budget:
                return renamed
            title = get_title(page, prop)
            prefix = next((p for p in OLD_TITLE_PREFIXES if title.startswith(p)), None)
            if prefix is None or not title[len(prefix):].strip():
                continue
            repo.sync(db, page, {prop: nt.p_title(title[len(prefix):])})
            renamed += 1
    return renamed


def _onboard_member(ctx: Context, member, topics, pairs: set) -> int:
    """Create the member's missing queue rows and fill their blank defaults."""
    repo = ctx.repo
    created = 0
    for topic in sorted(topics, key=lambda t: (t.level_no, t.order, t.name)):
        if (member.id, topic.id) in pairs:
            continue
        priority = "Important" if topic.core and topic.level_no == member.level_no else "Optional"
        queue_props = {
            S.P_LearningQueue.TITLE: nt.p_title(f"{topic.name} · {member.name}"),
            S.P_LearningQueue.WHO: nt.p_select(member.name),
            S.P_LearningQueue.MEMBER: nt.p_relation([member.id]),
            S.P_LearningQueue.TOPIC: nt.p_relation([topic.id]),
            S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Queued"),
            S.P_LearningQueue.PRIORITY: nt.p_select(priority),
        }
        repo.create(S.DB_LEARNING_QUEUE, queue_props, icon=icons.icon_for(S.DB_LEARNING_QUEUE, queue_props))
        pairs.add((member.id, topic.id))
        created += 1

    # fill_only: a value that is already set (even a coach's) is left alone.
    defaults = {
        S.P_Members.JOINED: nt.p_date(ctx.today, with_time=False),
        S.P_Members.DAILY_TARGET: nt.p_number(int(num(ctx.cfg, K.DEFAULT_DAILY_TARGET))),
        S.P_Members.CONTEST_SUBSCRIPTIONS: nt.p_multi(
            DEFAULT_SUBSCRIPTIONS.get(member.level_no, DEFAULT_SUBSCRIPTIONS[1])
        ),
    }
    repo.sync(S.DB_MEMBERS, member.page, defaults, fill_only=True)
    return created
