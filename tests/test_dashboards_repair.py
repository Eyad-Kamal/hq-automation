"""Dashboards job: created after recompute and daily, repair of missing views (once per day,
capped, idempotent), and Notion error text in the job details. Offline (FakeNotion)."""

from __future__ import annotations

from hq import main as cli
from hq import notion as nt
from hq.notion import NotionError
from hq import schema as S
from hq.jobs import OK, WARNING, dashboards
from hq.jobs import onboard
from hq.repo import get_url
from tests.fakes import NOW, build_world


def _page_id(w, member_id: str) -> str:
    page_id = dashboards._page_id_from_url(get_url(w.notion.page(member_id), S.P_Members.DASHBOARD))
    assert page_id is not None
    return page_id


def _is_marker(block: dict, db: str | None = None) -> bool:
    if block.get("type") != "paragraph":
        return False
    text = dashboards._block_text(block)
    return text.startswith(dashboards.MARKER_PREFIX) and (db is None or text == dashboards.MARKER_PREFIX + db)


def _remove_markers(w, page_id: str, db: str | None = None) -> None:
    w.notion.blocks[page_id] = [b for b in w.notion.blocks.get(page_id, []) if not _is_marker(b, db)]


def _views_named(w, name: str) -> list[dict]:
    return [v for v in w.notion.views if v["name"] == name]


def test_url_of_a_dashboard_gives_its_page_id() -> None:
    url = "https://www.notion.so/Eyad-Dashboard-3fa60538f4c581c59744c9baafe842c2?pvs=4"
    assert dashboards._page_id_from_url(url) == "3fa60538f4c581c59744c9baafe842c2"
    assert dashboards._page_id_from_url("not a page") is None


def test_each_created_view_leaves_a_marker_on_the_page() -> None:
    w = build_world()
    onboard.run(w.context())
    result = dashboards.run(w.context())
    assert result.result == OK
    page_id = _page_id(w, w.eyad)
    markers = [dashboards._block_text(b) for b in w.notion.blocks[page_id] if _is_marker(b)]
    assert len(markers) == 8
    assert "hq-view: " + S.DB_GOALS in markers


def test_failed_views_are_reported_with_the_notion_error_text() -> None:
    w = build_world()
    w.notion.view_faults = {"*"}  # every view create fails with HTTP 500 (injected)
    onboard.run(w.context())
    result = dashboards.run(w.context())
    assert result.result == WARNING
    assert "HTTP 500 (injected)" in result.details
    failure_lines = [line for line in result.details.splitlines() if line.startswith("FAILED ")]
    assert failure_lines and all(len(line) < 320 for line in failure_lines), "the error text is truncated"


def test_repair_creates_only_the_missing_view_and_is_idempotent() -> None:
    w = build_world()
    onboard.run(w.context())
    dashboards.run(w.context())
    total = len(w.notion.views)
    page_id = _page_id(w, w.eyad)
    _remove_markers(w, page_id, S.DB_GOALS)  # as if the Goals view had never been created

    result = dashboards.run(w.context(), repair_now=True)
    assert result.items == 1
    assert "missing views repaired: 1" in result.details
    assert len(w.notion.views) == total + 1
    assert len(_views_named(w, "Goals")) == 3  # Goals for Eyad and Sara, plus Eyad's repaired one
    repaired = w.notion.views[-1]
    assert repaired["name"] == "Goals"
    assert repaired["filter"] == {"property": "Member", "relation": {"contains": w.eyad}}, "Goals is filtered by the member relation"

    again = dashboards.run(w.context(), repair_now=True)
    assert again.items == 0
    assert len(w.notion.views) == total + 1, "a second repair creates nothing"


def test_repair_runs_once_per_day_unless_forced_or_after_a_recompute() -> None:
    w = build_world()
    onboard.run(w.context())
    dashboards.run(w.context())  # creates the pages; no repair yet
    dashboards.run(w.context())  # the first repair of the day; it marks the day as done
    page_id = _page_id(w, w.eyad)
    _remove_markers(w, page_id, S.DB_GOALS)
    total = len(w.notion.views)

    plain = dashboards.run(w.context())
    assert plain.items == 0 and len(w.notion.views) == total, "already repaired today"

    forced = dashboards.run(w.context(force=True))
    assert forced.items == 1 and len(w.notion.views) == total + 1


def test_repair_attempts_each_missing_view_at_most_once_per_pass() -> None:
    w = build_world()
    onboard.run(w.context())
    dashboards.run(w.context())
    for member in (w.eyad, w.sara):
        _remove_markers(w, _page_id(w, member))  # every view of both dashboards is now "missing"
    w.notion.view_faults = {"*"}  # and every attempt fails
    result = dashboards.run(w.context(), repair_now=True)
    assert result.result == WARNING
    eyad_failures = [line for line in result.details.splitlines() if "Eyad / view" in line]
    assert len(eyad_failures) == 8, "one attempt per missing view, not more"
    assert len([line for line in result.details.splitlines() if "Sara / view" in line]) == 8


def test_present_views_reads_linked_view_blocks_by_database_title() -> None:
    views = dashboards._views("member-eyad", "Eyad", {})
    children = [{"type": "child_database", "child_database": {"title": S.DB_LEARNING_QUEUE}}]
    assert dashboards._present_views(children, views) == {S.DB_LEARNING_QUEUE}


def test_present_views_reads_marker_paragraphs() -> None:
    views = dashboards._views("member-eyad", "Eyad", {})
    children = [dashboards._marker(S.DB_GOALS), {"type": "callout", "callout": {}}]
    assert dashboards._present_views(children, views) == {S.DB_GOALS}


def test_dashboards_job_runs_after_recompute_and_daily_in_the_auto_sequence() -> None:
    w = build_world()
    names = [r.job for r in cli.run_auto(w.context(now=NOW))]
    assert names.index("dashboards") > names.index("daily") > names.index("recompute")


def test_every_view_filters_on_the_member_relation_never_on_who() -> None:
    views = {v.name: v for v in dashboards._views("member-x", "X", {})}
    for view in views.values():
        assert '"Who"' not in str(view.filter), view.name
    assert views["Tasks (open)"].filter["and"][0] == {"property": "Assignee", "relation": {"contains": "member-x"}}
    assert views["Problems"].filter == {"property": "Member", "relation": {"contains": "member-x"}}
    assert views["Goals"].filter == {"property": "Member", "relation": {"contains": "member-x"}}
    assert views["Weekly points"].filter["and"][0] == {"property": "Member", "relation": {"contains": "member-x"}}


def test_every_chart_view_is_created_with_an_x_axis_sort() -> None:
    w = build_world()
    # the fake reads property ids from existing rows, so each chart database needs one row
    w.notion.seed(S.DB_DAILY_LOG, {S.P_DailyLog.TITLE: nt.p_title("d"), S.P_DailyLog.DATE: nt.p_date(NOW.date(), with_time=False), S.P_DailyLog.POINTS: nt.p_number(1)})
    w.notion.seed(S.DB_WEEKLY_SUMMARY, {S.P_WeeklySummary.TITLE: nt.p_title("w"), S.P_WeeklySummary.WEEK_START: nt.p_date(NOW.date(), with_time=False), S.P_WeeklySummary.POINTS: nt.p_number(1)})
    w.notion.seed(S.DB_RATING_HISTORY, {S.P_RatingHistory.TITLE: nt.p_title("r"), S.P_RatingHistory.DATE: nt.p_date(NOW.date(), with_time=False), S.P_RatingHistory.NEW: nt.p_number(1000)})
    onboard.run(w.context())
    dashboards.run(w.context())
    charts = [v for v in w.notion.views if v["type"] == "chart"]
    assert len(charts) == 6, "daily, weekly and rating charts for each of the two active members"
    for chart in charts:
        assert chart["configuration"]["x_axis"]["sort"] == {"type": "ascending"}, chart["name"]


def test_repair_adds_the_missing_tasks_and_rating_views_and_nothing_else() -> None:
    w = build_world()
    onboard.run(w.context())
    dashboards.run(w.context())
    page_id = _page_id(w, w.eyad)
    _remove_markers(w, page_id, S.DB_TASKS)
    _remove_markers(w, page_id, S.DB_RATING_HISTORY)
    total = len(w.notion.views)
    result = dashboards.run(w.context(), repair_now=True)
    assert result.items == 2
    assert sorted(v["name"] for v in w.notion.views[total:]) == ["Rating history", "Tasks (open)"]


def test_onboard_counts_icon_backfill_as_items_and_reports_it() -> None:
    w = build_world()
    w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("p"), S.P_Problems.JUDGE: nt.p_select("Codeforces")})
    result = onboard.run(w.context())
    assert "icons backfilled:" in result.details
    assert "Problems 1" in result.details
    assert result.items >= 1, "icon writes count as items, so they reach the Sync Log"


def test_icon_write_errors_are_reported_with_the_notion_message(monkeypatch) -> None:
    w = build_world()
    w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("p"), S.P_Problems.JUDGE: nt.p_select("Codeforces")})
    original = w.notion.update_page

    def refuse_icons(page_id, properties=None, *, icon=None):
        if icon is not None:
            raise NotionError("PATCH /pages -> HTTP 400: icon not allowed here")
        return original(page_id, properties, icon=icon)

    monkeypatch.setattr(w.notion, "update_page", refuse_icons)
    result = onboard.run(w.context())
    assert result.result == WARNING
    assert "icon write Problems: NotionError" in result.details
    assert "icon not allowed here" in result.details
