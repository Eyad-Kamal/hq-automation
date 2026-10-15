"""Job ``recompute``: derive every number the dashboards show from the raw rows.

For each active member (and then for teams and goals on full runs):

* Solved problems give per-Cairo-day stats: problems, points, max effective
  rating, and contests attended (Contest Entries with an attended status).
  Points come from the Notion "Points" formula when it has a value; otherwise
  they are computed by ``scoring.points``.
* A day is active when it has a solved problem or an attended contest.
  ``streak.compute`` over all active days gives current/best streak, freezes
  and frozen days.
* Daily Log rows for the last DAILY_LOG_DAYS days (default 60) plus today are upserted by Source Key
  "<member id>:<YYYY-MM-DD>". Minutes and Reflection are never written.
* Members get points today/this week/last week/total, problem count, streak,
  freezes and last active date. Teams get the sums of their members.
* Learning Queue rows get mastery counts and the auto tier. The queue status
  moves Queued -> Practicing when the first solve arrives. It never becomes
  Mastered automatically.
* Goals get Current; Status becomes Achieved when Current reaches Target.
* Achievements are awarded once per (badge, member) for the badges that
  ``achievements.evaluate`` returns. "Weekly MVP" is excluded here, because the
  weekly job awards it per week.
* Daily auto Topic Practice tasks get Done Count (the listed problems the member
  has solved) and become Done once Done Count reaches Target.

Team totals and goals are only recomputed on full runs (no --member filter).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import OK, WARNING, Context, JobResult
from hq.logic import achievements, goals, mastery, scoring, streak
from hq.logic.problem_ref import identities
from hq.repo import (
    Member,
    Repo,
    Topic,
    cairo_day_of,
    get_checkbox,
    get_date,
    get_formula,
    get_number,
    get_relation,
    get_select,
    get_status,
    get_text,
    get_title,
    get_url,
)
from hq.schema import ConfigKey as K
from hq.schema import Mode
from hq.timeutil import week_label, week_start

log = logging.getLogger("hq.jobs.recompute")

JOB = "recompute"
ATTENDED_STATUSES = frozenset({"Attended", "Upsolving", "Upsolved"})
COMPETENT_OR_MORE = frozenset({"Competent", "Proficient", "Master"})
ADDITIVE_GOALS = frozenset({"Problems Solved", "Points", "Contests Attended", "Topics ≥ Competent", "Problems ≥ Rating"})
NOT_RECOMPUTED_BADGES = frozenset({"Weekly MVP"})
TASK_OPEN_STATUSES = frozenset({"To Do", "Planned", "In Progress"})


@dataclass
class Solved:
    """One solved problem of one member, read from the Problems table."""

    page_id: str
    key: str
    rating: int | None
    effective: int
    mode: str
    day: date | None
    points: float
    topic_ids: list[str]


@dataclass
class DayStat:
    problems: int = 0
    points: float = 0.0
    contests: int = 0
    max_rating: int = 0


@dataclass
class MemberResult:
    stats: dict[str, Any]
    solved: list[Solved] = field(default_factory=list)


def member_ref_rating(repo: Repo, member: Member) -> int:
    """Notion's "Ref Rating" when available, else CF rating, else the level's band low, else 800."""
    formula = get_formula(member.page, S.P_Members.REF_RATING)
    if isinstance(formula, (int, float)) and not isinstance(formula, bool):
        return int(formula)
    if member.cf_rating is not None:
        return member.cf_rating
    low = repo.level_band_low(member.level_ids[0]) if member.level_ids else None
    return low if low is not None else scoring.DEFAULT_RATING


def _lc_map(cfg: dict[str, Any]) -> dict[str, int]:
    return {
        "Easy": int(num(cfg, K.LC_EASY)),
        "Medium": int(num(cfg, K.LC_MEDIUM)),
        "Hard": int(num(cfg, K.LC_HARD)),
    }


def _member_problems(
    repo: Repo,
    member: Member,
    ref: int,
    cfg: dict[str, Any],
    duplicates: list[str],
) -> list[Solved]:
    """Solved problems of one member. Rows that name the same problem are one solve: rows
    sharing a Source Key, a normalized Problem Key, or a normalized Link are grouped, the
    row with the earliest Solved On is kept, and the others are recorded in ``duplicates``
    (F1). A Warning is logged for each ignored row."""
    lc_map = _lc_map(cfg)
    upsolve = num(cfg, K.MODE_UPSOLVE)
    editorial = num(cfg, K.MODE_EDITORIAL)
    candidates: list[dict] = []
    for page in repo.rows(S.DB_PROBLEMS):
        if get_status(page, S.P_Problems.STATUS) != "Solved":
            continue
        if member.id not in get_relation(page, S.P_Problems.MEMBER):
            continue
        candidates.append(page)

    # Group rows that share any identity (union-find over the identity strings).
    parent = list(range(len(candidates)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, page in enumerate(candidates):
        idents = identities(get_text(page, S.P_Problems.PROBLEM_KEY), get_url(page, S.P_Problems.LINK))
        source = get_text(page, S.P_Problems.SOURCE_KEY).strip().lower()
        if source:
            idents.add(f"src:{source}")
        for ident in idents:
            if ident in owner:
                parent[find(i)] = find(owner[ident])
            else:
                owner[ident] = i

    groups: dict[int, list[int]] = {}
    for i in range(len(candidates)):
        groups.setdefault(find(i), []).append(i)

    def solved_on_key(i: int) -> tuple[int, str]:
        when = get_date(candidates[i], S.P_Problems.SOLVED_ON)
        return (0, when.isoformat()) if when is not None else (1, "")

    keep: set[int] = set()
    for members_of_group in groups.values():
        first = min(members_of_group, key=solved_on_key)
        keep.add(first)
        for i in members_of_group:
            if i == first:
                continue
            label = get_text(candidates[i], S.P_Problems.SOURCE_KEY) or get_text(candidates[i], S.P_Problems.PROBLEM_KEY)
            duplicates.append(f"Problems {label}")
            log.warning("duplicate solve ignored for %s: %s (kept the earliest Solved On)", member.name, label)

    lc = _lc_map(cfg)
    solved: list[Solved] = []
    for i, page in enumerate(candidates):
        if i not in keep:
            continue
        rating_value = get_number(page, S.P_Problems.RATING)
        rating = int(rating_value) if rating_value is not None else None
        difficulty = get_select(page, S.P_Problems.DIFFICULTY)
        effective = scoring.effective_rating(rating, difficulty, lc)
        mode = get_select(page, S.P_Problems.MODE) or Mode.PRACTICE
        at_solve = get_number(page, S.P_Problems.RATING_AT_SOLVE)
        ref_here = int(at_solve) if at_solve is not None else ref
        formula_points = get_formula(page, S.P_Problems.POINTS)
        if isinstance(formula_points, (int, float)) and not isinstance(formula_points, bool):
            points = float(formula_points)
        else:
            points = scoring.points(effective, ref_here, mode, upsolve=upsolve, editorial=editorial)
        solved_on = get_date(page, S.P_Problems.SOLVED_ON)
        solved.append(
            Solved(
                page_id=page["id"],
                key=get_text(page, S.P_Problems.PROBLEM_KEY),
                rating=rating,
                effective=effective,
                mode=mode,
                day=cairo_day_of(solved_on),
                points=points,
                topic_ids=get_relation(page, S.P_Problems.TOPICS),
            )
        )
    return solved


def _attended_entries(repo: Repo, member: Member, duplicates: list[str]) -> list[dict]:
    """Attended Contest Entries of one member, one per (contest, member) (F1)."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for entry in repo.rows(S.DB_CONTEST_ENTRIES):
        if member.id not in get_relation(entry, S.P_ContestEntries.MEMBER):
            continue
        if get_status(entry, S.P_ContestEntries.STATUS) not in ATTENDED_STATUSES:
            continue
        contests = tuple(get_relation(entry, S.P_ContestEntries.CONTEST)) or (get_title(entry, S.P_ContestEntries.TITLE),)
        if contests in seen:
            duplicates.append(f"Contest Entries {get_title(entry, S.P_ContestEntries.TITLE)}")
            continue
        seen.add(contests)
        out.append(entry)
    return out


def _day_stats(solved: list[Solved], contest_days: Counter) -> dict[date, DayStat]:
    stats: dict[date, DayStat] = {}
    for item in solved:
        if item.day is None:
            continue
        stat = stats.setdefault(item.day, DayStat())
        stat.problems += 1
        stat.points = round(stat.points + item.points, 2)
        stat.max_rating = max(stat.max_rating, item.effective)
    for day, count in contest_days.items():
        if day is None:
            continue
        stats.setdefault(day, DayStat()).contests += count
    return stats


def daily_target(member: Member, cfg: dict[str, Any]) -> int:
    if member.daily_target is not None:
        return member.daily_target
    return int(num(cfg, K.DEFAULT_DAILY_TARGET))


def daily_log_props(
    member: Member,
    team: str | None,
    day: date,
    stat: DayStat,
    *,
    active: bool,
    frozen: bool,
    streak_value: int,
    target: int,
) -> dict[str, Any]:
    return {
        S.P_DailyLog.TITLE: nt.p_title(f"{member.name} · {day.isoformat()}"),
        S.P_DailyLog.MEMBER: nt.p_relation([member.id]),
        S.P_DailyLog.WHO: nt.p_select(member.name),
        S.P_DailyLog.TEAM: nt.p_select(team),
        S.P_DailyLog.DATE: nt.p_date(day, with_time=False),
        S.P_DailyLog.WEEK: nt.p_text(week_label(day)),
        S.P_DailyLog.PROBLEMS: nt.p_number(stat.problems),
        S.P_DailyLog.POINTS: nt.p_number(round(stat.points, 2)),
        S.P_DailyLog.CONTESTS: nt.p_number(stat.contests),
        S.P_DailyLog.MAX_RATING_SOLVED: nt.p_number(stat.max_rating or None),
        S.P_DailyLog.ACTIVE: nt.p_checkbox(active),
        S.P_DailyLog.FROZEN: nt.p_checkbox(frozen),
        S.P_DailyLog.STREAK: nt.p_number(streak_value),
        S.P_DailyLog.TARGET: nt.p_number(target),
        S.P_DailyLog.SOURCE_KEY: nt.p_text(f"{member.id}:{day.isoformat()}"),
    }


def ensure_daily_log(ctx: Context, member: Member, day: date) -> dict:
    """Create today's Daily Log row for a member if it does not exist (zeros until recompute runs)."""
    key = f"{member.id}:{day.isoformat()}"
    existing = ctx.repo.find(S.DB_DAILY_LOG, S.P_DailyLog.SOURCE_KEY, key)
    if existing is not None:
        return existing
    team = ctx.repo.team_name(member.team_ids[0]) if member.team_ids else None
    props = daily_log_props(
        member,
        team,
        day,
        DayStat(),
        active=False,
        frozen=False,
        streak_value=0,
        target=daily_target(member, ctx.cfg),
    )
    return ctx.repo.create(S.DB_DAILY_LOG, props)


def run(ctx: Context) -> JobResult:
    repo = ctx.repo
    writes_before = repo.writes
    today = ctx.today
    topics = {t.id: t for t in repo.topics()}
    members = ctx.active_members()
    results: dict[str, MemberResult] = {}
    errors: list[str] = []
    duplicates: list[str] = []

    who_fixed = _fill_who_team(repo, members)
    for member in members:
        try:
            results[member.id] = _recompute_member(ctx, member, topics, today, duplicates)
        except Exception as exc:  # noqa: BLE001 - one member must not stop the others
            log.exception("recompute failed for %s", member.name)
            errors.append(f"{member.name}: {type(exc).__name__}: {exc}")

    if ctx.member_filter is None:
        _update_teams(repo)
        _update_goals(ctx, repo, results, members)

    items = repo.writes - writes_before
    details = [
        f"members recomputed: {len(results)}/{len(members)}",
        f"writes: {items}",
        f"Who/Team filled or corrected: {who_fixed}",
        *errors,
    ]
    if duplicates:
        details.append(f"duplicate rows ignored ({len(duplicates)}): " + "; ".join(sorted(set(duplicates))))
    result = WARNING if (errors or duplicates) else OK
    return JobResult(job=JOB, result=result, items=items, details="\n".join(details))


def _recompute_member(
    ctx: Context,
    member: Member,
    topics: dict[str, Topic],
    today: date,
    duplicates: list[str],
) -> MemberResult:
    repo = ctx.repo
    cfg = ctx.cfg
    ref = member_ref_rating(repo, member)
    solved = _member_problems(repo, member, ref, cfg, duplicates)
    entries = _attended_entries(repo, member, duplicates)
    contest_days = Counter(cairo_day_of(get_date(e, S.P_ContestEntries.DATE)) for e in entries)
    stats = _day_stats(solved, contest_days)
    active = {d for d, s in stats.items() if s.problems > 0 or s.contests > 0}
    freeze_every = int(num(cfg, K.FREEZE_EVERY))
    freeze_max = int(num(cfg, K.FREEZE_MAX))
    current_state = streak.compute(active, today, freeze_every, freeze_max)

    # Daily Log: the window plus today. Minutes and Reflection are never touched.
    team = repo.team_name(member.team_ids[0]) if member.team_ids else None
    target = daily_target(member, cfg)
    window_days = max(0, int(num(cfg, K.DAILY_LOG_DAYS)))
    for offset in range(-window_days, 1):
        day = today + timedelta(days=offset)
        stat = stats.get(day, DayStat())
        day_streak = streak.compute(active, day, freeze_every, freeze_max).current
        props = daily_log_props(
            member,
            team,
            day,
            stat,
            active=day in active,
            frozen=day in current_state.frozen_days,
            streak_value=day_streak,
            target=target,
        )
        repo.upsert(S.DB_DAILY_LOG, S.P_DailyLog.SOURCE_KEY, f"{member.id}:{day.isoformat()}", props)

    # Member totals.
    week = week_start(today)
    prev_week = week - timedelta(days=7)
    points_today = stats[today].points if today in stats else 0.0
    this_week = _sum_points(stats, week, today)
    last_week = _sum_points(stats, prev_week, week - timedelta(days=1))
    total = round(sum(s.points for s in solved), 2)
    last_active = max(active) if active else None
    repo.sync(
        S.DB_MEMBERS,
        member.page,
        {
            S.P_Members.POINTS_TODAY: nt.p_number(round(points_today, 2)),
            S.P_Members.POINTS_THIS_WEEK: nt.p_number(this_week),
            S.P_Members.POINTS_LAST_WEEK: nt.p_number(last_week),
            S.P_Members.TOTAL_POINTS: nt.p_number(total),
            S.P_Members.PROBLEMS_TOTAL: nt.p_number(len(solved)),
            S.P_Members.CURRENT_STREAK: nt.p_number(current_state.current),
            S.P_Members.BEST_STREAK: nt.p_number(current_state.best),
            S.P_Members.FREEZES: nt.p_number(current_state.freezes),
            S.P_Members.LAST_ACTIVE: nt.p_date(last_active, with_time=False),
        },
    )

    tiers = _update_learning_queue(repo, member, solved, topics, cfg)
    rating_for_gates = member.cf_rating if member.cf_rating is not None else ref
    stat_values = _member_stats(
        repo,
        member,
        solved,
        entries,
        tiers,
        topics,
        cfg,
        today,
        rating_for_gates,
        current_state,
    )
    _award_badges(repo, member, stat_values, today)
    _sync_practice_tasks(repo, member)
    return MemberResult(stats=stat_values, solved=solved)


def _fill_who_team(repo: Repo, members: list[Member]) -> int:
    """Set "Who" (and the Problems "Team" select) from the linked member, so rows created
    by hand (no Who) are grouped under the right person in charts. Team-mode Contest
    Entries and Team-scope Goals with no Member take the team name as Who. Only differing
    values are written. Returns the number of rows changed."""
    by_id = {m.id: m for m in members}
    team_names = {t["id"]: get_title(t, S.P_Teams.TITLE) for t in repo.rows(S.DB_TEAMS)}

    def member_of(row: dict, prop: str) -> Member | None:
        for member_id in get_relation(row, prop):
            if member_id in by_id:
                return by_id[member_id]
        return None

    def team_of(member: Member) -> str | None:
        return team_names.get(member.team_ids[0]) if member.team_ids else None

    changed = 0
    for row in repo.rows(S.DB_PROBLEMS):
        member = member_of(row, S.P_Problems.MEMBER)
        if member is None:
            continue
        props = {S.P_Problems.WHO: nt.p_select(member.name)}
        if team_of(member):  # a member without a team never clears a Team set by hand
            props[S.P_Problems.TEAM] = nt.p_select(team_of(member))
        changed += 1 if repo.sync(S.DB_PROBLEMS, row, props) else 0
    for row in repo.rows(S.DB_CONTEST_ENTRIES):
        member = member_of(row, S.P_ContestEntries.MEMBER)
        if member is not None:
            who = member.name
        elif get_select(row, S.P_ContestEntries.MODE) == "Team" and get_relation(row, S.P_ContestEntries.TEAM):
            who = team_names.get(get_relation(row, S.P_ContestEntries.TEAM)[0])
        else:
            continue
        if who:
            changed += 1 if repo.sync(S.DB_CONTEST_ENTRIES, row, {S.P_ContestEntries.WHO: nt.p_select(who)}) else 0
    for row in repo.rows(S.DB_GOALS):
        member = member_of(row, S.P_Goals.MEMBER)
        team_ids = get_relation(row, S.P_Goals.TEAM)
        if member is not None:
            who = member.name
        elif get_select(row, S.P_Goals.SCOPE) == "Team" and team_ids:
            who = team_names.get(team_ids[0])
        else:
            continue
        if who:
            changed += 1 if repo.sync(S.DB_GOALS, row, {S.P_Goals.WHO: nt.p_select(who)}) else 0
    for db, prop, who_prop in (
        (S.DB_LEARNING_QUEUE, S.P_LearningQueue.MEMBER, S.P_LearningQueue.WHO),
        (S.DB_ACHIEVEMENTS, S.P_Achievements.MEMBER, S.P_Achievements.WHO),
        (S.DB_RATING_HISTORY, S.P_RatingHistory.MEMBER, S.P_RatingHistory.WHO),
    ):
        for row in repo.rows(db):
            member = member_of(row, prop)
            if member is None:
                continue
            changed += 1 if repo.sync(db, row, {who_prop: nt.p_select(member.name)}) else 0
    return changed


def _sum_points(stats: dict[date, DayStat], start: date, end: date) -> float:
    total = 0.0
    day = start
    while day <= end:
        if day in stats:
            total += stats[day].points
        day += timedelta(days=1)
    return round(total, 2)


def _update_learning_queue(
    repo: Repo,
    member: Member,
    solved: list[Solved],
    topics: dict[str, Topic],
    cfg: dict[str, Any],
) -> dict[str, str]:
    """Update the member's queue rows. Returns topic id -> effective tier (override first)."""
    tiers: dict[str, str] = {}
    for row in repo.rows(S.DB_LEARNING_QUEUE):
        if member.id not in get_relation(row, S.P_LearningQueue.MEMBER):
            continue
        topic_ids = get_relation(row, S.P_LearningQueue.TOPIC)
        topic = topics.get(topic_ids[0]) if topic_ids else None
        if topic is None or not topic.has_band:
            continue
        solves = [(p.effective, p.mode) for p in solved if topic.id in p.topic_ids]
        result = mastery.compute(
            solves,
            int(topic.band_low),  # type: ignore[arg-type]
            int(topic.band_high),  # type: ignore[arg-type]
            get_checkbox(row, S.P_LearningQueue.THEORY_DONE),
            cfg,
        )
        props: dict[str, Any] = {
            S.P_LearningQueue.PROBLEMS_COUNT: nt.p_number(result.count),
            S.P_LearningQueue.BELOW_BAND: nt.p_number(result.below),
            S.P_LearningQueue.IN_BAND: nt.p_number(result.in_band),
            S.P_LearningQueue.ABOVE_BAND: nt.p_number(result.above),
            S.P_LearningQueue.MAX_RATING: nt.p_number(result.max_rating),
            S.P_LearningQueue.MASTERY_POINTS: nt.p_number(result.points),
            S.P_LearningQueue.AUTO_TIER: nt.p_select(result.tier),
        }
        if result.count > 0 and get_status(row, S.P_LearningQueue.QUEUE_STATUS) == "Queued":
            props[S.P_LearningQueue.QUEUE_STATUS] = nt.p_status("Practicing")
        repo.sync(S.DB_LEARNING_QUEUE, row, props)
        tiers[topic.id] = get_select(row, S.P_LearningQueue.COACH_OVERRIDE) or result.tier
    return tiers


def _member_stats(
    repo: Repo,
    member: Member,
    solved: list[Solved],
    entries: list[dict],
    tiers: dict[str, str],
    topics: dict[str, Topic],
    cfg: dict[str, Any],
    today: date,
    rating_for_gates: int,
    current_state: streak.StreakResult,
) -> dict[str, Any]:
    month = (today.year, today.month)
    contests_month = 0
    for entry in entries:
        day = cairo_day_of(get_date(entry, S.P_ContestEntries.DATE))
        if day is not None and (day.year, day.month) == month:
            contests_month += 1
    topics_mastered = sum(
        1
        for row in repo.rows(S.DB_LEARNING_QUEUE)
        if member.id in get_relation(row, S.P_LearningQueue.MEMBER)
        and get_status(row, S.P_LearningQueue.QUEUE_STATUS) == "Mastered"
    )
    templates = sum(1 for t in repo.rows(S.DB_CODE_TEMPLATES) if member.id in get_relation(t, S.P_CodeTemplates.AUTHOR))
    resources = sum(
        1 for r in repo.rows(S.DB_RESOURCES) if member.id in get_relation(r, S.P_Resources.ADDED_BY_MEMBER)
    )
    mvp_weeks = sum(
        1
        for s in repo.rows(S.DB_WEEKLY_SUMMARY)
        if member.id in get_relation(s, S.P_WeeklySummary.MEMBER) and get_checkbox(s, S.P_WeeklySummary.MVP)
    )
    ratings = [p.rating for p in solved if p.rating is not None]
    return {
        "problems_total": len(solved),
        "points_total": round(sum(p.points for p in solved), 2),
        "max_rating_solved": max(ratings) if ratings else 0,
        "current_streak": current_state.current,
        "best_streak": current_state.best,
        "cf_rating": member.cf_rating or 0,
        "contests_month": contests_month,
        "contests_total": len(entries),
        "upsolved_full_contests": sum(
            1 for e in entries if get_status(e, S.P_ContestEntries.STATUS) == "Upsolved"
        ),
        "team_contests": sum(1 for e in entries if get_select(e, S.P_ContestEntries.MODE) == "Team"),
        "templates_added": templates,
        "resources_added": resources,
        "topics_competent": sum(1 for tier in tiers.values() if tier in COMPETENT_OR_MORE),
        "topics_mastered": topics_mastered,
        "levels_complete": _levels_complete(member, tiers, topics, cfg, rating_for_gates),
        "weekly_mvp": mvp_weeks,
        "solved_ratings": [p.effective for p in solved],
    }


def _levels_complete(
    member: Member,
    tiers: dict[str, str],
    topics: dict[str, Topic],
    cfg: dict[str, Any],
    rating: int,
) -> int:
    """Levels where every core topic is Competent or better AND the rating gate is met."""
    by_level: dict[int, list[Topic]] = {}
    for topic in topics.values():
        if topic.core and topic.level_no:
            by_level.setdefault(topic.level_no, []).append(topic)
    done = 0
    for level_no, core in by_level.items():
        if not all(tiers.get(t.id) in COMPETENT_OR_MORE for t in core):
            continue
        gate_key = f"GATE_L{level_no}"
        if gate_key in cfg and rating < num(cfg, gate_key):
            continue
        done += 1
    return done


def _award_badges(repo: Repo, member: Member, stat_values: dict[str, Any], today: date) -> None:
    owned = [a for a in repo.rows(S.DB_ACHIEVEMENTS) if member.id in get_relation(a, S.P_Achievements.MEMBER)]
    already = {get_select(a, S.P_Achievements.BADGE) for a in owned} | NOT_RECOMPUTED_BADGES
    for badge, tier, detail in achievements.evaluate(stat_values, {b for b in already if b}):
        key = f"{badge}:{member.id}"
        if repo.find(S.DB_ACHIEVEMENTS, S.P_Achievements.SOURCE_KEY, key) is not None:
            continue
        repo.create(
            S.DB_ACHIEVEMENTS,
            {
                S.P_Achievements.TITLE: nt.p_title(f"{badge} · {member.name}"),
                S.P_Achievements.WHO: nt.p_select(member.name),
                S.P_Achievements.MEMBER: nt.p_relation([member.id]),
                S.P_Achievements.BADGE: nt.p_select(badge),
                S.P_Achievements.TIER: nt.p_select(tier),
                S.P_Achievements.EARNED_ON: nt.p_date(today, with_time=False),
                S.P_Achievements.DETAIL: nt.p_text(detail),
                S.P_Achievements.SOURCE_KEY: nt.p_text(key),
            },
        )


def _sync_practice_tasks(repo: Repo, member: Member) -> None:
    """Done Count and Done status for the member's auto Topic Practice tasks."""
    solved_keys = {
        get_text(p, S.P_Problems.PROBLEM_KEY)
        for p in repo.rows(S.DB_PROBLEMS)
        if get_status(p, S.P_Problems.STATUS) == "Solved" and member.id in get_relation(p, S.P_Problems.MEMBER)
    }
    for task in repo.rows(S.DB_TASKS):
        if get_select(task, S.P_Tasks.TYPE) != "Topic Practice" or get_select(task, S.P_Tasks.ORIGIN) != "Auto":
            continue
        if member.id not in get_relation(task, S.P_Tasks.ASSIGNEE):
            continue
        keys = _task_problem_keys(task)
        done = sum(1 for key in keys if key in solved_keys)
        props: dict[str, Any] = {S.P_Tasks.DONE_COUNT: nt.p_number(done)}
        target = get_number(task, S.P_Tasks.TARGET)
        status = get_status(task, S.P_Tasks.STATUS)
        if target and done >= target and status in TASK_OPEN_STATUSES:
            props[S.P_Tasks.STATUS] = nt.p_status("Done")
        repo.sync(S.DB_TASKS, task, props)


def _task_problem_keys(task: dict) -> list[str]:
    """Problem keys listed in a task's Links, one "<key> | <url>" per line."""
    text = get_text(task, S.P_Tasks.LINKS)
    keys: list[str] = []
    for line in text.splitlines():
        key = line.split("|", 1)[0].strip()
        if key:
            keys.append(key)
    return keys


def _update_teams(repo: Repo) -> None:
    active = [m for m in repo.members() if m.active]
    for team in repo.rows(S.DB_TEAMS):
        members = [m for m in active if team["id"] in m.team_ids]
        if not members:
            continue
        this_week = round(sum(get_number(m.page, S.P_Members.POINTS_THIS_WEEK) or 0 for m in members), 2)
        last_week = round(sum(get_number(m.page, S.P_Members.POINTS_LAST_WEEK) or 0 for m in members), 2)
        total = round(sum(get_number(m.page, S.P_Members.TOTAL_POINTS) or 0 for m in members), 2)
        repo.sync(
            S.DB_TEAMS,
            team,
            {
                S.P_Teams.POINTS_THIS_WEEK: nt.p_number(this_week),
                S.P_Teams.POINTS_LAST_WEEK: nt.p_number(last_week),
                S.P_Teams.TOTAL_POINTS: nt.p_number(total),
            },
        )


def _update_goals(ctx: Context, repo: Repo, results: dict[str, MemberResult], members: list[Member]) -> None:
    by_id = {m.id: m for m in members}
    for goal in repo.rows(S.DB_GOALS):
        metric = get_select(goal, S.P_Goals.METRIC) or ""
        threshold_value = get_number(goal, S.P_Goals.THRESHOLD_RATING)
        threshold = int(threshold_value) if threshold_value is not None else None
        try:
            if get_select(goal, S.P_Goals.SCOPE) == "Team":
                team_ids = get_relation(goal, S.P_Goals.TEAM)
                if not team_ids:
                    continue
                values = [
                    goals.current_value(metric, results[m.id].stats, threshold)
                    for m in members
                    if team_ids[0] in m.team_ids and m.id in results
                ]
                if not values:
                    continue
                current = sum(values) if metric in ADDITIVE_GOALS else max(values)
            else:
                member_ids = get_relation(goal, S.P_Goals.MEMBER)
                if not member_ids or member_ids[0] not in results or member_ids[0] not in by_id:
                    continue
                current = goals.current_value(metric, results[member_ids[0]].stats, threshold)
        except ValueError as exc:
            log.warning("goal %s skipped: %s", get_title(goal, S.P_Goals.TITLE), exc)
            continue
        props: dict[str, Any] = {S.P_Goals.CURRENT: nt.p_number(current)}
        target = get_number(goal, S.P_Goals.TARGET)
        status = get_status(goal, S.P_Goals.STATUS)
        deadline = get_date(goal, S.P_Goals.DEADLINE)
        if target is not None and status == "Active":
            if current >= target:
                props[S.P_Goals.STATUS] = nt.p_status("Achieved")
            elif deadline is not None and cairo_day_of(deadline) < ctx.today:
                # Past its deadline and still short of the target (F10).
                props[S.P_Goals.STATUS] = nt.p_status("Missed")
        repo.sync(S.DB_GOALS, goal, props)
