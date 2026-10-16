"""Job cleanup: two-run confirmation, trash rules, cap, dashboards, select options, switch.
Offline (FakeNotion). Each "run" builds a fresh Context (fresh cache), as a separate CLI run would."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from hq import config
from hq import main as cli
from hq import notion as nt
from hq import schema as S
from hq.jobs import OK, WARNING, cleanup
from hq.jobs.dashboards import DASHBOARD_PARENT_PAGE_ID
from hq.notion import NotionError
from hq.repo import get_select
from tests.fakes import NOW, build_world

LATER = NOW + timedelta(minutes=20)  # past the 15-minute window


def _run(w, now=NOW, **kwargs):
    return cleanup.run(w.context(now=now, **kwargs))


def _pending(w) -> list[str]:
    raw = config.get_state(w.context().repo, config.STATE_CLEANUP_PENDING)
    return sorted(json.loads(raw)["names"]) if raw else []


def _ghost_rows(w) -> dict[str, str]:
    """Leftovers of a deleted member "Ghost": a row linked to a Members row that no longer exists,
    and a row with no member link at all."""
    return {
        "problem": w.notion.seed(
            S.DB_PROBLEMS,
            {
                S.P_Problems.TITLE: nt.p_title("Ghost solve"),
                S.P_Problems.WHO: nt.p_select("Ghost"),
                S.P_Problems.MEMBER: nt.p_relation(["deleted-member"]),
            },
        ),
        "task": w.notion.seed(S.DB_TASKS, {S.P_Tasks.TITLE: nt.p_title("Ghost task"), S.P_Tasks.WHO: nt.p_select("Ghost")}),
    }


def _config(w, key: str, value: int) -> None:
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title(key), S.P_Config.NUMBER: nt.p_number(value)})


def _who_options(w, db: str, prop: str = S.P_Problems.WHO) -> list[str]:
    return [o["name"] for o in w.notion.options.get(w.ids[db], {}).get(prop, [])]


def test_first_sighting_is_only_recorded() -> None:
    w = build_world()
    ids = _ghost_rows(w)
    result = _run(w)
    assert result.result == OK
    assert result.items == 0
    assert w.notion.trashed == []
    assert _pending(w) == ["Ghost"]
    assert ids["task"] in [r["id"] for r in w.notion.rows(S.DB_TASKS)]


def test_second_sighting_after_the_window_acts() -> None:
    w = build_world()
    ids = _ghost_rows(w)
    _run(w)
    result = _run(w, LATER)
    assert result.result == OK
    assert set(w.notion.trashed) == {ids["problem"], ids["task"]}
    assert result.items == 4  # two rows and two select options (Problems, Tasks)
    assert "confirmed: ['Ghost']" in result.details
    assert "trashed: 2 (capped: no)" in result.details


def test_not_before_fifteen_minutes() -> None:
    w = build_world()
    _ghost_rows(w)
    _run(w)
    result = _run(w, NOW + timedelta(minutes=10))
    assert w.notion.trashed == []
    assert "confirmed: []" in result.details
    assert _pending(w) == ["Ghost"]


def test_exactly_fifteen_minutes_is_enough() -> None:
    w = build_world()
    _ghost_rows(w)
    _run(w)
    _run(w, NOW + timedelta(minutes=15))
    assert len(w.notion.trashed) == 2


def test_members_read_failure_does_nothing(monkeypatch) -> None:
    w = build_world()
    _ghost_rows(w)
    _run(w)
    ctx = w.context(now=LATER)
    real_rows = ctx.repo.rows

    def failing_rows(db: str):
        if db == S.DB_MEMBERS:
            raise NotionError("POST /data_sources/x/query -> HTTP 500", status=500)
        return real_rows(db)

    monkeypatch.setattr(ctx.repo, "rows", failing_rows)
    result = cleanup.run(ctx)
    assert result.result == WARNING
    assert result.items == 0
    assert w.notion.trashed == []
    assert _pending(w) == ["Ghost"], "the pending state is left as it was"


def test_members_with_no_rows_does_nothing() -> None:
    w = build_world()
    ids = _ghost_rows(w)
    _run(w)
    w.notion.tables[w.ids[S.DB_MEMBERS]] = []
    result = _run(w, LATER)
    assert result.result == WARNING
    assert "no rows" in result.details
    assert w.notion.trashed == []
    assert ids["task"] in [r["id"] for r in w.notion.rows(S.DB_TASKS)]


def test_rows_with_a_live_member_are_kept_as_a_rename() -> None:
    w = build_world()
    _ghost_rows(w)
    renamed = w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("Renamed member's solve"),
            S.P_Problems.WHO: nt.p_select("Ghost"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
        },
    )
    _run(w)
    _run(w, LATER)
    assert renamed not in w.notion.trashed
    assert w.notion.page(renamed) is not None
    assert "Ghost" in _who_options(w, S.DB_PROBLEMS), "an option still in use is not removed"


def test_team_name_departure_trashes_only_unlinked_rows() -> None:
    w = build_world()
    # "Old Squad" is a team name: it is a Team option of Problems.
    team_problem = w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("Squad solve"),
            S.P_Problems.WHO: nt.p_select("Old Squad"),
            S.P_Problems.TEAM: nt.p_select("Old Squad"),
        },
    )
    team_entry = w.notion.seed(
        S.DB_CONTEST_ENTRIES,
        {
            S.P_ContestEntries.TITLE: nt.p_title("Squad round"),
            S.P_ContestEntries.WHO: nt.p_select("Old Squad"),
            S.P_ContestEntries.TEAM: nt.p_relation(["deleted-team"]),
        },
    )
    live_goal = w.notion.seed(
        S.DB_GOALS,
        {
            S.P_Goals.TITLE: nt.p_title("Squad goal"),
            S.P_Goals.WHO: nt.p_select("Old Squad"),
            S.P_Goals.TEAM: nt.p_relation([w.team]),  # the team row is alive: a renamed team, not a departure
        },
    )
    _run(w)
    result = _run(w, LATER)
    assert set(w.notion.trashed) == {team_problem, team_entry}
    assert live_goal not in w.notion.trashed
    assert "Old Squad" not in _who_options(w, S.DB_PROBLEMS, S.P_Problems.TEAM), "the Team option goes once no row uses it"
    assert "Old Squad" in _who_options(w, S.DB_GOALS, S.P_Goals.WHO), "the Goals row still uses the Who option"
    assert "Old Squad" in result.details


def test_reserved_names_are_never_departed() -> None:
    w = build_world()
    club = w.notion.seed(S.DB_WEEKLY_SUMMARY, {S.P_WeeklySummary.TITLE: nt.p_title("Club"), S.P_WeeklySummary.WHO: nt.p_select("Club"), S.P_WeeklySummary.SCOPE: nt.p_select("Club")})
    unassigned = w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("Loose"), S.P_Problems.WHO: nt.p_select("Unassigned")})
    no_who = w.notion.seed(S.DB_TASKS, {S.P_Tasks.TITLE: nt.p_title("Loose task"), S.P_Tasks.WHO: nt.p_select("No Who")})
    team_word = w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("Team word"), S.P_Problems.WHO: nt.p_select("Eyad"), S.P_Problems.TEAM: nt.p_select("Team")})
    _ghost_rows(w)
    _run(w)
    _run(w, LATER)
    assert {club, unassigned, no_who, team_word}.isdisjoint(w.notion.trashed)
    assert "Club" in _who_options(w, S.DB_WEEKLY_SUMMARY, S.P_WeeklySummary.WHO)


def test_cap_and_continuation_on_later_runs() -> None:
    w = build_world()
    _config(w, "CLEANUP_PER_RUN", 2)
    for i in range(5):
        w.notion.seed(S.DB_TASKS, {S.P_Tasks.TITLE: nt.p_title(f"Ghost {i}"), S.P_Tasks.WHO: nt.p_select("Ghost")})
    _run(w)
    first = _run(w, LATER)
    assert len(w.notion.trashed) == 2
    assert "trashed: 2 (capped: yes)" in first.details
    second = _run(w, LATER + timedelta(minutes=10))
    assert len(w.notion.trashed) == 4
    assert "capped: yes" in second.details
    third = _run(w, LATER + timedelta(minutes=20))
    assert len(w.notion.trashed) == 5
    assert "trashed: 1 (capped: no)" in third.details
    assert "Ghost" not in _who_options(w, S.DB_TASKS, S.P_Tasks.WHO), "the option goes once its rows are gone"


def test_dashboard_pages_of_the_departed_name_are_trashed() -> None:
    w = build_world()
    _ghost_rows(w)
    plain = w.notion.create_child_page(DASHBOARD_PARENT_PAGE_ID, "Ghost · Dashboard")["id"]
    emoji = w.notion.create_child_page(DASHBOARD_PARENT_PAGE_ID, "👤 Ghost · Dashboard")["id"]
    live = w.notion.create_child_page(DASHBOARD_PARENT_PAGE_ID, "Eyad · Dashboard")["id"]
    _run(w)
    result = _run(w, LATER)
    assert plain in w.notion.trashed and emoji in w.notion.trashed
    assert live not in w.notion.trashed
    assert "dashboards trashed: 2" in result.details
    assert result.items == 6  # two rows, two dashboards and two select options


def test_dashboards_count_toward_the_cap() -> None:
    w = build_world()
    _config(w, "CLEANUP_PER_RUN", 1)
    _ghost_rows(w)
    dash = w.notion.create_child_page(DASHBOARD_PARENT_PAGE_ID, "Ghost · Dashboard")["id"]
    _run(w)
    result = _run(w, LATER)
    assert len(w.notion.trashed) == 1
    assert dash not in w.notion.trashed
    assert "capped: yes" in result.details
    _run(w, LATER + timedelta(minutes=10))  # the second run's one page goes to the remaining row
    assert dash not in w.notion.trashed
    _run(w, LATER + timedelta(minutes=20))
    assert dash in w.notion.trashed


def test_select_option_is_removed_once_no_row_uses_it() -> None:
    w = build_world()
    _ghost_rows(w)
    w.notion.seed(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("Eyad solve"), S.P_Problems.WHO: nt.p_select("Eyad"), S.P_Problems.MEMBER: nt.p_relation([w.eyad])})
    before = {o["name"]: o["id"] for o in w.notion.options[w.ids[S.DB_PROBLEMS]][S.P_Problems.WHO]}
    _run(w)
    result = _run(w, LATER)
    assert _who_options(w, S.DB_PROBLEMS) == ["Eyad"]
    assert w.notion.options[w.ids[S.DB_PROBLEMS]][S.P_Problems.WHO][0]["id"] == before["Eyad"], "kept options keep their ids"
    assert (w.ids[S.DB_PROBLEMS], S.P_Problems.WHO, ["Eyad"]) in w.notion.option_updates
    assert "select options removed: 2" in result.details  # Problems and Tasks
    assert result.result == OK


def test_refused_option_removal_is_a_warning_and_rows_stay_trashed() -> None:
    w = build_world()
    ids = _ghost_rows(w)
    w.notion.option_update_faults = True
    _run(w)
    result = _run(w, LATER)
    assert set(w.notion.trashed) == {ids["problem"], ids["task"]}
    assert result.result == WARNING
    assert "FAILED" in result.details
    assert "select options removed: 0" in result.details
    assert result.items == 2
    assert "Ghost" in _who_options(w, S.DB_PROBLEMS)


def test_switch_off_does_nothing() -> None:
    w = build_world()
    _config(w, "MEMBER_CLEANUP", 0)
    ids = _ghost_rows(w)
    _run(w)
    result = _run(w, LATER)
    assert result.skipped is True
    assert result.items == 0
    assert w.notion.trashed == []
    assert ids["task"] in [r["id"] for r in w.notion.rows(S.DB_TASKS)]


def test_a_name_known_again_is_dropped_from_pending() -> None:
    w = build_world()
    _ghost_rows(w)
    _run(w)
    member = w.notion.seed(S.DB_MEMBERS, {S.P_Members.TITLE: nt.p_title("Ghost"), S.P_Members.ACTIVE: nt.p_checkbox(False)})
    result = _run(w, LATER)
    assert result.items == 0
    assert w.notion.trashed == []
    assert _pending(w) == []
    # Deleted again: the window starts over, so no action at the first new sighting.
    w.notion.tables[w.ids[S.DB_MEMBERS]] = [r for r in w.notion.rows(S.DB_MEMBERS) if r["id"] != member]
    _run(w, LATER + timedelta(minutes=10))
    assert w.notion.trashed == []
    _run(w, LATER + timedelta(minutes=30))
    assert len(w.notion.trashed) == 2


def test_dry_run_writes_nothing_but_lists_the_trashes() -> None:
    w = build_world()
    _ghost_rows(w)
    _run(w)
    ctx = w.context(dry_run=True, now=LATER)
    result = cleanup.run(ctx)
    assert w.notion.trashed == []
    assert any(line.startswith("trash ") for line in ctx.repo.write_log)
    assert "trashed: 2" in result.details


def test_cleanup_is_a_cli_job_and_runs_last_in_auto() -> None:
    assert cli.JOBS["cleanup"] is cleanup.run
    assert "cleanup" in cli.CHOICES
    w = build_world()
    results = cli.run_auto(w.context())
    assert results[-1].job == "cleanup"


def test_workflow_offers_cleanup_as_a_job() -> None:
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "hq.yml").read_text(encoding="utf-8")
    assert "backfill-weekly, cleanup]" in text


def test_trash_and_option_update_requests() -> None:
    from hq.notion import NOTION_API, Notion
    from tests.helpers import FakeResponse, FakeSession

    session = FakeSession(lambda _m, _u, _k: FakeResponse(200, {"id": "x"}))
    client = Notion("token", session=session, sleep=lambda _s: None, min_interval=0)
    client.trash_page("page-1")
    client.update_data_source("ds-1", {"Who": {"select": {"options": [{"name": "Eyad"}]}}})
    trash, update = session.calls
    assert (trash["method"], trash["url"], trash["json"]) == ("PATCH", NOTION_API + "/pages/page-1", {"in_trash": True})
    assert (update["method"], update["url"]) == ("PATCH", NOTION_API + "/data_sources/ds-1")
    assert update["json"] == {"properties": {"Who": {"select": {"options": [{"name": "Eyad"}]}}}}


def test_all_block_children_follows_the_cursor() -> None:
    from hq.notion import NOTION_API, Notion
    from tests.helpers import FakeResponse, FakeSession

    pages = {
        None: {"results": [{"id": "a", "type": "child_page"}], "has_more": True, "next_cursor": "c1"},
        "c1": {"results": [{"id": "b", "type": "paragraph"}], "has_more": False, "next_cursor": None},
    }

    def handler(_m, _u, kwargs):
        return FakeResponse(200, pages[kwargs["params"].get("start_cursor")])

    session = FakeSession(handler)
    client = Notion("token", session=session, sleep=lambda _s: None, min_interval=0)
    blocks = client.all_block_children("parent")
    assert [b["id"] for b in blocks] == ["a", "b"]
    assert session.urls() == [NOTION_API + "/blocks/parent/children"] * 2
