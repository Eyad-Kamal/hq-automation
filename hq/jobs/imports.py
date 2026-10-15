"""Job ``imports``: pull accepted solves and rating history for active members.

Per active member with at least one handle:

* since = (Last Sync - 2 days), or now - IMPORT_BACKFILL_DAYS when never synced.
  The 2-day overlap re-reads recent solves, so a source outage that lasts less
  than two days loses nothing.
* Solves become Problems rows keyed by Source Key "<problem key>:<member id>".
  Existing rows are only ever FILLED where empty. User-edited fields (Notes,
  Key Idea, Time (min), a non-empty Topics, a Mode the user changed) are not
  overwritten. Status becomes Solved and Imported becomes ticked.
* Upsolve detection: a Practice solve of a problem from a contest in which the
  member has a live/virtual solve (or a recorded Rated Live/Virtual Contest Entry)
  and which was solved after that contest started, gets Mode "Upsolve". Only
  submissions inside the import window, plus entries already recorded, are seen.
* Live/virtual solves also create the Contest Entry (member, contest) if it is
  missing, link the problems to it, and mark the member's contest Task Attended.
  A missing contest row is created as a placeholder (see ``_ensure_contest``).
* Rating history (CF and AtCoder) rows are created once per Source Key
  "<platform>:<contest name>:<YYYY-MM-DD>:<member id>".
* Members get CF Rating, CF Max Rating, AtCoder Rating and Last Sync.
* Rating @ Solve (every judge) is the member's CF rating just before the solve, when
  CF rating history exists; otherwise the member's Ref Rating at import time.
  Filled only where empty.

Failure handling:

* Sources are called in strict mode, so a failed call is told apart from "no new
  solves". A member whose source failed, or whose creates were capped, does NOT
  advance Last Sync, so the next run reads the same window again. Rows already
  imported are found by Source Key and are not duplicated.
* CF user.info is fetched in one batch; if the batch fails, each handle is retried
  alone and only the bad handles are skipped and reported.
* At most IMPORT_MAX_CREATES rows are created per run (default 400). The rest is
  imported by later runs.

STATE_LAST_IMPORT is written at the end. A failing member is recorded and the
others still run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from hq import config
from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import ERROR, OK, WARNING, Context, JobResult, cairo_midnight
from hq.jobs.recompute import member_ref_rating
from hq.logic import contest_kind, scoring, topic_map
from hq.logic.problem_ref import identities
from hq.models import RatingChange, Solve
from hq.repo import (
    Member,
    Repo,
    get_checkbox,
    get_date,
    get_number,
    get_relation,
    get_select,
    get_status,
    get_text,
    get_title,
    get_url,
    is_blank,
)
from hq.schema import ConfigKey as K
from hq.schema import Judge, Mode
from hq.timeutil import cairo_date

log = logging.getLogger("hq.jobs.imports")
PLACEHOLDER_CONTEST = re.compile(r"^(Codeforces|CF Gym) contest \d+$")

JOB = "imports"
DUE_AFTER = timedelta(minutes=25)
SYNC_OVERLAP = timedelta(days=2)
CF_JUDGES = frozenset({Judge.CODEFORCES, Judge.CF_GYM})
LIVE_MODES = frozenset({Mode.LIVE, Mode.VIRTUAL})
ENTRY_PARTICIPATIONS = frozenset({"Rated Live", "Virtual"})
CF_TAG_SET = frozenset(S.CF_TAGS)
_STATUS_SOLVED = "Solved"
_ALWAYS_SKIPPED = (S.P_Problems.STATUS, S.P_Problems.IMPORTED, S.P_Problems.MODE, S.P_Problems.SOURCE_KEY)


@dataclass
class ImportStats:
    problems_created: int = 0
    problems_updated: int = 0
    entries_created: int = 0
    contests_created: int = 0
    ratings_created: int = 0
    creates: int = 0  # every create this run, checked against IMPORT_MAX_CREATES
    capped: int = 0  # creates that were skipped because the cap was reached
    members_ok: int = 0
    errors: list[str] = field(default_factory=list)
    source_failures: list[str] = field(default_factory=list)
    bad_handles: list[str] = field(default_factory=list)


@dataclass
class _MemberOutcome:
    """What happened to one member: which sources failed, and whether all work was done."""

    failed_sources: list[str] = field(default_factory=list)
    incomplete: bool = False

    @property
    def clean(self) -> bool:
        return not self.failed_sources and not self.incomplete


def imports_due(ctx: Context) -> bool:
    """True when the last import is older than 25 minutes (or never happened)."""
    last = config.state_datetime(ctx.repo, config.STATE_LAST_IMPORT)
    return last is None or ctx.now - last >= DUE_AFTER


def _sync_start(value: date | datetime) -> datetime:
    """Last Sync as an aware datetime. A date means the Cairo day's start (F17)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return cairo_midnight(value)


def since_for(member: Member, now: datetime, backfill_days: int) -> datetime:
    if member.last_sync is None:
        return now - timedelta(days=backfill_days)
    return _sync_start(member.last_sync) - SYNC_OVERLAP


def rating_at(changes: list[RatingChange], when: datetime) -> int | None:
    """The rating just before ``when``: the old value of the first change after it,
    or the newest new value when every change is earlier. None without history."""
    if not changes:
        return None
    ordered = sorted(changes, key=lambda c: c.date_utc)
    for change in ordered:
        if change.date_utc > when:
            return change.old
    return ordered[-1].new


def _platform_of(external_id: str) -> tuple[str, str]:
    """(Contests Platform option, placeholder name prefix) for an external id."""
    if external_id.startswith("GYM-"):
        return "CF Gym", "CF Gym contest"
    if external_id.startswith("CF-"):
        return "Codeforces", "Codeforces contest"
    if external_id.startswith("AT-"):
        return "AtCoder", "AtCoder contest"
    if external_id.startswith("LC-"):
        return "LeetCode", "LeetCode contest"
    return "Other", "Contest"


def _cf_user_info(ctx: Context, handles: list[str]) -> tuple[dict[str, dict], list[str]]:
    """CF user.info for the handles. One batch call; if it fails, each handle is retried
    alone. Returns (info by handle, bad handles that could not be read)."""
    if not handles:
        return {}, []
    batch = ctx.sources.cf_user_info(handles, strict=True)
    info: dict[str, dict] = dict(batch or {})
    bad: list[str] = []
    for handle in handles:
        if handle in info:
            continue
        single = ctx.sources.cf_user_info([handle], strict=True)
        if single and handle in single:
            info[handle] = single[handle]
        else:
            bad.append(handle)
    return info, bad


def run(ctx: Context) -> JobResult:
    repo = ctx.repo
    cfg = ctx.cfg
    renamed = _rename_placeholder_contests(ctx)
    backfill = int(num(cfg, K.IMPORT_BACKFILL_DAYS))
    offset = int(num(cfg, K.ATCODER_OFFSET))
    members = [m for m in ctx.active_members() if m.has_handles]
    topics = [t.info() for t in repo.topics() if t.has_band]
    cf_handles = [m.cf_handle for m in members if m.cf_handle]
    cf_info, bad_handles = _cf_user_info(ctx, cf_handles)
    entries = _entry_index(repo)

    stats = ImportStats(bad_handles=bad_handles)
    for member in members:
        try:
            outcome = _import_member(ctx, member, stats, topics, offset, backfill, cf_info, entries)
            if outcome.failed_sources:
                stats.source_failures.append(f"{member.name}: {', '.join(outcome.failed_sources)} failed; Last Sync kept")
            if outcome.incomplete:
                stats.source_failures.append(f"{member.name}: create limit reached; the rest imports next run")
            if outcome.clean:
                stats.members_ok += 1
        except Exception as exc:  # noqa: BLE001 - one member must not stop the others
            log.exception("import failed for %s", member.name)
            stats.errors.append(f"{member.name}: {type(exc).__name__}: {exc}")

    totals = _fill_total_problems(ctx)
    config.set_state(repo, config.STATE_LAST_IMPORT, ctx.now.isoformat())

    items = stats.problems_created + stats.problems_updated + stats.entries_created + stats.ratings_created + renamed + totals
    details = [
        f"members imported cleanly: {stats.members_ok}/{len(members)}",
        f"placeholder contests renamed: {renamed}",
        f"contest Total Problems filled: {totals}",
        f"problems: {stats.problems_created} new, {stats.problems_updated} filled",
        f"contest entries: {stats.entries_created} new (contests created: {stats.contests_created})",
        f"rating rows: {stats.ratings_created} new",
        f"creates this run: {stats.creates}, skipped by IMPORT_MAX_CREATES: {stats.capped}",
    ]
    if bad_handles:
        details.append("bad CF handles (skipped): " + ", ".join(bad_handles))
    details += stats.source_failures
    details += [f"FAILED {line}" for line in stats.errors]
    if stats.errors and stats.members_ok == 0:
        result = ERROR
    elif stats.errors or stats.source_failures or bad_handles:
        result = WARNING
    else:
        result = OK
    return JobResult(job=JOB, result=result, items=items, details="\n".join(details))


def _create(ctx: Context, stats: ImportStats, db: str, props: dict[str, Any], outcome: _MemberOutcome) -> dict | None:
    """Create one row unless the per-run create cap is reached. None when skipped."""
    cap = int(num(ctx.cfg, K.IMPORT_MAX_CREATES))
    if stats.creates >= cap:
        stats.capped += 1
        outcome.incomplete = True
        return None
    stats.creates += 1
    return ctx.repo.create(db, props)


def _entry_index(repo: Repo) -> dict[tuple[str, str], dict]:
    """(Contest page id, member page id) -> Contest Entries row."""
    index: dict[tuple[str, str], dict] = {}
    for entry in repo.rows(S.DB_CONTEST_ENTRIES):
        for contest_id in get_relation(entry, S.P_ContestEntries.CONTEST):
            for member_id in get_relation(entry, S.P_ContestEntries.MEMBER):
                index.setdefault((contest_id, member_id), entry)
    return index


def _import_member(
    ctx: Context,
    member: Member,
    stats: ImportStats,
    topics: list,
    offset: int,
    backfill: int,
    cf_info: dict[str, dict],
    entries: dict[tuple[str, str], dict],
) -> _MemberOutcome:
    repo = ctx.repo
    outcome = _MemberOutcome()
    since = since_for(member, ctx.now, backfill)
    ref = member_ref_rating(repo, member)  # before this run changes the member's rating

    solves: list[Solve] = []
    cf_history: list[RatingChange] = []
    at_history: list[RatingChange] = []
    if member.cf_handle:
        cf_solves = ctx.sources.cf_solves(member.cf_handle, since, strict=True)
        if cf_solves is None:
            outcome.failed_sources.append("Codeforces solves")
        else:
            solves += list(cf_solves)
        history = ctx.sources.cf_rating_history(member.cf_handle, strict=True)
        if history is None:
            outcome.failed_sources.append("Codeforces rating history")
        else:
            cf_history = list(history)
    if member.atcoder_handle:
        at_solves = ctx.sources.at_solves(member.atcoder_handle, since, offset, strict=True)
        if at_solves is None:
            outcome.failed_sources.append("AtCoder solves")
        else:
            solves += list(at_solves)
        history = ctx.sources.at_rating_history(member.atcoder_handle, strict=True)
        if history is None:
            outcome.failed_sources.append("AtCoder rating history")
        else:
            at_history = list(history)
    if member.leetcode_handle:
        lc_solves = ctx.sources.lc_solves(member.leetcode_handle, since, strict=True)
        if lc_solves is None:
            outcome.failed_sources.append("LeetCode solves")
        else:
            solves += list(lc_solves)
    solves.sort(key=lambda s: s.solved_at_utc)

    live_starts = _live_starts(repo, member, solves, entries)
    team = repo.team_name(member.team_ids[0]) if member.team_ids else None

    problem_pages: dict[str, dict] = {}
    modes: dict[str, str] = {}
    for solve in solves:
        mode = _effective_mode(solve, live_starts)
        modes[solve.problem_key] = mode
        # Rating @ Solve for every judge (F8): CF history when it exists, else Ref Rating.
        rating_then = rating_at(cf_history, solve.solved_at_utc) if cf_history else ref
        topic_ids: list[str] = []
        if solve.tags and topics:
            effective = scoring.effective_rating(solve.rating, solve.difficulty, _lc_map_of(ctx))
            topic_ids = topic_map.map_topics(solve.tags, effective, topics)
        props = _problem_props(member, solve, mode, rating_then, topic_ids, team)
        page = _upsert_problem(ctx, member, solve, props, mode, stats, outcome)
        if page is not None:
            problem_pages[solve.problem_key] = page

    _record_contest_entries(ctx, member, solves, modes, problem_pages, entries, stats, outcome)
    _record_ratings(ctx, member, cf_history, "codeforces", "Codeforces", stats, outcome)
    _record_ratings(ctx, member, at_history, "atcoder", "AtCoder", stats, outcome)

    updates: dict[str, Any] = {}
    info = cf_info.get(member.cf_handle) if member.cf_handle else None
    if info is not None:
        updates[S.P_Members.CF_RATING] = nt.p_number(info.get("rating"))
        updates[S.P_Members.CF_MAX_RATING] = nt.p_number(info.get("maxRating"))
    if at_history:
        latest = max(at_history, key=lambda c: c.date_utc)
        updates[S.P_Members.ATCODER_RATING] = nt.p_number(latest.new)
    if outcome.clean:
        # Last Sync only moves forward when everything was read and written (F4, F19).
        updates[S.P_Members.LAST_SYNC] = nt.p_date(ctx.today, with_time=False)
    repo.sync(S.DB_MEMBERS, member.page, updates)
    return outcome


def _lc_map_of(ctx: Context) -> dict[str, int]:
    return {
        "Easy": int(num(ctx.cfg, K.LC_EASY)),
        "Medium": int(num(ctx.cfg, K.LC_MEDIUM)),
        "Hard": int(num(ctx.cfg, K.LC_HARD)),
    }


def _live_starts(
    repo: Repo,
    member: Member,
    solves: list[Solve],
    entries: dict[tuple[str, str], dict],
) -> dict[str, datetime]:
    """Contest external id -> when the member started it live/virtually (earliest known)."""
    starts: dict[str, datetime] = {}
    for solve in solves:
        if solve.mode in LIVE_MODES and solve.contest_external_id:
            current = starts.get(solve.contest_external_id)
            if current is None or solve.solved_at_utc < current:
                starts[solve.contest_external_id] = solve.solved_at_utc
    for (contest_id, member_id), entry in entries.items():
        if member_id != member.id:
            continue
        if get_select(entry, S.P_ContestEntries.PARTICIPATION) not in ENTRY_PARTICIPATIONS:
            continue
        contest = repo.by_id(S.DB_CONTESTS, contest_id)
        day = get_date(entry, S.P_ContestEntries.DATE)
        external_id = get_text(contest, S.P_Contests.EXTERNAL_ID) if contest else ""
        if external_id and day is not None:
            starts.setdefault(external_id, _sync_start(day))
    return starts


def _effective_mode(solve: Solve, live_starts: dict[str, datetime]) -> str:
    if solve.mode == Mode.PRACTICE and solve.contest_external_id:
        started = live_starts.get(solve.contest_external_id)
        if started is not None and solve.solved_at_utc > started:
            return Mode.UPSOLVE
    return solve.mode


def _problem_props(
    member: Member,
    solve: Solve,
    mode: str,
    rating_then: int | None,
    topic_ids: list[str],
    team: str | None,
) -> dict[str, Any]:
    tags = [t for t in solve.tags if t in CF_TAG_SET]
    return {
        S.P_Problems.TITLE: nt.p_title(solve.name or solve.problem_key),
        S.P_Problems.MEMBER: nt.p_relation([member.id]),
        S.P_Problems.WHO: nt.p_select(member.name),
        S.P_Problems.TEAM: nt.p_select(team),
        S.P_Problems.JUDGE: nt.p_select(solve.judge),
        S.P_Problems.PROBLEM_KEY: nt.p_text(solve.problem_key),
        S.P_Problems.LINK: nt.p_url(solve.link),
        S.P_Problems.RATING: nt.p_number(solve.rating),
        S.P_Problems.DIFFICULTY: nt.p_select(solve.difficulty),
        S.P_Problems.CF_TAGS: nt.p_multi(tags),
        S.P_Problems.TOPICS: nt.p_relation(topic_ids),
        S.P_Problems.SOLVED_ON: nt.p_date(solve.solved_at_utc),
        S.P_Problems.MODE: nt.p_select(mode),
        S.P_Problems.STATUS: nt.p_status(_STATUS_SOLVED),
        S.P_Problems.ATTEMPTS: nt.p_number(solve.attempts),
        S.P_Problems.RATING_AT_SOLVE: nt.p_number(rating_then),
        S.P_Problems.IMPORTED: nt.p_checkbox(True),
        S.P_Problems.SOURCE_KEY: nt.p_text(f"{solve.problem_key}:{member.id}"),
    }


def _upsert_problem(
    ctx: Context,
    member: Member,
    solve: Solve,
    props: dict[str, Any],
    mode: str,
    stats: ImportStats,
    outcome: _MemberOutcome,
) -> dict | None:
    repo = ctx.repo
    key = f"{solve.problem_key}:{member.id}"
    page = repo.find(S.DB_PROBLEMS, S.P_Problems.SOURCE_KEY, key)
    if page is None:
        manual = _manual_match(repo, member, solve)
        if manual is not None:
            # A row the member or a coach typed for the same problem: adopt it, fill only blanks.
            changes = _adopt_changes(manual, props)
            if changes:
                stats.problems_updated += 1
                repo.update(S.DB_PROBLEMS, manual, changes)
            return manual
        created = _create(ctx, stats, S.DB_PROBLEMS, props, outcome)
        if created is not None:
            stats.problems_created += 1
        return created
    changes = _problem_changes(page, props, mode)
    if changes:
        stats.problems_updated += 1
        repo.update(S.DB_PROBLEMS, page, changes)
    return page


def _manual_match(repo: Repo, member: Member, solve: Solve) -> dict | None:
    """A Problems row of this member, not yet linked to a Source Key, whose Problem Key or
    Link names the same problem as the solve (case, trailing slash, scheme and the CF
    problemset/contest forms are ignored)."""
    wanted = identities(solve.problem_key, solve.link)
    if not wanted:
        return None
    for row in repo.rows(S.DB_PROBLEMS):
        if member.id not in get_relation(row, S.P_Problems.MEMBER):
            continue
        if get_text(row, S.P_Problems.SOURCE_KEY).strip():
            continue  # already an imported row (of this or another key)
        if identities(get_text(row, S.P_Problems.PROBLEM_KEY), get_url(row, S.P_Problems.LINK)) & wanted:
            return row
    return None


def _adopt_changes(page: dict, props: dict[str, Any]) -> dict[str, Any]:
    """Fill-only changes that link an adopted manual row to the import: every property
    that is empty on the row gets the imported value. Imported stays unticked, and a value
    the user entered is never overwritten. Status is the exception: an accepted solve sets it
    to Solved."""
    current = page.get("properties") or {}
    changes = {
        name: value
        for name, value in props.items()
        if name not in (S.P_Problems.IMPORTED, S.P_Problems.STATUS)
        and is_blank(current.get(name))
        and not is_blank(value)
    }
    # An accepted submission is a fact: Status becomes Solved even when the row said otherwise.
    if get_status(page, S.P_Problems.STATUS) != _STATUS_SOLVED:
        changes[S.P_Problems.STATUS] = nt.p_status(_STATUS_SOLVED)
    return changes


def _problem_changes(page: dict, props: dict[str, Any], mode: str) -> dict[str, Any]:
    """Fill-only changes for an existing Problems row (see module docstring)."""
    changes: dict[str, Any] = {}
    current = page.get("properties") or {}
    for name, value in props.items():
        if name in _ALWAYS_SKIPPED:
            continue
        if is_blank(current.get(name)) and not is_blank(value):
            changes[name] = value
    current_mode = get_select(page, S.P_Problems.MODE)
    if not current_mode or (current_mode == Mode.PRACTICE and mode == Mode.UPSOLVE):
        changes[S.P_Problems.MODE] = nt.p_select(mode)
    if get_status(page, S.P_Problems.STATUS) != _STATUS_SOLVED:
        changes[S.P_Problems.STATUS] = nt.p_status(_STATUS_SOLVED)
    if not get_checkbox(page, S.P_Problems.IMPORTED):
        changes[S.P_Problems.IMPORTED] = nt.p_checkbox(True)
    return changes


def _record_contest_entries(
    ctx: Context,
    member: Member,
    solves: list[Solve],
    modes: dict[str, str],
    problem_pages: dict[str, dict],
    entries: dict[tuple[str, str], dict],
    stats: ImportStats,
    outcome: _MemberOutcome,
) -> None:
    groups: dict[str, list[tuple[Solve, dict]]] = {}
    for solve in solves:
        page = problem_pages.get(solve.problem_key)
        if page is None or modes.get(solve.problem_key) not in LIVE_MODES or not solve.contest_external_id:
            continue
        groups.setdefault(solve.contest_external_id, []).append((solve, page))
    for external_id, items in groups.items():
        contest, created = _ensure_contest(ctx, external_id, [s for s, _ in items], stats, outcome)
        if contest is None:
            continue
        stats.contests_created += int(created)
        _ensure_entry(ctx, member, contest, items, entries, stats, outcome)


def _ensure_contest(
    ctx: Context,
    external_id: str,
    solves: list[Solve],
    stats: ImportStats,
    outcome: _MemberOutcome,
) -> tuple[dict | None, bool]:
    """The Contests row for an external id. Created as a placeholder when missing:
    submissions do not carry contest names, so a coach should rename it.
    Returns (page or None when the create cap was reached, created?)."""
    page = ctx.repo.find(S.DB_CONTESTS, S.P_Contests.EXTERNAL_ID, external_id)
    if page is not None:
        return page, False
    platform, prefix = _platform_of(external_id)
    number = external_id.split("-", 1)[1] if "-" in external_id else external_id
    name = f"{prefix} {number}"
    start = min(s.solved_at_utc for s in solves)
    real = ctx.sources.cf_contest(external_id) if external_id.startswith(("CF-", "GYM-")) else None
    if real is not None:  # the real name, start and duration from contest.list (not a placeholder)
        name, start, platform = real.name, real.start_utc, real.platform
    props = {
        S.P_Contests.TITLE: nt.p_title(name),
        S.P_Contests.PLATFORM: nt.p_select(platform),
        S.P_Contests.KIND: nt.p_select(contest_kind.classify(platform, name)),
        S.P_Contests.START: nt.p_date(start),
        S.P_Contests.PHASE: nt.p_select("Finished"),
        S.P_Contests.RATED: nt.p_checkbox(any(s.mode == Mode.LIVE for s in solves)),
        S.P_Contests.EXTERNAL_ID: nt.p_text(external_id),
        S.P_Contests.TASKS_CREATED: nt.p_checkbox(True),
        S.P_Contests.ALERT_SENT: nt.p_checkbox(True),
    }
    if real is not None:
        props[S.P_Contests.DURATION_MIN] = nt.p_number(real.duration_min)
    created = _create(ctx, stats, S.DB_CONTESTS, props, outcome)
    return created, created is not None


def _fill_total_problems(ctx: Context) -> int:
    """Fill an empty Total Problems on regular Codeforces Contest Entries from the problemset
    (problems per contest id). Fill-only. Gym contests are left empty (no lookup). Returns
    the number of entries filled."""
    repo = ctx.repo
    filled = 0
    for entry in repo.rows(S.DB_CONTEST_ENTRIES):
        if get_number(entry, S.P_ContestEntries.TOTAL_PROBLEMS):
            continue
        contest = repo.by_id(S.DB_CONTESTS, (get_relation(entry, S.P_ContestEntries.CONTEST) or [None])[0])
        if contest is None or get_select(contest, S.P_Contests.PLATFORM) != "Codeforces":
            continue
        count = ctx.sources.cf_problem_count(get_text(contest, S.P_Contests.EXTERNAL_ID))
        if count:
            repo.sync(S.DB_CONTEST_ENTRIES, entry, {S.P_ContestEntries.TOTAL_PROBLEMS: nt.p_number(count)}, fill_only=True)
            filled += 1
    return filled


def _rename_placeholder_contests(ctx: Context) -> int:
    """Contests rows still named "Codeforces contest <id>" or "CF Gym contest <id>" get the
    real name, start, duration and Kind; their Contest Entry titles become
    "<contest name> · <member>". Returns the number of contests renamed."""
    repo = ctx.repo
    renamed = 0
    for contest in repo.rows(S.DB_CONTESTS):
        name = get_title(contest, S.P_Contests.TITLE)
        external_id = get_text(contest, S.P_Contests.EXTERNAL_ID)
        if not PLACEHOLDER_CONTEST.match(name) or not external_id.startswith(("CF-", "GYM-")):
            continue
        real = ctx.sources.cf_contest(external_id)
        if real is None or real.name == name:
            continue
        repo.sync(
            S.DB_CONTESTS,
            contest,
            {
                S.P_Contests.TITLE: nt.p_title(real.name),
                S.P_Contests.START: nt.p_date(real.start_utc),
                S.P_Contests.DURATION_MIN: nt.p_number(real.duration_min),
                S.P_Contests.KIND: nt.p_select(contest_kind.classify(real.platform, real.name)),
            },
        )
        renamed += 1
        for entry in repo.rows(S.DB_CONTEST_ENTRIES):
            if contest["id"] not in get_relation(entry, S.P_ContestEntries.CONTEST):
                continue
            who = get_select(entry, S.P_ContestEntries.WHO)
            if who:
                repo.sync(S.DB_CONTEST_ENTRIES, entry, {S.P_ContestEntries.TITLE: nt.p_title(f"{real.name} · {who}")})
    return renamed


def _ensure_entry(
    ctx: Context,
    member: Member,
    contest: dict,
    items: list[tuple[Solve, dict]],
    entries: dict[tuple[str, str], dict],
    stats: ImportStats,
    outcome: _MemberOutcome,
) -> None:
    repo = ctx.repo
    key = (contest["id"], member.id)
    problem_ids = [page["id"] for _, page in items]
    solves = [s for s, _ in items]
    participation = "Rated Live" if any(s.mode == Mode.LIVE for s in solves) else "Virtual"
    entry = entries.get(key)
    linked = get_relation(entry, S.P_ContestEntries.PROBLEMS) if entry is not None else []
    all_ids = list(dict.fromkeys([*linked, *problem_ids]))
    solved = len(all_ids)
    if entry is None:
        day = cairo_date(min(s.solved_at_utc for s in solves))
        props = {
            S.P_ContestEntries.TITLE: nt.p_title(f"{get_title(contest, S.P_Contests.TITLE)} · {member.name}"),
            S.P_ContestEntries.MODE: nt.p_select("Solo"),
            S.P_ContestEntries.WHO: nt.p_select(member.name),
            S.P_ContestEntries.MEMBER: nt.p_relation([member.id]),
            S.P_ContestEntries.CONTEST: nt.p_relation([contest["id"]]),
            S.P_ContestEntries.DATE: nt.p_date(day, with_time=False),
            S.P_ContestEntries.PARTICIPATION: nt.p_select(participation),
            S.P_ContestEntries.STATUS: nt.p_status("Attended"),
            S.P_ContestEntries.SOLVED: nt.p_number(solved),
            S.P_ContestEntries.PROBLEMS: nt.p_relation(all_ids),
        }
        entry = _create(ctx, stats, S.DB_CONTEST_ENTRIES, props, outcome)
        if entry is None:
            return
        entries[key] = entry
        stats.entries_created += 1
    else:
        repo.sync(S.DB_CONTEST_ENTRIES, entry, {S.P_ContestEntries.PARTICIPATION: nt.p_select(participation)}, fill_only=True)
        status = get_status(entry, S.P_ContestEntries.STATUS)
        changes: dict[str, Any] = {
            S.P_ContestEntries.SOLVED: nt.p_number(solved),
            S.P_ContestEntries.PROBLEMS: nt.p_relation(all_ids),
        }
        if status in (None, "Planned"):
            changes[S.P_ContestEntries.STATUS] = nt.p_status("Attended")
        repo.sync(S.DB_CONTEST_ENTRIES, entry, changes)
    _attend_contest_tasks(repo, member, contest, entry)


def _attend_contest_tasks(repo: Repo, member: Member, contest: dict, entry: dict) -> None:
    for task in repo.rows(S.DB_TASKS):
        if get_select(task, S.P_Tasks.TYPE) != "Contest":
            continue
        if contest["id"] not in get_relation(task, S.P_Tasks.CONTEST):
            continue
        if member.id not in get_relation(task, S.P_Tasks.ASSIGNEE):
            continue
        repo.sync(S.DB_TASKS, task, {S.P_Tasks.CONTEST_ENTRY: nt.p_relation([entry["id"]])}, fill_only=True)
        if get_status(task, S.P_Tasks.STATUS) in ("Planned", "To Do"):
            repo.update(S.DB_TASKS, task, {S.P_Tasks.STATUS: nt.p_status("Attended")})


def _record_ratings(
    ctx: Context,
    member: Member,
    changes: list[RatingChange],
    key_prefix: str,
    platform: str,
    stats: ImportStats,
    outcome: _MemberOutcome,
) -> None:
    repo = ctx.repo
    for change in changes:
        # The date is part of the key (F12): two contests with the same name stay apart.
        key = f"{key_prefix}:{change.contest_name}:{cairo_date(change.date_utc).isoformat()}:{member.id}"
        if repo.find(S.DB_RATING_HISTORY, S.P_RatingHistory.SOURCE_KEY, key) is not None:
            continue
        props = {
            S.P_RatingHistory.TITLE: nt.p_title(change.contest_name or "Rating change"),
            S.P_RatingHistory.WHO: nt.p_select(member.name),
            S.P_RatingHistory.MEMBER: nt.p_relation([member.id]),
            S.P_RatingHistory.PLATFORM: nt.p_select(platform),
            S.P_RatingHistory.DATE: nt.p_date(change.date_utc),
            S.P_RatingHistory.OLD: nt.p_number(change.old),
            S.P_RatingHistory.NEW: nt.p_number(change.new),
            S.P_RatingHistory.RANK: nt.p_number(change.rank),
            S.P_RatingHistory.CONTEST_NAME: nt.p_text(change.contest_name),
            S.P_RatingHistory.SOURCE_KEY: nt.p_text(key),
        }
        if _create(ctx, stats, S.DB_RATING_HISTORY, props, outcome) is not None:
            stats.ratings_created += 1
