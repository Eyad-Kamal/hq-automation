"""Job ``cleanup``: when a member's row is deleted from Members, trash that member's leftovers and
remove the name from the Who and Team selects (Config MEMBER_CLEANUP, default 1; 0 = off).

Known names are the names of every Members row and every Teams row, plus RESERVED_NAMES (the
Who and Team values the code writes that are not member or team names). A departed name is an
option of a Who select, or of a Team select, in a database that has one, that is not known.

Safety, in order of checks:

* Members or Teams cannot be read, or Members has no rows: nothing happens, a Warning is logged.
* Two runs: a departed name is acted on only when it was already departed on an earlier run, at
  least PENDING_DELAY before this one (the time of its first sighting, kept in STATE_CLEANUP_PENDING).
  A name seen for the first time is only recorded. A name that is known again is dropped.
* A row is trashed only when its Who (for a team name, also its Team select) is a confirmed name,
  its Member relation links to no live Members row, and its Team relation (where the database has
  one) links to no live Teams row. A relation that points at a deleted row links to nothing: a
  deleted Members row no longer appears in the query. The Team check applies to every name, so the
  rows of a renamed team are kept.
* Trashing moves a page to the Notion trash (recoverable for 30 days). At most CLEANUP_PER_RUN pages
  (rows and dashboard pages together) are trashed per run; the rest waits for the next run.
* A dashboard page is a child page of DASHBOARD_PARENT_PAGE_ID titled "<name> · Dashboard" or
  "👤 <name> · Dashboard", for a confirmed name.
* An option is removed from a Who or Team select only when no row of that database still has that
  value, and only when no dashboard of a confirmed name is left and the cap was not hit. A removed
  option would hide the name from later runs, so the dashboards must go first. A refused option
  change is a Warning; the rows are gone either way.

Nothing else is touched: Members and Teams rows, Config, Sync Log, Topics, Levels, Contests,
Resources and Code Templates are never scanned for trashing.

The Notion calls used here (``in_trash`` on a page, the option list of a data source) are not
verified against the live API; see hq/notion.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from hq import config
from hq import schema as S
from hq.config import num
from hq.jobs import OK, WARNING, Context, JobResult
from hq.jobs.dashboards import DASHBOARD_PARENT_PAGE_ID
from hq.repo import Repo, get_relation, get_select, get_title
from hq.schema import ConfigKey as K

log = logging.getLogger("hq.jobs.cleanup")

JOB = "cleanup"
PENDING_DELAY = timedelta(minutes=15)  # a departed name must have been seen this long before it is acted on
RESERVED_NAMES: frozenset[str] = frozenset({"Unassigned", "Club", "No Who", "Team"})
DASHBOARD_TITLES: tuple[str, ...] = ("{name} · Dashboard", "👤 {name} · Dashboard")
ERROR_CHARS = 200


@dataclass(frozen=True)
class Scan:
    """A database with a Who select, and what links its rows to a member or a team."""

    db: str
    who: str  # select property: the member name (or the team name, for team rows)
    member: str  # relation to Members
    team_select: str | None = None  # select property holding a team name
    team_rel: str | None = None  # relation to Teams


SCANS: tuple[Scan, ...] = (
    Scan(S.DB_PROBLEMS, S.P_Problems.WHO, S.P_Problems.MEMBER, team_select=S.P_Problems.TEAM),
    Scan(S.DB_TASKS, S.P_Tasks.WHO, S.P_Tasks.ASSIGNEE),
    Scan(S.DB_CONTEST_ENTRIES, S.P_ContestEntries.WHO, S.P_ContestEntries.MEMBER, team_rel=S.P_ContestEntries.TEAM),
    Scan(S.DB_LEARNING_QUEUE, S.P_LearningQueue.WHO, S.P_LearningQueue.MEMBER),
    Scan(S.DB_GOALS, S.P_Goals.WHO, S.P_Goals.MEMBER, team_rel=S.P_Goals.TEAM),
    Scan(S.DB_ACHIEVEMENTS, S.P_Achievements.WHO, S.P_Achievements.MEMBER),
    Scan(S.DB_DAILY_LOG, S.P_DailyLog.WHO, S.P_DailyLog.MEMBER, team_select=S.P_DailyLog.TEAM),
    Scan(S.DB_WEEKLY_SUMMARY, S.P_WeeklySummary.WHO, S.P_WeeklySummary.MEMBER, team_rel=S.P_WeeklySummary.TEAM),
    Scan(S.DB_RATING_HISTORY, S.P_RatingHistory.WHO, S.P_RatingHistory.MEMBER),
)


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:ERROR_CHARS]}"


def _linked(row: dict[str, Any], prop: str | None, live_ids: set[str]) -> bool:
    """True when the relation ``prop`` of ``row`` links to at least one live row."""
    return prop is not None and any(rid in live_ids for rid in get_relation(row, prop))


def _is_leftover(row: dict[str, Any], scan: Scan, name: str, teams: set[str], member_ids: set[str], team_ids: set[str]) -> bool:
    """True when ``row`` belongs to the departed ``name`` and to no live member or team."""
    hit = get_select(row, scan.who) == name
    is_team = name in teams
    if not hit and is_team and scan.team_select is not None:
        hit = get_select(row, scan.team_select) == name
    if not hit:
        return False
    if _linked(row, scan.member, member_ids):
        return False
    # Checked for every name, not only team names: a team row whose team was renamed keeps its
    # Who value, and its Team relation then still links to the live team.
    if _linked(row, scan.team_rel, team_ids):
        return False
    return True


def _read_pending(repo: Repo) -> dict[str, datetime]:
    """{departed name: first sighting} from STATE_CLEANUP_PENDING. Empty when unset or unreadable."""
    raw = config.get_state(repo, config.STATE_CLEANUP_PENDING)
    if not raw:
        return {}
    try:
        names = json.loads(raw)["names"]
        found: dict[str, datetime] = {}
        for name, when in names.items():
            value = datetime.fromisoformat(str(when))
            found[str(name)] = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return found
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("STATE_CLEANUP_PENDING is not readable (%s); the two-run check starts again", exc)
        return {}


def _write_pending(repo: Repo, seen: dict[str, datetime]) -> None:
    value = json.dumps({"names": {n: t.replace(microsecond=0).isoformat() for n, t in seen.items()}}, ensure_ascii=False, sort_keys=True)
    config.set_state(repo, config.STATE_CLEANUP_PENDING, value)


def _options(meta: Any) -> list[dict[str, Any]]:
    """The select options of a schema property (empty for anything that is not a select)."""
    if not isinstance(meta, dict) or meta.get("type") != "select":
        return []
    return [dict(o) for o in ((meta.get("select") or {}).get("options") or []) if isinstance(o, dict)]


def _payload(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Option list for the update: the kept options, with their ids and colors."""
    out: list[dict[str, Any]] = []
    for option in options:
        item: dict[str, Any] = {"name": option["name"], "color": option.get("color") or "default"}
        if option.get("id"):
            item["id"] = option["id"]
        out.append(item)
    return out


def run(ctx: Context) -> JobResult:
    if int(num(ctx.cfg, K.MEMBER_CLEANUP)) != 1:
        return JobResult(job=JOB, result=OK, items=0, details="cleanup: off (MEMBER_CLEANUP is 0)", skipped=True)
    repo = ctx.repo
    try:
        members = repo.rows(S.DB_MEMBERS)
        teams = repo.rows(S.DB_TEAMS)
    except Exception as exc:  # noqa: BLE001 - an unreadable table means nothing is cleaned up
        log.warning("cleanup skipped: Members or Teams could not be read: %s", _short(exc))
        return JobResult(job=JOB, result=WARNING, items=0, details=f"cleanup: skipped, Members or Teams not readable: {_short(exc)}")
    if not members:
        log.warning("cleanup skipped: Members has no rows")
        return JobResult(job=JOB, result=WARNING, items=0, details="cleanup: skipped, Members has no rows")

    member_ids = {p["id"] for p in members}
    team_ids = {p["id"] for p in teams}
    known = {get_title(p, S.P_Members.TITLE).strip() for p in members}
    known |= {get_title(p, S.P_Teams.TITLE).strip() for p in teams}
    known |= set(RESERVED_NAMES)
    known.discard("")

    failures: list[str] = []
    # Discovery: the Who and Team select options of every scanned database (schema reads only).
    select_options: dict[tuple[str, str], list[dict[str, Any]]] = {}
    departed: set[str] = set()
    team_names: set[str] = set()
    for scan in SCANS:
        try:
            schema = repo.data_source_properties(scan.db)
        except Exception as exc:  # noqa: BLE001 - one database's schema must not stop the others
            failures.append(f"{scan.db}: schema could not be read ({_short(exc)})")
            continue
        for prop, is_team in ((scan.who, False), (scan.team_select, True)):
            if prop is None:
                continue
            options = _options(schema.get(prop))
            if not options and prop not in schema:
                continue
            select_options[(scan.db, prop)] = options
            for option in options:
                name = str(option.get("name") or "")
                if name.strip() and name.strip() not in known:
                    departed.add(name)
                    if is_team:
                        team_names.add(name)

    # Two-run confirmation: the first sighting of each departed name is kept while it stays departed.
    now = ctx.now if ctx.now.tzinfo is not None else ctx.now.replace(tzinfo=timezone.utc)
    previous = _read_pending(repo)
    seen = {name: previous.get(name, now) for name in departed}
    confirmed = sorted(name for name in departed if name in previous and now - previous[name] >= PENDING_DELAY)
    pending = sorted(departed - set(confirmed))
    if seen != previous:
        _write_pending(repo, seen)

    trashed = 0
    dashboards_trashed = 0
    removed = 0
    capped = False
    cap = max(0, int(num(ctx.cfg, K.CLEANUP_PER_RUN)))

    if confirmed:
        # Leftover rows, one database at a time.
        for scan in SCANS:
            if capped:
                break
            try:
                rows = list(repo.rows(scan.db))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{scan.db}: rows could not be read ({_short(exc)})")
                continue
            for row in rows:
                name = next((n for n in confirmed if _is_leftover(row, scan, n, team_names, member_ids, team_ids)), None)
                if name is None:
                    continue
                if trashed + dashboards_trashed >= cap:
                    capped = True
                    break
                try:
                    repo.trash(scan.db, row)
                    trashed += 1
                except Exception as exc:  # noqa: BLE001 - one row must not stop the others
                    failures.append(f"{scan.db} row {row.get('id')}: trash failed ({_short(exc)})")

        # Dashboard pages of the confirmed names, under the Members dashboards page.
        # ``dashboards_open`` is set when a dashboard may still exist: the option removal below
        # must wait for it, because a removed option makes the name undetectable on later runs.
        dashboards_open = True
        if not capped:
            try:
                children = repo.child_pages(DASHBOARD_PARENT_PAGE_ID)
            except Exception as exc:  # noqa: BLE001
                children = []
                failures.append(f"dashboards: child pages could not be listed ({_short(exc)})")
                dashboards_open = True
            else:
                dashboards_open = False
            titles = {template.format(name=name): name for name in confirmed for template in DASHBOARD_TITLES}
            for page_id, title in children:
                if title not in titles:
                    continue
                if trashed + dashboards_trashed >= cap:
                    capped = True
                    dashboards_open = True
                    break
                try:
                    repo.trash_page_id(page_id)
                    dashboards_trashed += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"dashboard {titles[title]}: trash failed ({_short(exc)})")
                    dashboards_open = True

        # Option removal, once no row of that database still has the value, and only when no
        # dashboard of a confirmed name is left (see above).
        removals_allowed = not capped and not dashboards_open
        for (db, prop), options in select_options.items():
            if not removals_allowed:
                break
            names = [n for n in confirmed if any(o.get("name") == n for o in options)]
            if not names:
                continue
            try:
                rows = repo.rows(db)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{db}.{prop}: rows could not be read ({_short(exc)})")
                continue
            gone = [n for n in names if not any(get_select(r, prop) == n for r in rows)]
            if not gone:
                continue
            kept = [o for o in options if o.get("name") not in gone]
            try:
                repo.update_select_options(db, prop, _payload(kept))
                removed += len(gone)
            except Exception as exc:  # noqa: BLE001 - the rows are gone already; the option can stay
                failures.append(f"{db}.{prop}: option {', '.join(gone)} not removed ({_short(exc)})")

    details = [
        f"departed names pending: {pending}",
        f"confirmed: {confirmed}",
        f"trashed: {trashed} (capped: {'yes' if capped else 'no'})",
        f"dashboards trashed: {dashboards_trashed}",
        f"select options removed: {removed}",
        *(f"FAILED {line}" for line in failures),
    ]
    result = WARNING if failures else OK
    items = trashed + dashboards_trashed + removed
    return JobResult(job=JOB, result=result, items=items, details="\n".join(details))
