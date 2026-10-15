"""Job ``dashboards``: personal dashboards, one page per active member, with linked database
views filtered to that member (Config DASHBOARDS_AUTO, default 1).

For each active member whose Members "Dashboard" URL is empty, the page
"👤 <Name> · Dashboard" is created under the Members page. It holds a header callout and
linked views (``create_database`` on the Views API, Notion-Version 2026-03-11):

* Tasks (open), Problems (newest first), Goals and Achievements, each filtered to Who = name;
* Daily points, Weekly points (Scope = Person) and Rating history as line charts;
* Learning Queue as a board grouped by Queue Status.

Every view filters on Who = name. Notion lists a name as an option of a Who select only once a
job has written it, so the job runs after recompute and daily (see ``hq.main.run_auto``). A view
the API still rejects is retried by the repair pass, which runs once per Cairo day, right after a
recompute, or on a forced run. The repair pass finds the views already on a page by the marker
paragraph each created view leaves (``hq-view: <database title>``) and by the database title of
linked-view blocks; it creates only the missing views, once each per member per pass.

A chart or board that the API rejects (HTTP 400) is created as a table with the same filter.
Every failed view is reported with its Notion error text (truncated) in the job details.
The Dashboard URL is written right after the page is created, so a member gets one dashboard.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from hq import config
from hq import icons
from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import OK, WARNING, Context, JobResult
from hq.notion import NotionError
from hq.repo import Member, Repo, get_url
from hq.schema import ConfigKey as K

log = logging.getLogger("hq.jobs.dashboards")

JOB = "dashboards"
# The Members page of MUST CPC Training HQ (where dashboards are created).
DASHBOARD_PARENT_PAGE_ID = "3fa60538-f4c5-81c5-9744-c9baafe842c2"
MARKER_PREFIX = "hq-view: "  # paragraph written after a view is created; the rest is the view's database title
# Block types a linked database view can appear as on a page (the exact type is not verified).
VIEW_BLOCK_TYPES = ("child_database", "link_to_database", "linked_database")
ERROR_CHARS = 200  # Notion error text kept in the job details


@dataclass
class _View:
    name: str
    db: str
    type: str
    filter: dict[str, Any]
    sorts: list[dict[str, Any]] = field(default_factory=list)
    chart: dict[str, Any] | None = None  # chart configuration without the "type" key
    board_group: str | None = None  # Status property to group a board by


@dataclass
class Report:
    pages: int = 0  # dashboard pages created
    views: int = 0  # views created on new pages
    repaired: int = 0  # missing views created by the repair pass
    failures: list[str] = field(default_factory=list)


def _who(name: str) -> dict[str, Any]:
    return {"property": S.P_Problems.WHO, "select": {"equals": name}}


def _and(*filters: dict[str, Any]) -> dict[str, Any]:
    return {"and": list(filters)}


def _line_chart(
    x_prop: str,
    x_group: str,
    y_prop: str,
    aggregator: str,
    props: dict[str, str],
) -> dict[str, Any] | None:
    """Line chart configuration from property ids, or None when an id is missing."""
    if x_prop not in props or y_prop not in props:
        return None
    y_axis: dict[str, Any] = {"aggregator": aggregator, "property_id": props[y_prop]}
    return {
        "chart_type": "line",
        "x_axis": {
            "type": "date",
            "property_id": props[x_prop],
            "group_by": x_group,
            "hide_empty_groups": False,
            "start_day_of_week": 1,
        },
        "y_axis": y_axis,
    }


def _views(name: str, ids: dict[str, dict[str, str]]) -> list[_View]:
    who = _who(name)
    open_tasks = _and(
        who,
        {"property": S.P_Tasks.STATUS, "status": {"does_not_equal": "Done"}},
        {"property": S.P_Tasks.STATUS, "status": {"does_not_equal": "Skipped"}},
        {"property": S.P_Tasks.STATUS, "status": {"does_not_equal": "Missed"}},
    )
    daily = _line_chart(S.P_DailyLog.DATE, "day", S.P_DailyLog.POINTS, "sum", ids.get(S.DB_DAILY_LOG, {}))
    weekly = _line_chart(
        S.P_WeeklySummary.WEEK_START, "week", S.P_WeeklySummary.POINTS, "sum", ids.get(S.DB_WEEKLY_SUMMARY, {})
    )
    rating = _line_chart(S.P_RatingHistory.DATE, "day", S.P_RatingHistory.NEW, "max", ids.get(S.DB_RATING_HISTORY, {}))
    return [
        _View("Tasks (open)", S.DB_TASKS, "table", open_tasks, [{"property": S.P_Tasks.DUE, "direction": "ascending"}]),
        _View(
            "Daily points",
            S.DB_DAILY_LOG,
            "chart",
            who,
            [{"property": S.P_DailyLog.DATE, "direction": "ascending"}],
            daily,
        ),
        _View(
            "Weekly points",
            S.DB_WEEKLY_SUMMARY,
            "chart",
            _and(who, {"property": S.P_WeeklySummary.SCOPE, "select": {"equals": "Person"}}),
            [{"property": S.P_WeeklySummary.WEEK_START, "direction": "ascending"}],
            weekly,
        ),
        _View(
            "Rating history",
            S.DB_RATING_HISTORY,
            "chart",
            who,
            [{"property": S.P_RatingHistory.DATE, "direction": "ascending"}],
            rating,
        ),
        _View("Learning Queue", S.DB_LEARNING_QUEUE, "board", who, board_group=S.P_LearningQueue.QUEUE_STATUS),
        _View(
            "Problems",
            S.DB_PROBLEMS,
            "table",
            who,
            [{"property": S.P_Problems.SOLVED_ON, "direction": "descending"}],
        ),
        _View("Goals", S.DB_GOALS, "table", who),
        _View("Achievements", S.DB_ACHIEVEMENTS, "gallery", who),
    ]


def _property_ids(repo: Repo) -> dict[str, dict[str, str]]:
    """{database title: {property name: property id}} for the databases the charts use."""
    out: dict[str, dict[str, str]] = {}
    for db in (S.DB_DAILY_LOG, S.DB_WEEKLY_SUMMARY, S.DB_RATING_HISTORY, S.DB_LEARNING_QUEUE):
        try:
            props = repo.data_source_properties(db)
        except NotionError as exc:
            log.warning("could not read the %s schema for dashboard charts: %s", db, exc)
            props = {}
        out[db] = {
            name: str(meta.get("id"))
            for name, meta in props.items()
            if isinstance(meta, dict) and meta.get("id")
        }
    return out


def _header(name: str) -> dict[str, Any]:
    text = (
        f"Dashboard for {name}. Every view is filtered to Who = {name}. "
        "Daily and weekly charts cover the history kept in Daily Log and Weekly Summary."
    )
    return {
        "object": "block",
        "type": "callout",
        "callout": {"icon": {"type": "emoji", "emoji": "👤"}, "rich_text": nt.rich_text(text)},
    }


def _marker(db: str) -> dict[str, Any]:
    """Paragraph recording that a view of ``db`` exists on the page (gray, so it reads as a note)."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": nt.rich_text(MARKER_PREFIX + db), "color": "gray"},
    }


def _view_body(view: _View, page_id: str, repo: Repo, configuration: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": view.name,
        "type": view.type,
        "data_source_id": repo.ds(view.db),
        "create_database": {"parent": {"type": "page_id", "page_id": page_id}},
        "filter": view.filter,
    }
    if view.sorts:
        body["sorts"] = view.sorts
    if configuration is not None:
        body["configuration"] = configuration
    return body


def _create_view(repo: Repo, view: _View, page_id: str, member_name: str) -> None:
    """Create one linked view. A chart or board the API rejects becomes a table. Raises
    NotionError with both messages when the table fallback fails too."""
    try:
        if view.type == "chart":
            if view.chart is None:
                raise NotionError("chart property ids are missing")
            repo.create_view(_view_body(view, page_id, repo, {"type": "chart", **view.chart}))
        elif view.type == "board":
            if view.board_group is None:
                raise NotionError("board group property is missing")
            group = {"type": "status", "property_id": view.board_group}
            repo.create_view(_view_body(view, page_id, repo, {"type": "board", "group_by": group}))
        else:
            repo.create_view(_view_body(view, page_id, repo, None))
    except NotionError as exc:
        log.warning("view %s for %s created as a table instead: %s", view.name, member_name, exc)
        table = _view_body(view, page_id, repo, {"type": "table"})
        table["type"] = "table"
        try:
            repo.create_view(table)
        except NotionError as exc2:
            raise NotionError(f"{exc}; the table fallback also failed: {exc2}") from exc2


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:ERROR_CHARS]}"


def _attempt(repo: Repo, view: _View, page_id: str, member: Member, report: Report) -> bool:
    """Create one view and its marker. A failure is recorded in ``report``; returns success."""
    try:
        _create_view(repo, view, page_id, member.name)
        repo.append(page_id, [_marker(view.db)])
    except Exception as exc:  # noqa: BLE001 - one view must not stop the others or the dashboard
        log.warning("view %s for %s was not created: %s", view.name, member.name, _error_text(exc))
        report.failures.append(f"{member.name} / view {view.name}: {_error_text(exc)}")
        return False
    return True


def build_dashboard(ctx: Context, member: Member, report: Report, ids: dict[str, dict[str, str]]) -> str:
    """Create one member's dashboard page and its views. Returns the page URL."""
    repo = ctx.repo
    title = f"👤 {member.name} · Dashboard"
    page = repo.create_child_page(DASHBOARD_PARENT_PAGE_ID, title, [_header(member.name)], icon=icons.ui("member"))
    url = page.get("url") or ""
    # The URL is written before any view is created: a view failure must never leave the
    # member without a Dashboard URL, or the next run would create a duplicate page.
    if url:
        repo.update(S.DB_MEMBERS, member.page, {S.P_Members.DASHBOARD: nt.p_url(url)})
    for view in _views(member.name, ids):
        if _attempt(repo, view, page["id"], member, report):
            report.views += 1
    return url


_HEX32_END = re.compile(r"([0-9a-f]{32})$")


def _page_id_from_url(url: str) -> str | None:
    """The page id at the end of a Notion page URL ("<Title>-<32 hex digits>"), or None."""
    segment = urlparse(url).path.rstrip("/").split("/")[-1]
    match = _HEX32_END.search(segment.replace("-", "").lower())
    return match.group(1) if match else None


def _block_text(block: dict[str, Any]) -> str:
    rich = (block.get("paragraph") or {}).get("rich_text") or []
    return "".join(item.get("plain_text") or (item.get("text") or {}).get("content", "") for item in rich)


def _present_views(children: list[dict[str, Any]], views: list[_View]) -> set[str]:
    """Database titles of the views already on the page: from marker paragraphs, and from
    the database title (or view name) of linked-view blocks."""
    found: set[str] = set()
    for block in children:
        btype = block.get("type")
        if btype in VIEW_BLOCK_TYPES:
            title = str(((block.get(btype) or {}).get("title")) or "").strip()
            found.update(v.db for v in views if title in (v.db, v.name))
        elif btype == "paragraph":
            text = _block_text(block)
            if text.startswith(MARKER_PREFIX):
                found.add(text[len(MARKER_PREFIX) :].strip())
    return found


def _repair_dashboard(ctx: Context, member: Member, ids: dict[str, dict[str, str]], report: Report) -> None:
    """Create the views missing from one member's dashboard (each at most once in this pass)."""
    repo = ctx.repo
    page_id = _page_id_from_url(get_url(member.page, S.P_Members.DASHBOARD) or "")
    if page_id is None:
        report.failures.append(f"{member.name} / repair: the Dashboard URL is not a Notion page link")
        return
    views = _views(member.name, ids)
    found = _present_views(repo.block_children(page_id), views)
    for view in views:
        if view.db in found:
            continue
        if _attempt(repo, view, page_id, member, report):
            report.repaired += 1


def _repair_due(ctx: Context) -> bool:
    """True once per Cairo day (STATE_LAST_DASHBOARD_REPAIR), unless the state is already today."""
    return config.get_state(ctx.repo, config.STATE_LAST_DASHBOARD_REPAIR) != ctx.today.isoformat()


def run(ctx: Context, *, repair_now: bool = False) -> JobResult:
    """Create missing dashboards, then repair the views of existing ones when the repair is due.

    ``repair_now`` is set by the auto sequence right after a recompute (the Who options it
    writes are what the views need); the repair also runs once per Cairo day and on --force."""
    if int(num(ctx.cfg, K.DASHBOARDS_AUTO)) != 1:
        return JobResult(job=JOB, result=OK, items=0, details="dashboards: off (DASHBOARDS_AUTO is 0)")
    repo = ctx.repo
    members = ctx.active_members()
    needs_page = [m for m in members if not get_url(m.page, S.P_Members.DASHBOARD)]
    has_page = [m for m in members if get_url(m.page, S.P_Members.DASHBOARD)]
    repair = bool(has_page) and (repair_now or ctx.force or _repair_due(ctx))
    report = Report()
    if needs_page or repair:
        ids = _property_ids(repo)
        for member in needs_page:
            try:
                build_dashboard(ctx, member, report, ids)
                report.pages += 1
            except Exception as exc:  # noqa: BLE001 - one member's dashboard must not stop the others
                log.exception("dashboard failed for %s", member.name)
                report.failures.append(f"{member.name} / dashboard page: {_error_text(exc)}")
        if repair:
            for member in has_page:
                try:
                    _repair_dashboard(ctx, member, ids, report)
                except Exception as exc:  # noqa: BLE001
                    log.exception("dashboard repair failed for %s", member.name)
                    report.failures.append(f"{member.name} / repair: {_error_text(exc)}")
            config.set_state(repo, config.STATE_LAST_DASHBOARD_REPAIR, ctx.today.isoformat())
    details = [
        f"dashboard pages created: {report.pages}",
        f"views created on new pages: {report.views}",
        f"missing views repaired: {report.repaired}",
        *(f"FAILED {line}" for line in report.failures),
    ]
    result = WARNING if report.failures else OK
    items = report.pages + report.views + report.repaired
    return JobResult(job=JOB, result=result, items=items, details="\n".join(details))
