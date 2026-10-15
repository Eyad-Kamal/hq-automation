"""Job ``alerts``: remind participants shortly before a contest starts.

For each contest with 0 < minutes_to_start <= ALERT_MINUTES + 10 and "Alert Sent"
unticked:

1. "Alert Sent" is ticked FIRST, so an alert is never sent twice even if a later
   step fails (the cost is that a failed comment is not retried; it is reported).
2. If the contest has active tasks (Planned / To Do / In Progress), one comment
   is posted on the contest page: the reminder text plus an @mention for each
   participant's Person. If SMTP is configured, each participant with an Email
   also gets one email.

A contest with no active participant only gets "Alert Sent" ticked.
Each contest is handled on its own: one failure is reported and the others run.
"""

from __future__ import annotations

import logging
from datetime import datetime

from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import OK, WARNING, Context, JobResult
from hq.notify import comment_rich_text
from hq.repo import get_checkbox, get_date, get_people, get_relation, get_status, get_title, get_url
from hq.schema import ConfigKey as K

log = logging.getLogger("hq.jobs.alerts")

JOB = "alerts"
ACTIVE_TASK_STATUSES = ("Planned", "To Do", "In Progress")
WINDOW_SLACK_MINUTES = 10  # covers GitHub's scheduling delay


def run(ctx: Context) -> JobResult:
    repo = ctx.repo
    window = num(ctx.cfg, K.ALERT_MINUTES) + WINDOW_SLACK_MINUTES
    tasks = repo.rows(S.DB_TASKS)
    members = {m.id: m for m in repo.members()}
    posted = emailed = 0
    failures: list[str] = []
    lines: list[str] = []

    for page in repo.rows(S.DB_CONTESTS):
        if get_checkbox(page, S.P_Contests.ALERT_SENT):
            continue
        start = get_date(page, S.P_Contests.START)
        if not isinstance(start, datetime):
            continue
        minutes = (start - ctx.now).total_seconds() / 60
        if not 0 < minutes <= window:
            continue
        name = get_title(page, S.P_Contests.TITLE)
        try:
            sent_posts, sent_mails, line, warning = _alert_contest(ctx, page, minutes, tasks, members)
        except Exception as exc:  # noqa: BLE001 - one contest must not stop the others
            log.exception("alert failed for %s", name)
            failures.append(f"FAILED {name}: {type(exc).__name__}: {exc}")
            continue
        posted += sent_posts
        emailed += sent_mails
        lines.append(line)
        if warning:
            failures.append(warning)

    details = [f"alerts posted: {posted}, emails sent: {emailed}", *lines, *failures]
    return JobResult(
        job=JOB,
        result=WARNING if failures else OK,
        items=posted,
        details="\n".join(details),
    )


def _alert_contest(ctx: Context, page: dict, minutes: float, tasks: list, members: dict) -> tuple[int, int, str, str]:
    """Alert for one contest. Returns (posted, emails, summary line, warning or '')."""
    repo = ctx.repo
    name = get_title(page, S.P_Contests.TITLE)
    link = get_url(page, S.P_Contests.LINK) or ""
    mins = max(1, round(minutes))
    contest_id = page["id"]
    participants = [
        t
        for t in tasks
        if contest_id in get_relation(t, S.P_Tasks.CONTEST)
        and get_status(t, S.P_Tasks.STATUS) in ACTIVE_TASK_STATUSES
    ]
    # Tick first: an alert is never sent twice, even if a later step fails.
    repo.update(S.DB_CONTESTS, page, {S.P_Contests.ALERT_SENT: nt.p_checkbox(True)})
    if not participants:
        return 0, 0, f"{name}: no active tasks, nothing sent", ""

    person_ids: list[str] = []
    emails: list[str] = []
    for task in participants:
        assignee_ids = get_relation(task, S.P_Tasks.ASSIGNEE)
        member = members.get(assignee_ids[0]) if assignee_ids else None
        person_ids += get_people(task, S.P_Tasks.ASSIGNEE_PERSON) or (member.person_ids if member else [])
        if member is not None and member.email:
            emails.append(member.email)

    text = f"⏰ {name} starts in {mins} min — {link}"
    warning = ""
    try:
        repo.comment(contest_id, comment_rich_text(text, person_ids))
        line = f"{name}: comment posted for {len(participants)} task(s)"
    except Exception as exc:  # noqa: BLE001 - reported as a warning, not retried
        line = f"{name}: comment not posted"
        warning = f"{name}: comment failed ({type(exc).__name__})"

    sent = 0
    if ctx.mail is not None and not ctx.dry_run:
        subject = f"⏰ {name} starts in {mins} min"
        body = f"{name} starts in about {mins} minutes.\nLink: {link}\nOpen your task in MUST CPC Training HQ."
        for address in dict.fromkeys(emails):
            if ctx.mail(address, subject, body):
                sent += 1
    return 1, sent, line, warning
