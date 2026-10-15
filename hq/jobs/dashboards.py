"""Personal dashboards: one page per active member, with linked database views filtered to
that member (onboard step; Config DASHBOARDS_AUTO, default 1).

For each active member whose Members "Dashboard" URL is empty, the page
"👤 <Name> · Dashboard" is created under the Members page. It holds a header callout and
linked views (``create_database`` on the Views API, Notion-Version 2026-03-11):

* Tasks (open), Problems (newest first), Goals and Achievements, each filtered to Who = name;
* Daily points, Weekly points (Scope = Person) and Rating history as line charts;
* Learning Queue as a board grouped by Queue Status.

A chart or board that the API rejects (HTTP 400) is created as a table with the same filter,
and a Warning is logged. The Dashboard URL is written right after the page is created, so a
member gets one dashboard; if that write fails the next run would create another one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.jobs import Context
from hq.notion import NotionError
from hq.repo import Member, Repo, get_url
from hq.schema import ConfigKey as K

log = logging.getLogger("hq.jobs.dashboards")

# The Members page of MUST CPC Training HQ (where dashboards are created).
DASHBOARD_PARENT_PAGE_ID = "3fa60538-f4c5-81c5-9744-c9baafe842c2"


@dataclass
class _View:
    name: str
    db: str
    type: str
    filter: dict[str, Any]
    sorts: list[dict[str, Any]] = field(default_factory=list)
    chart: dict[str, Any] | None = None  # chart configuration without the "type" key
    board_group: str | None = None  # Status property to group a board by


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
    """Create one linked view. A chart or board the API rejects becomes a table."""
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
        repo.create_view(table)


def build_dashboard(ctx: Context, member: Member) -> str:
    """Create one member's dashboard page and its views. Returns the page URL."""
    repo = ctx.repo
    title = f"👤 {member.name} · Dashboard"
    page = repo.create_child_page(DASHBOARD_PARENT_PAGE_ID, title, [_header(member.name)])
    url = page.get("url") or ""
    # The URL is written before any view is created: a view failure must never leave the
    # member without a Dashboard URL, or the next run would create a duplicate page.
    if url:
        repo.update(S.DB_MEMBERS, member.page, {S.P_Members.DASHBOARD: nt.p_url(url)})
    ids = _property_ids(repo)
    for view in _views(member.name, ids):
        try:
            _create_view(repo, view, page["id"], member.name)
        except Exception as exc:  # noqa: BLE001 - one view must not stop the others or the dashboard
            log.warning("view %s for %s was not created: %s: %s", view.name, member.name, type(exc).__name__, exc)
    return url


def ensure_dashboards(ctx: Context, members: list[Member]) -> int:
    """Create a dashboard for every active member whose Dashboard URL is empty (when
    DASHBOARDS_AUTO is 1). Returns the number of dashboards created."""
    if int(num(ctx.cfg, K.DASHBOARDS_AUTO)) != 1:
        return 0
    created = 0
    for member in members:
        if get_url(member.page, S.P_Members.DASHBOARD):
            continue
        try:
            build_dashboard(ctx, member)
            created += 1
        except Exception:  # noqa: BLE001 - one member's dashboard must not stop the others
            log.exception("dashboard failed for %s", member.name)
    return created
