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
* Before mastery, each member's solves are routed to Topics (see ``_route_member_topics``):
  same-band topics are filled in roadmap order. Script-owned rows get Topics and "Auto Topics"
  rewritten (at most TOPIC_ROUTE_PER_RUN per run); user-edited rows are kept as they are.
* Goals get Current; Status becomes Achieved when Current reaches Target. Topic goals
  (Topic Problems, Topic Tier, Level Topics ≥ Competent) use the goal's Linked Topics or
  Linked Level; a goal without the link it needs is skipped with a warning.
* Achievements are awarded once per (badge, member) for the badges that
  ``achievements.evaluate`` returns. "Weekly MVP" is excluded here, because the
  weekly job awards it per week.
* Daily auto Topic Practice tasks get Done Count (the listed problems the member
  has solved) and become Done once Done Count reaches Target.
* Seasons (nothing is deleted): Members get Season Points and Season Solved (deduped solves
  on or after the member's effective season start). The "In Season" checkboxes of Problems,
  Daily Log, Contest Entries and Weekly Summary are set by ``_season_flags``, at most
  SEASON_FLAG_PER_RUN writes per run. Points per day and Cumulative Points stay all-time.
  See ``hq.logic.season`` and README "Seasons".

Team totals and goals are only recomputed on full runs (no --member filter).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from hq import icons
from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import OK, WARNING, Context, JobResult
from hq.logic import achievements, goals, mastery, scoring, streak, topic_map
from hq.logic.problem_ref import identities
from hq.logic.topic_map import TopicInfo
from hq.logic.season import Season, in_season
from hq.repo import (
    Member,
    Repo,
    Topic,
    cairo_day_of,
    get_checkbox,
    get_date,
    get_formula,
    get_multi,
    get_number,
    get_relation,
    get_select,
    get_status,
    get_text,
    get_title,
    get_url,
    is_lc_daily_task,
)
from hq.schema import ConfigKey as K
from hq.schema import Mode
from hq.timeutil import week_label, week_start

log = logging.getLogger("hq.jobs.recompute")

JOB = "recompute"
ATTENDED_STATUSES = frozenset({"Attended", "Upsolving", "Upsolved"})
COMPETENT_OR_MORE = frozenset({"Competent", "Proficient", "Master"})
ADDITIVE_GOALS = frozenset(
    {
        "Problems Solved",
        "Points",
        "Contests Attended",
        "Topics ≥ Competent",
        "Problems ≥ Rating",
        goals.TOPIC_PROBLEMS,
        goals.LEVEL_TOPICS_COMPETENT,
    }
)  # Team scope sums these; "Topic Tier" takes the best member's tier (max).
NOT_RECOMPUTED_BADGES = frozenset({"Weekly MVP"})
TASK_OPEN_STATUSES = frozenset({"To Do", "Planned", "In Progress"})
# (database, member relation, date property, In Season property, Mode property or None).
# A row with no member, or a Contest Entry whose Mode is Team, uses the global season start.
SEASON_FLAG_SPECS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (S.DB_PROBLEMS, S.P_Problems.MEMBER, S.P_Problems.SOLVED_ON, S.P_Problems.IN_SEASON, None),
    (S.DB_DAILY_LOG, S.P_DailyLog.MEMBER, S.P_DailyLog.DATE, S.P_DailyLog.IN_SEASON, None),
    (
        S.DB_CONTEST_ENTRIES,
        S.P_ContestEntries.MEMBER,
        S.P_ContestEntries.DATE,
        S.P_ContestEntries.IN_SEASON,
        S.P_ContestEntries.MODE,
    ),
    (S.DB_WEEKLY_SUMMARY, S.P_WeeklySummary.MEMBER, S.P_WeeklySummary.WEEK_START, S.P_WeeklySummary.IN_SEASON, None),
)


@dataclass
class Solved:
    """One solved problem of one member, read from the Problems table.

    ``topic_ids`` starts as the row's current Topics and is replaced by the routed topics
    for script-owned rows (see ``_route_member_topics``) before mastery is computed.
    """

    page_id: str
    key: str
    rating: int | None
    effective: int
    mode: str
    day: date | None
    points: float
    topic_ids: list[str]
    page: dict = field(default_factory=dict)
    solved_on: str = ""  # ISO text of Solved On ("" when empty); the routing order key
    cf_tags: tuple[str, ...] = ()
    auto_topics: tuple[str, ...] = ()  # ids in the Auto Topics text, in order
    imported: bool = False
    current_topics: tuple[str, ...] = ()  # the row's Topics as read, before routing
    owned: bool = False  # script-owned (routing may rewrite Topics)


@dataclass
class TopicRouting:
    """Per-run state of the topic router: the write budget and what was written."""

    budget: int
    enabled: bool  # False when the Problems schema has no "Auto Topics" (no writes)
    written: int = 0
    capped: bool = False


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


def season_rules(ctx: Context) -> Season:
    """The global season start (Config SEASON_START) and every member's own Season Start."""
    own = {m.id: cairo_day_of(get_date(m.page, S.P_Members.SEASON_START)) for m in ctx.repo.members()}
    return Season(start=ctx.cfg.get(K.SEASON_START), own_starts=own)


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
        current_topics = tuple(get_relation(page, S.P_Problems.TOPICS))
        solved.append(
            Solved(
                page_id=page["id"],
                key=get_text(page, S.P_Problems.PROBLEM_KEY),
                rating=rating,
                effective=effective,
                mode=mode,
                day=cairo_day_of(solved_on),
                points=points,
                topic_ids=list(current_topics),
                page=page,
                solved_on=solved_on.isoformat() if solved_on is not None else "",
                cf_tags=tuple(get_multi(page, S.P_Problems.CF_TAGS)),
                auto_topics=_parse_ids(get_text(page, S.P_Problems.AUTO_TOPICS)),
                imported=get_checkbox(page, S.P_Problems.IMPORTED),
                current_topics=current_topics,
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
    cumulative: float | None = None,
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
        S.P_DailyLog.CUMULATIVE_POINTS: nt.p_number(None if cumulative is None else round(cumulative, 2)),
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
    return ctx.repo.create(S.DB_DAILY_LOG, props, icon=icons.icon_for(S.DB_DAILY_LOG, props))


def _cumulative_points(stats: dict[date, DayStat], day: date) -> float:
    """Total points of every solved problem up to and including ``day`` (solves without a date are not counted)."""
    return round(sum(stat.points for stday, stat in stats.items() if stday <= day), 2)


def _upsert_daily_log(repo: Repo, member: Member, day: date, props: dict[str, Any], *, active: bool, frozen: bool) -> None:
    """Create or sync one Daily Log row, and keep its state icon (streak / daily / freeze) current."""
    key = f"{member.id}:{day.isoformat()}"
    page = repo.find(S.DB_DAILY_LOG, S.P_DailyLog.SOURCE_KEY, key)
    if page is None:
        repo.create(S.DB_DAILY_LOG, props, icon=icons.icon_for(S.DB_DAILY_LOG, props))
        return
    repo.sync(S.DB_DAILY_LOG, page, props)
    icons.daily_log_state(repo, page, active=active, frozen=frozen)


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
    season = season_rules(ctx)
    routing = TopicRouting(
        budget=max(0, int(num(ctx.cfg, K.TOPIC_ROUTE_PER_RUN))),
        enabled=_auto_topics_exists(repo),
    )
    if not routing.enabled:
        log.warning('Problems property "Auto Topics" is missing; topic writes are skipped this run')
    for member in members:
        try:
            results[member.id] = _recompute_member(ctx, member, topics, today, duplicates, season, routing)
        except Exception as exc:  # noqa: BLE001 - one member must not stop the others
            log.exception("recompute failed for %s", member.name)
            errors.append(f"{member.name}: {type(exc).__name__}: {exc}")
    flags_written, flags_capped = _season_flags(ctx, season)

    if ctx.member_filter is None:
        _update_teams(repo)
        _update_goals(ctx, repo, results, members)

    items = repo.writes - writes_before
    details = [
        f"members recomputed: {len(results)}/{len(members)}",
        f"writes: {items}",
        f"Who/Team filled or corrected: {who_fixed}",
        f"season start: {season.start.isoformat() if season.start else 'none'}; "
        f"in-season flags changed: {flags_written} (capped: {'yes' if flags_capped else 'no'})",
        f"topics routed: {routing.written} (capped: {'yes' if routing.capped else 'no'})",
        *errors,
    ]
    if not routing.enabled:
        details.append('warning: Problems property "Auto Topics" is missing; topic writes skipped')
    if duplicates:
        details.append(f"duplicate rows ignored ({len(duplicates)}): " + "; ".join(sorted(set(duplicates))))
    result = WARNING if (errors or duplicates) else OK
    return JobResult(job=JOB, result=result, items=items, details="\n".join(details))


def _season_flag_changes(ctx: Context, season: Season, scope: set[str] | None) -> list[tuple[str, dict, str, bool]]:
    """Rows whose In Season checkbox differs from the wanted value, as (database, row, flag property,
    wanted). ``scope`` (member ids of a --member run) limits the rows; None means every row."""
    changes: list[tuple[str, dict, str, bool]] = []
    for db, member_prop, date_prop, flag_prop, mode_prop in SEASON_FLAG_SPECS:
        for row in ctx.repo.rows(db):
            member_ids = get_relation(row, member_prop)
            member_id = member_ids[0] if member_ids else None
            if mode_prop is not None and get_select(row, mode_prop) == "Team":
                member_id = None
            if scope is not None and member_id not in scope:
                continue
            wanted = in_season(cairo_day_of(get_date(row, date_prop)), season.start_for(member_id))
            if get_checkbox(row, flag_prop) != wanted:
                changes.append((db, row, flag_prop, wanted))
    return changes


def _season_flags(ctx: Context, season: Season) -> tuple[int, bool]:
    """Write the wrong In Season checkboxes, at most SEASON_FLAG_PER_RUN per run (the rest on later
    runs). Returns (checkboxes written, whether the cap left some for later)."""
    scope = None if ctx.member_filter is None else {m.id for m in ctx.members()}
    budget = max(0, int(num(ctx.cfg, K.SEASON_FLAG_PER_RUN)))
    changes = _season_flag_changes(ctx, season, scope)
    written = 0
    for db, row, flag_prop, wanted in changes[:budget]:
        if ctx.repo.sync(db, row, {flag_prop: nt.p_checkbox(wanted)}):
            written += 1
    return written, len(changes) > budget


def _recompute_member(
    ctx: Context,
    member: Member,
    topics: dict[str, Topic],
    today: date,
    duplicates: list[str],
    season: Season,
    routing: TopicRouting,
) -> MemberResult:
    repo = ctx.repo
    cfg = ctx.cfg
    ref = member_ref_rating(repo, member)
    solved = _member_problems(repo, member, ref, cfg, duplicates)
    infos = [t.info() for t in topics.values() if t.has_band]
    _route_member_topics(repo, member, solved, infos, cfg, routing)
    season_solves = [p for p in solved if in_season(p.day, season.start_for(member.id))]
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
            cumulative=_cumulative_points(stats, day),
        )
        _upsert_daily_log(repo, member, day, props, active=day in active, frozen=day in current_state.frozen_days)

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
            S.P_Members.SEASON_POINTS: nt.p_number(round(sum(p.points for p in season_solves), 2)),
            S.P_Members.SEASON_SOLVED: nt.p_number(len(season_solves)),
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


def _parse_ids(text: str) -> tuple[str, ...]:
    """Topic ids of a comma-separated text (the Auto Topics format), in order, without blanks."""
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _auto_topics_exists(repo: Repo) -> bool:
    """True when the Problems schema has the "Auto Topics" property. A failed read counts as missing."""
    try:
        return S.P_Problems.AUTO_TOPICS in repo.data_source_properties(S.DB_PROBLEMS)
    except Exception:  # noqa: BLE001 - a schema read must not stop the recompute
        log.warning("could not read the Problems schema; topic writes are skipped this run", exc_info=True)
        return False


def _script_owned(solved: Solved, infos: list[TopicInfo]) -> bool:
    """Whether recompute may rewrite this row's Topics (the ownership rules, in order):

    (a) Auto Topics is non-empty and equals the row's current Topics set;
    (b) Auto Topics is empty, the row is Imported, and its current Topics equal what the old
        ``map_topics`` gives for its CF Tags and effective rating (adopts imported rows);
    (c) Topics is empty and the row has CF Tags.
    """
    current = set(solved.current_topics)
    if solved.auto_topics and set(solved.auto_topics) == current:
        return True
    if not solved.auto_topics and solved.imported:
        if current == set(topic_map.map_topics(solved.cf_tags, solved.effective, infos)):
            return True
    return bool(not current and solved.cf_tags)


def _route_member_topics(
    repo: Repo,
    member: Member,
    solved: list[Solved],
    infos: list[TopicInfo],
    cfg: dict[str, Any],
    routing: TopicRouting,
) -> None:
    """Route each deduped solve of one member to topics in roadmap order (see
    ``topic_map.route_topics_sequential``), replace ``topic_ids`` of the script-owned rows with
    the routed ids, and write Topics + Auto Topics of those rows whose value changes (at most
    ``routing.budget`` rows per run). User-owned rows keep their Topics and still count toward
    running mastery."""
    if not infos:
        return
    overrides: dict[str, str] = {}
    for row in repo.rows(S.DB_LEARNING_QUEUE):
        if member.id not in get_relation(row, S.P_LearningQueue.MEMBER):
            continue
        override = get_select(row, S.P_LearningQueue.COACH_OVERRIDE)
        topic_ids = get_relation(row, S.P_LearningQueue.TOPIC)
        if override and topic_ids:
            overrides[topic_ids[0]] = override

    ordered = sorted(solved, key=lambda s: (0 if s.solved_on else 1, s.solved_on, s.page_id))
    for item in ordered:
        item.owned = _script_owned(item, infos)
    problems = [
        topic_map.RouteProblem(
            row_id=item.page_id,
            tags=item.cf_tags,
            rating=item.effective,
            mode=item.mode,
            fixed=None if item.owned else item.current_topics,
        )
        for item in ordered
    ]
    routed = topic_map.route_topics_sequential(
        problems,
        infos,
        tier_threshold=num(cfg, K.TIER_COMPETENT),
        factor_fn=lambda rating, low, high, mode: mastery.solve_points(rating, low, high, mode, cfg),
        overrides=overrides,
    )

    for item in ordered:
        if not item.owned:
            continue
        new_ids = routed.get(item.page_id, [])
        item.topic_ids = list(new_ids)
        props: dict[str, Any] = {}
        if set(new_ids) != set(item.current_topics):
            props[S.P_Problems.TOPICS] = nt.p_relation(new_ids)
        if tuple(new_ids) != item.auto_topics:
            props[S.P_Problems.AUTO_TOPICS] = nt.p_text(",".join(new_ids))
        if not props or not routing.enabled:
            continue
        if routing.written >= routing.budget:
            routing.capped = True
            continue
        repo.update(S.DB_PROBLEMS, item.page, props)
        routing.written += 1


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
        "solved_topics": [p.topic_ids for p in solved],
        "topic_tiers": dict(tiers),
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
        props = {
            S.P_Achievements.TITLE: nt.p_title(f"{badge} · {member.name}"),
            S.P_Achievements.WHO: nt.p_select(member.name),
            S.P_Achievements.MEMBER: nt.p_relation([member.id]),
            S.P_Achievements.BADGE: nt.p_select(badge),
            S.P_Achievements.TIER: nt.p_select(tier),
            S.P_Achievements.EARNED_ON: nt.p_date(today, with_time=False),
            S.P_Achievements.DETAIL: nt.p_text(detail),
            S.P_Achievements.SOURCE_KEY: nt.p_text(key),
        }
        repo.create(S.DB_ACHIEVEMENTS, props, icon=icons.icon_for(S.DB_ACHIEVEMENTS, props))


def _sync_practice_tasks(repo: Repo, member: Member) -> None:
    """Done Count and Done status for the member's auto Topic Practice tasks and LeetCode daily tasks.

    A listed problem counts as done when the member has a solved Problems row with the same
    normalized Problem Key or the same normalized Link (see ``problem_ref``). A LeetCode daily
    task lists one problem, so it becomes Done with that solve."""
    solved: set[str] = set()
    for p in repo.rows(S.DB_PROBLEMS):
        if get_status(p, S.P_Problems.STATUS) == "Solved" and member.id in get_relation(p, S.P_Problems.MEMBER):
            solved |= identities(get_text(p, S.P_Problems.PROBLEM_KEY), get_url(p, S.P_Problems.LINK))
    for task in repo.rows(S.DB_TASKS):
        auto_practice = get_select(task, S.P_Tasks.TYPE) == "Topic Practice" and get_select(task, S.P_Tasks.ORIGIN) == "Auto"
        if not (auto_practice or is_lc_daily_task(task)):
            continue
        if member.id not in get_relation(task, S.P_Tasks.ASSIGNEE):
            continue
        done = sum(1 for ids in _task_problem_identities(task) if ids & solved)
        props: dict[str, Any] = {S.P_Tasks.DONE_COUNT: nt.p_number(done)}
        target = get_number(task, S.P_Tasks.TARGET)
        status = get_status(task, S.P_Tasks.STATUS)
        if target and done >= target and status in TASK_OPEN_STATUSES:
            props[S.P_Tasks.STATUS] = nt.p_status("Done")
        repo.sync(S.DB_TASKS, task, props)


def _task_problem_identities(task: dict) -> list[set[str]]:
    """Identities of each problem listed in a task's Links ("<key> | <url>" per line; the URL is optional)."""
    out: list[set[str]] = []
    for line in get_text(task, S.P_Tasks.LINKS).splitlines():
        key, _, link = line.partition("|")
        ids = identities(key.strip(), link.strip())
        if ids:
            out.append(ids)
    return out


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


def _goal_links(goal: dict, topics: list[Topic]) -> dict[str, Any]:
    """The link arguments of ``goals.current_value`` for one goal row: its Linked Topics, and the
    ids of the topics of its Linked Level (None when the goal has no Linked Level)."""
    level_ids = get_relation(goal, S.P_Goals.LINKED_LEVEL)
    level_topic_ids = None
    if level_ids:
        level_topic_ids = [t.id for t in topics if level_ids[0] in t.level_ids]
    return {"topic_ids": get_relation(goal, S.P_Goals.LINKED_TOPICS), "level_topic_ids": level_topic_ids}


def _update_goals(ctx: Context, repo: Repo, results: dict[str, MemberResult], members: list[Member]) -> None:
    by_id = {m.id: m for m in members}
    topics = repo.topics()
    for goal in repo.rows(S.DB_GOALS):
        metric = get_select(goal, S.P_Goals.METRIC) or ""
        threshold_value = get_number(goal, S.P_Goals.THRESHOLD_RATING)
        threshold = int(threshold_value) if threshold_value is not None else None
        links = _goal_links(goal, topics)
        try:
            if get_select(goal, S.P_Goals.SCOPE) == "Team":
                team_ids = get_relation(goal, S.P_Goals.TEAM)
                if not team_ids:
                    continue
                values = [
                    goals.current_value(metric, results[m.id].stats, threshold, **links)
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
                current = goals.current_value(metric, results[member_ids[0]].stats, threshold, **links)
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
