"""Job ``weekly``: summarise the previous Saturday-to-Friday week.

Due when Cairo time is at or after the Saturday 00:10 of the current week and
STATE_LAST_WEEKLY is before that Saturday. The job summarises the week that
just ended (previous Saturday up to the Friday before the current Saturday),
then sets STATE_LAST_WEEKLY to the current Saturday.

Outputs (all keyed by Source Key so a rerun updates instead of duplicating):

* Weekly Summary rows: one per active member ("person:<member>:<week>"), one
  per team ("team:<team>:<week>"), and one for the club ("club:club:<week>").
  Points, problems, active days, ratings and contests come from the Daily Log
  rows via ``weekly.summarize``. Points Δ is compared with the previous week's
  Daily Log points. Rating Δ is the sum of CF/AtCoder rating changes in the week.
  Topics Advanced counts distinct topics of problems solved that week.
* MVP: the member with the most points that week (points > 0; ties broken by
  name). That member's summary gets the MVP tick and a "Weekly MVP" Achievement
  keyed "Weekly MVP:<member>:<week>".
* One Weekly Reports page per week (found by Week Start, so it is created once).
  Its body holds the leaderboard, teams, movers, streaks and contests. Summaries
  link to it via "Report". A comment mentions ADMIN_USER_ID.

Team and club summaries, the report and the MVP are only produced on full runs
(without --member); a single-member run only refreshes that member's summary.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from hq import config
from hq import icons
from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import OK, Context, JobResult
from hq.jobs import recompute
from hq.logic import weekly
from hq.notify import comment_rich_text
from hq.repo import (
    Member,
    Repo,
    cairo_day_of,
    get_checkbox,
    get_date,
    get_number,
    get_relation,
    get_status,
    get_title,
)
from hq.schema import ConfigKey as K
from hq.timeutil import CAIRO_TZ, week_label, week_start

log = logging.getLogger("hq.jobs.weekly")
JOB = "weekly"
REPORT_TIME = time(0, 10)
MVP_BADGE = "Weekly MVP"
BOARD_LIMIT = 3
MAX_CREATE_CHILDREN = 100


def weekly_due(ctx: Context) -> bool:
    local = ctx.now.astimezone(CAIRO_TZ)
    saturday = week_start(local.date())
    boundary = datetime.combine(saturday, REPORT_TIME, tzinfo=CAIRO_TZ)
    last = config.state_date(ctx.repo, config.STATE_LAST_WEEKLY)
    return local >= boundary and (last is None or last < saturday)


def run(ctx: Context) -> JobResult:
    repo = ctx.repo
    saturday = week_start(ctx.today)
    week_begin = saturday - timedelta(days=7)
    prev_begin = week_begin - timedelta(days=7)
    week_days = [week_begin + timedelta(days=i) for i in range(7)]
    prev_days = [prev_begin + timedelta(days=i) for i in range(7)]
    label = week_label(week_begin)
    writes_before = repo.writes

    daily = _daily_index(repo)
    members = [m for m in repo.members() if m.active]
    scope = ctx.active_members()
    full_run = ctx.member_filter is None
    rating_delta, topics_per_member = _week_extras(repo, week_begin, saturday)

    person_rows: dict[str, list[dict]] = {}
    person_prev: dict[str, float] = {}
    for m in members:
        person_rows[m.id] = [daily[(m.id, d)] for d in week_days if (m.id, d) in daily]
        person_prev[m.id] = sum(daily[(m.id, d)]["points"] for d in prev_days if (m.id, d) in daily)

    person_summary: dict[str, dict[str, Any]] = {}
    for m in scope:
        summary = weekly.summarize(person_rows.get(m.id, []), person_prev.get(m.id, 0.0))
        summary["rating_delta"] = rating_delta.get(m.id, 0)
        summary["topics_advanced"] = len(topics_per_member.get(m.id, set()))
        person_summary[m.id] = summary

    created_report = False
    report: dict | None = None
    mvp_member: Member | None = None
    if full_run:
        mvp_member = _pick_mvp(members, person_summary)
        teams = _team_summaries(repo, members, person_rows, person_prev, rating_delta, topics_per_member)
        club_rows = _aggregate([row for rows in person_rows.values() for row in rows])
        club_prev = sum(person_prev.values())
        club = weekly.summarize(club_rows, club_prev)
        club["rating_delta"] = sum(rating_delta.values())
        club["topics_advanced"] = len(set().union(*topics_per_member.values())) if topics_per_member else 0
        report, created_report = _ensure_report(ctx, week_begin, label, club, members, person_summary, teams, mvp_member)

    for m in scope:
        _upsert_summary(
            repo,
            m.name,
            "Person",
            m.name,
            week_begin,
            label,
            person_summary[m.id],
            key=f"person:{m.id}:{week_begin.isoformat()}",
            member_id=m.id,
            mvp=mvp_member is not None and mvp_member.id == m.id,
            report_id=report["id"] if report else None,
        )
    if mvp_member is not None:
        _award_mvp(repo, mvp_member, week_begin, label, person_summary[mvp_member.id])

    if full_run:
        for team, summary in teams:
            _upsert_summary(
                repo,
                f"{team['name']}",
                "Team",
                team["name"],  # Who = team name, so charts group by team (F9)
                week_begin,
                label,
                summary,
                key=f"team:{team['id']}:{week_begin.isoformat()}",
                team_id=team["id"],
                mvp=False,
                report_id=report["id"] if report else None,
            )
        _upsert_summary(
            repo,
            "Club",
            "Club",  # scope "Club"; Who = "Club" for the club row (F9)
            "Club",
            week_begin,
            label,
            club,
            key=f"club:club:{week_begin.isoformat()}",
            mvp=False,
            report_id=report["id"] if report else None,
        )

    history_rows = 0
    if full_run:
        # Older weeks: only missing rows are created and changed values written (no report, no MVP).
        history_rows = backfill_history(ctx, range(2, int(num(ctx.cfg, K.WEEKLY_BACKFILL_WEEKS)) + 1))
    config.set_state(repo, config.STATE_LAST_WEEKLY, saturday.isoformat())
    items = repo.writes - writes_before
    details = [
        f"week {label}: {len(scope)} member summar{'y' if len(scope) == 1 else 'ies'}",
        f"report: {'created' if created_report else ('exists' if report else 'not written (partial run)')}",
        f"MVP: {mvp_member.name if mvp_member else 'none'}",
        f"history weeks: {history_rows} older summary row(s) written",
    ]
    return JobResult(job=JOB, result=OK, items=items, details="\n".join(details))


def backfill_run(ctx: Context) -> JobResult:
    """CLI ``backfill-weekly``: fill Weekly Summary rows for the last WEEKLY_BACKFILL_WEEKS
    complete weeks (the latest one included). No Weekly Reports pages and no MVP awards."""
    writes_before = ctx.repo.writes
    weeks = max(1, int(num(ctx.cfg, K.WEEKLY_BACKFILL_WEEKS)))
    written = backfill_history(ctx, range(1, weeks + 1))
    details = [f"weeks checked: {weeks}", f"summary rows written: {written}"]
    return JobResult(job=JOB, result=OK, items=ctx.repo.writes - writes_before, details="\n".join(details))


def _computed_daily_index(ctx: Context) -> dict[tuple[str, date], dict[str, Any]]:
    """Per member and Cairo day, the same row shape as ``_daily_index``, computed from the
    Problems and Contest Entries rows. Used for older weeks, which have no Daily Log rows."""
    repo = ctx.repo
    index: dict[tuple[str, date], dict[str, Any]] = {}
    duplicates: list[str] = []
    for member in [m for m in repo.members() if m.active]:
        ref = recompute.member_ref_rating(repo, member)
        solved = recompute._member_problems(repo, member, ref, ctx.cfg, duplicates)
        entries = recompute._attended_entries(repo, member, duplicates)
        contest_days = Counter(cairo_day_of(get_date(e, S.P_ContestEntries.DATE)) for e in entries)
        for day, stat in recompute._day_stats(solved, contest_days).items():
            index[(member.id, day)] = {
                "date": day,
                "problems": stat.problems,
                "points": round(stat.points, 2),
                "contests": stat.contests,
                "max_rating": stat.max_rating or None,
                "active": stat.problems > 0 or stat.contests > 0,
            }
    return index


def backfill_history(ctx: Context, week_offsets: range) -> int:
    """Person, Team and Club Weekly Summary rows for the weeks ``saturday - 7*k`` (k in
    ``week_offsets``). Creates missing rows and updates changed values only. Existing rows
    keep their MVP tick and Report link. Returns the number of rows written."""
    repo = ctx.repo
    saturday = week_start(ctx.today)
    writes_before = repo.writes
    index = _computed_daily_index(ctx)
    members = [m for m in repo.members() if m.active]
    for k in week_offsets:
        week_begin = saturday - timedelta(days=7 * k)
        prev_begin = week_begin - timedelta(days=7)
        week_days = [week_begin + timedelta(days=i) for i in range(7)]
        prev_days = [prev_begin + timedelta(days=i) for i in range(7)]
        label = week_label(week_begin)
        rating_delta, topics_per_member = _week_extras(repo, week_begin, week_begin + timedelta(days=7))
        person_rows = {m.id: [index[(m.id, d)] for d in week_days if (m.id, d) in index] for m in members}
        person_prev = {m.id: sum(index[(m.id, d)]["points"] for d in prev_days if (m.id, d) in index) for m in members}
        for m in members:
            summary = weekly.summarize(person_rows[m.id], person_prev[m.id])
            summary["rating_delta"] = rating_delta.get(m.id, 0)
            summary["topics_advanced"] = len(topics_per_member.get(m.id, set()))
            _upsert_history_summary(
                repo, m.name, "Person", m.name, week_begin, label, summary,
                key=f"person:{m.id}:{week_begin.isoformat()}", member_id=m.id,
            )
        for team, summary in _team_summaries(repo, members, person_rows, person_prev, rating_delta, topics_per_member):
            _upsert_history_summary(
                repo, team["name"], "Team", team["name"], week_begin, label, summary,
                key=f"team:{team['id']}:{week_begin.isoformat()}", team_id=team["id"],
            )
        club_rows = _aggregate([row for rows in person_rows.values() for row in rows])
        club = weekly.summarize(club_rows, sum(person_prev.values()))
        club["rating_delta"] = sum(rating_delta.values())
        club["topics_advanced"] = len(set().union(*topics_per_member.values())) if topics_per_member else 0
        _upsert_history_summary(
            repo, "Club", "Club", "Club", week_begin, label, club,
            key=f"club:club:{week_begin.isoformat()}",
        )
    return repo.writes - writes_before


def _upsert_history_summary(
    repo: Repo,
    name: str,
    scope: str,
    who: str,
    week_begin: date,
    label: str,
    summary: dict[str, Any],
    *,
    key: str,
    member_id: str | None = None,
    team_id: str | None = None,
) -> None:
    """Upsert a summary for a backfilled week, keeping the MVP tick and Report link that
    an existing row already has."""
    existing = repo.find(S.DB_WEEKLY_SUMMARY, S.P_WeeklySummary.SOURCE_KEY, key)
    mvp = get_checkbox(existing, S.P_WeeklySummary.MVP) if existing is not None else False
    report_ids = get_relation(existing, S.P_WeeklySummary.REPORT) if existing is not None else []
    _upsert_summary(
        repo, name, scope, who, week_begin, label, summary,
        key=key, member_id=member_id, team_id=team_id, mvp=mvp,
        report_id=report_ids[0] if report_ids else None,
    )


def _daily_index(repo: Repo) -> dict[tuple[str, date], dict[str, Any]]:
    index: dict[tuple[str, date], dict[str, Any]] = {}
    for page in repo.rows(S.DB_DAILY_LOG):
        day = cairo_day_of(get_date(page, S.P_DailyLog.DATE))
        if day is None:
            continue
        row = {
            "date": day,
            "problems": get_number(page, S.P_DailyLog.PROBLEMS) or 0,
            "points": get_number(page, S.P_DailyLog.POINTS) or 0.0,
            "contests": get_number(page, S.P_DailyLog.CONTESTS) or 0,
            "max_rating": get_number(page, S.P_DailyLog.MAX_RATING_SOLVED),
            "active": get_checkbox(page, S.P_DailyLog.ACTIVE),
        }
        for member_id in get_relation(page, S.P_DailyLog.MEMBER):
            index[(member_id, day)] = row
    return index


def _week_extras(repo: Repo, begin: date, end: date) -> tuple[dict[str, float], dict[str, set[str]]]:
    rating_delta: dict[str, float] = defaultdict(float)
    for page in repo.rows(S.DB_RATING_HISTORY):
        day = cairo_day_of(get_date(page, S.P_RatingHistory.DATE))
        old = get_number(page, S.P_RatingHistory.OLD)
        new = get_number(page, S.P_RatingHistory.NEW)
        if day is None or not begin <= day < end or old is None or new is None:
            continue
        for member_id in get_relation(page, S.P_RatingHistory.MEMBER):
            rating_delta[member_id] += new - old
    topics: dict[str, set[str]] = defaultdict(set)
    for page in repo.rows(S.DB_PROBLEMS):
        if get_status(page, S.P_Problems.STATUS) != "Solved":
            continue
        day = cairo_day_of(get_date(page, S.P_Problems.SOLVED_ON))
        if day is None or not begin <= day < end:
            continue
        for member_id in get_relation(page, S.P_Problems.MEMBER):
            topics[member_id].update(get_relation(page, S.P_Problems.TOPICS))
    return dict(rating_delta), dict(topics)


def _aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine several members' daily rows into one row per day."""
    by_day: dict[date, dict[str, Any]] = {}
    for row in rows:
        day = row["date"]
        cur = by_day.setdefault(
            day,
            {"date": day, "problems": 0, "points": 0.0, "contests": 0, "max_rating": None, "active": False},
        )
        cur["problems"] += row["problems"] or 0
        cur["points"] = round(cur["points"] + (row["points"] or 0), 2)
        cur["contests"] += row["contests"] or 0
        if row["max_rating"] is not None:
            cur["max_rating"] = max(cur["max_rating"] or 0, row["max_rating"])
        cur["active"] = cur["active"] or bool(row["active"])
    return list(by_day.values())


def _pick_mvp(members: list[Member], summaries: dict[str, dict[str, Any]]) -> Member | None:
    ranked = [m for m in members if m.id in summaries and summaries[m.id]["points"] > 0]
    if not ranked:
        return None
    return sorted(ranked, key=lambda m: (-summaries[m.id]["points"], m.name))[0]


def _team_summaries(
    repo: Repo,
    members: list[Member],
    person_rows: dict[str, list[dict]],
    person_prev: dict[str, float],
    rating_delta: dict[str, float],
    topics: dict[str, set[str]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for team in repo.rows(S.DB_TEAMS):
        team_members = [m for m in members if team["id"] in m.team_ids]
        if not team_members:
            continue
        rows = [row for m in team_members for row in person_rows.get(m.id, [])]
        prev = sum(person_prev.get(m.id, 0.0) for m in team_members)
        summary = weekly.summarize(_aggregate(rows), prev)
        summary["rating_delta"] = sum(rating_delta.get(m.id, 0) for m in team_members)
        union: set[str] = set()
        for m in team_members:
            union |= topics.get(m.id, set())
        summary["topics_advanced"] = len(union)
        name = get_title(team, S.P_Teams.TITLE) or "(team)"
        out.append(({"id": team["id"], "name": name}, summary))
    return out


def _find_report(repo: Repo, week_begin: date) -> dict | None:
    for page in repo.rows(S.DB_WEEKLY_REPORTS):
        if get_date(page, S.P_WeeklyReports.WEEK_START) == week_begin:
            return page
    return None


def _ensure_report(
    ctx: Context,
    week_begin: date,
    label: str,
    club: dict[str, Any],
    members: list[Member],
    person_summary: dict[str, dict[str, Any]],
    teams: list[tuple[dict[str, Any], dict[str, Any]]],
    mvp: Member | None,
) -> tuple[dict, bool]:
    repo = ctx.repo
    existing = _find_report(repo, week_begin)
    body = _report_body(label, club, members, person_summary, teams, mvp)
    if existing is not None:
        # A report from an earlier run whose body never landed is filled in (F11).
        if not repo.has_children(existing["id"]):
            repo.append(existing["id"], body)
        return existing, False
    mvp_name = mvp.name if mvp else "—"
    props = {
        S.P_WeeklyReports.TITLE: nt.p_title(f"📰 Week of {week_begin.isoformat()}"),
        S.P_WeeklyReports.WEEK_START: nt.p_date(week_begin, with_time=False),
        S.P_WeeklyReports.WEEK_LABEL: nt.p_text(label),
        S.P_WeeklyReports.CLUB_POINTS: nt.p_number(club["points"]),
        S.P_WeeklyReports.CLUB_PROBLEMS: nt.p_number(club["problems"]),
        S.P_WeeklyReports.MVP: nt.p_text(mvp_name),
        S.P_WeeklyReports.HIGHLIGHTS: nt.p_text(_highlights(club, mvp, members, person_summary)),
    }
    # The body goes in as children at create time (Notion takes at most 100 per request);
    # anything beyond that is appended.
    report = repo.create(S.DB_WEEKLY_REPORTS, props, children=body[:MAX_CREATE_CHILDREN], icon=icons.icon_for(S.DB_WEEKLY_REPORTS, props))
    repo.append(report["id"], body[MAX_CREATE_CHILDREN:])
    admin = str(ctx.cfg.get(K.ADMIN_USER_ID) or "").strip()
    text = f"Weekly report ready for the week of {week_begin.isoformat()}. "
    try:
        repo.comment(report["id"], comment_rich_text(text, [admin] if admin else []))
    except Exception:  # noqa: BLE001 - a missing comment capability must not fail the job
        log.exception("could not comment on the weekly report")
    return report, True


def _highlights(
    club: dict[str, Any],
    mvp: Member | None,
    members: list[Member],
    person_summary: dict[str, dict[str, Any]],
) -> str:
    parts = [f"Club {club['points']} pts, {club['problems']} problems."]
    if mvp is not None:
        parts.append(f"MVP: {mvp.name} ({person_summary[mvp.id]['points']} pts).")
    movers = [m for m in members if m.id in person_summary and person_summary[m.id]["points_delta"] > 0]
    if movers:
        best = max(movers, key=lambda m: (person_summary[m.id]["points_delta"], m.name))
        parts.append(f"Biggest gain: {best.name} ({_signed(person_summary[best.id]['points_delta'])} pts).")
    return " ".join(parts)


def _signed(value: float) -> str:
    return f"{value:+g}"


def _report_body(
    label: str,
    club: dict[str, Any],
    members: list[Member],
    person_summary: dict[str, dict[str, Any]],
    teams: list[tuple[dict[str, Any], dict[str, Any]]],
    mvp: Member | None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [_block("heading_2", f"Week of {label}")]
    ranked = sorted(
        (m for m in members if m.id in person_summary),
        key=lambda m: (-person_summary[m.id]["points"], m.name),
    )
    blocks.append(_block("heading_3", "Leaderboard"))
    for m in ranked:
        s = person_summary[m.id]
        streak_days = int(get_number(m.page, S.P_Members.CURRENT_STREAK) or 0)
        blocks.append(
            _block(
                "numbered_list_item",
                f"{m.name} — {s['points']} pts ({_signed(s['points_delta'])}) — {s['problems']} problems — 🔥{streak_days}",
            )
        )
    blocks.append(_block("heading_3", "Teams"))
    for team, summary in sorted(teams, key=lambda item: -item[1]["points"]):
        blocks.append(_block("bulleted_list_item", f"{team['name']} — {summary['points']} pts ({_signed(summary['points_delta'])})"))
    blocks.append(_block("heading_3", "Top movers (points vs last week)"))
    movers = [m for m in ranked if person_summary[m.id]["points_delta"] > 0][:BOARD_LIMIT]
    if movers:
        for m in movers:
            blocks.append(_block("bulleted_list_item", f"{m.name}: {_signed(person_summary[m.id]['points_delta'])} pts"))
    else:
        blocks.append(_block("paragraph", "No gains over last week."))
    blocks.append(_block("heading_3", "Longest current streaks"))
    streaks = sorted(
        ((m, int(get_number(m.page, S.P_Members.CURRENT_STREAK) or 0)) for m in members),
        key=lambda item: (-item[1], item[0].name),
    )
    streaks = [item for item in streaks if item[1] > 0][:BOARD_LIMIT]
    if streaks:
        for m, days in streaks:
            blocks.append(_block("bulleted_list_item", f"{m.name}: {days} days"))
    else:
        blocks.append(_block("paragraph", "No active streaks."))
    blocks.append(_block("heading_3", "Contests attended"))
    attended = [m for m in ranked if person_summary[m.id]["contests"] > 0]
    if attended:
        for m in attended:
            blocks.append(_block("bulleted_list_item", f"{m.name}: {person_summary[m.id]['contests']}"))
    else:
        blocks.append(_block("paragraph", "No contests this week."))
    blocks.append(
        _block(
            "paragraph",
            f"Club total: {club['points']} pts, {club['problems']} problems. MVP: {mvp.name if mvp else '—'}.",
        )
    )
    return blocks


def _block(kind: str, text: str) -> dict[str, Any]:
    return {"object": "block", "type": kind, kind: {"rich_text": nt.rich_text(text)}}


def _upsert_summary(
    repo: Repo,
    name: str,
    scope: str,
    who: str | None,
    week_begin: date,
    label: str,
    summary: dict[str, Any],
    *,
    key: str,
    member_id: str | None = None,
    team_id: str | None = None,
    mvp: bool,
    report_id: str | None,
) -> None:
    props = {
        S.P_WeeklySummary.TITLE: nt.p_title(f"{name} · {label}"),
        S.P_WeeklySummary.SCOPE: nt.p_select(scope),
        S.P_WeeklySummary.WHO: nt.p_select(who),
        S.P_WeeklySummary.WEEK_START: nt.p_date(week_begin, with_time=False),
        S.P_WeeklySummary.WEEK_LABEL: nt.p_text(label),
        S.P_WeeklySummary.POINTS: nt.p_number(summary["points"]),
        S.P_WeeklySummary.PROBLEMS: nt.p_number(summary["problems"]),
        S.P_WeeklySummary.ACTIVE_DAYS: nt.p_number(summary["active_days"]),
        S.P_WeeklySummary.AVG_RATING: nt.p_number(summary["avg_rating"]),
        S.P_WeeklySummary.MAX_RATING: nt.p_number(summary["max_rating"]),
        S.P_WeeklySummary.CONTESTS: nt.p_number(summary["contests"]),
        S.P_WeeklySummary.RATING_DELTA: nt.p_number(summary.get("rating_delta", 0)),
        S.P_WeeklySummary.POINTS_DELTA: nt.p_number(summary["points_delta"]),
        S.P_WeeklySummary.TOPICS_ADVANCED: nt.p_number(summary.get("topics_advanced", 0)),
        S.P_WeeklySummary.MVP: nt.p_checkbox(mvp),
        S.P_WeeklySummary.SOURCE_KEY: nt.p_text(key),
        S.P_WeeklySummary.MEMBER: nt.p_relation([member_id] if member_id else []),
        S.P_WeeklySummary.TEAM: nt.p_relation([team_id] if team_id else []),
        S.P_WeeklySummary.REPORT: nt.p_relation([report_id] if report_id else []),
    }
    repo.upsert(S.DB_WEEKLY_SUMMARY, S.P_WeeklySummary.SOURCE_KEY, key, props, icon=icons.icon_for(S.DB_WEEKLY_SUMMARY, props))


def _award_mvp(repo: Repo, member: Member, week_begin: date, label: str, summary: dict[str, Any]) -> None:
    key = f"{MVP_BADGE}:{member.id}:{week_begin.isoformat()}"
    if repo.find(S.DB_ACHIEVEMENTS, S.P_Achievements.SOURCE_KEY, key) is not None:
        return
    friday = week_begin + timedelta(days=6)
    props = {
        S.P_Achievements.TITLE: nt.p_title(f"{MVP_BADGE} · {member.name} · {label}"),
        S.P_Achievements.WHO: nt.p_select(member.name),
        S.P_Achievements.MEMBER: nt.p_relation([member.id]),
        S.P_Achievements.BADGE: nt.p_select(MVP_BADGE),
        S.P_Achievements.TIER: nt.p_select("Gold"),
        S.P_Achievements.EARNED_ON: nt.p_date(friday, with_time=False),
        S.P_Achievements.DETAIL: nt.p_text(f"MVP of the week of {label} with {summary['points']} points"),
        S.P_Achievements.SOURCE_KEY: nt.p_text(key),
    }
    repo.create(S.DB_ACHIEVEMENTS, props, icon=icons.icon_for(S.DB_ACHIEVEMENTS, props))
