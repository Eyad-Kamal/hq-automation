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
"""

from __future__ import annotations

import logging

from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import OK, WARNING, Context, JobResult
from hq.repo import get_relation
from hq.schema import ConfigKey as K

log = logging.getLogger("hq.jobs.onboard")
JOB = "onboard"

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

    details = [
        f"members checked: {len(members)}",
        f"learning queue rows created: {queue_rows}",
        *(f"FAILED {line}" for line in failures),
    ]
    result = WARNING if failures else OK
    return JobResult(job=JOB, result=result, items=repo.writes - writes_before, details="\n".join(details))


def _onboard_member(ctx: Context, member, topics, pairs: set) -> int:
    """Create the member's missing queue rows and fill their blank defaults."""
    repo = ctx.repo
    created = 0
    for topic in sorted(topics, key=lambda t: (t.level_no, t.order, t.name)):
        if (member.id, topic.id) in pairs:
            continue
        priority = "Important" if topic.core and topic.level_no == member.level_no else "Optional"
        repo.create(
            S.DB_LEARNING_QUEUE,
            {
                S.P_LearningQueue.TITLE: nt.p_title(f"{topic.name} · {member.name}"),
                S.P_LearningQueue.WHO: nt.p_select(member.name),
                S.P_LearningQueue.MEMBER: nt.p_relation([member.id]),
                S.P_LearningQueue.TOPIC: nt.p_relation([topic.id]),
                S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Queued"),
                S.P_LearningQueue.PRIORITY: nt.p_select(priority),
            },
        )
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
