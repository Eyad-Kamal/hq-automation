"""Seasons: the SEASON_START config, effective starts, In Season flags, season points, the flag cap.

A season start deletes nothing. These tests check that the all-time numbers stay as they were
and that only the season flags and Season Points / Season Solved change.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from hq import config
from hq import notion as nt
from hq import schema as S
from hq.jobs import imports, onboard, recompute, weekly
from hq.logic import scoring
from hq.logic.season import Season, effective_start, in_season
from hq.repo import Repo, cairo_day_of, get_checkbox, get_date, get_number, get_relation, get_text
from hq.timeutil import cairo_date
from tests.fakes import NOW, NOW_SATURDAY, build_world, seed_eyad_solves

SEASON = date(2026, 10, 14)  # 1985C (13 Oct) is before it; 1990A and 1999B (14 Oct) are on or after it
FLAG_PROPS = (S.P_Problems.IN_SEASON, S.P_DailyLog.IN_SEASON, S.P_ContestEntries.IN_SEASON, S.P_WeeklySummary.IN_SEASON)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _config(w, *, value: str | None = None, when=None, key: str = "SEASON_START") -> None:
    props = {S.P_Config.TITLE: nt.p_title(key)}
    if value is not None:
        props[S.P_Config.VALUE] = nt.p_text(value)
    if when is not None:
        props[S.P_Config.DATE] = nt.p_date(when, with_time=isinstance(when, datetime))
    w.notion.seed(S.DB_CONFIG, props)


def _loaded(w) -> dict:
    return config.load(Repo(w.notion, w.ids))


def _setup_with_season(start: date | None = SEASON):
    w = build_world()
    seed_eyad_solves(w)
    if start is not None:
        _config(w, when=start)
    return w


def _chain(w, now=NOW):
    onboard.run(w.context(now=now))
    imports.run(w.context(now=now))
    return recompute.run(w.context(now=now))


def _problem(w, key: str) -> dict:
    rows = [r for r in w.notion.rows(S.DB_PROBLEMS) if get_text(r, S.P_Problems.PROBLEM_KEY) == key]
    assert len(rows) == 1, key
    return rows[0]


def _daily(w, member_id: str, day: date) -> dict:
    key = f"{member_id}:{day.isoformat()}"
    rows = [r for r in w.notion.rows(S.DB_DAILY_LOG) if get_text(r, S.P_DailyLog.SOURCE_KEY) == key]
    assert len(rows) == 1, key
    return rows[0]


def _eyad_points() -> float:
    return round(
        scoring.points(1200, 1150, "Live Contest") + scoring.points(800, 1150, "Upsolve") + scoring.points(1300, 1150, "Practice"),
        2,
    )


def _season_points_of_last_two() -> float:
    # 1990A (Upsolve, 800) and 1999B (Practice, 1300): the solves on or after 14 Oct.
    return round(scoring.points(800, 1150, "Upsolve") + scoring.points(1300, 1150, "Practice"), 2)


def _every_flag_correct(w, season_start: date | None) -> None:
    """Each In Season checkbox equals the rule, computed from the rows themselves."""
    member_start = {m.id: cairo_day_of(get_date(m.page, S.P_Members.SEASON_START)) for m in w.context().repo.members()}
    rule = Season(start=season_start, own_starts=member_start)
    for db, member_prop, date_prop, flag_prop in (
        (S.DB_PROBLEMS, S.P_Problems.MEMBER, S.P_Problems.SOLVED_ON, S.P_Problems.IN_SEASON),
        (S.DB_DAILY_LOG, S.P_DailyLog.MEMBER, S.P_DailyLog.DATE, S.P_DailyLog.IN_SEASON),
    ):
        for row in w.notion.rows(db):
            members = get_relation(row, member_prop)
            expected = in_season(cairo_day_of(get_date(row, date_prop)), rule.start_for(members[0] if members else None))
            assert get_checkbox(row, flag_prop) is expected, (db, row.get("id"))


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------
def test_date_property_is_the_season_start() -> None:
    w = build_world()
    _config(w, when=date(2026, 10, 14))
    assert _loaded(w)["SEASON_START"] == date(2026, 10, 14)


def test_date_with_a_time_uses_its_cairo_day() -> None:
    # 23:30 UTC on 16 Oct is 02:30 on 17 Oct in Cairo (UTC+3 until the end of October).
    w = build_world()
    when = datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc)
    _config(w, when=when)
    assert _loaded(w)["SEASON_START"] == date(2026, 10, 17)
    assert _loaded(w)["SEASON_START"] == cairo_date(when)


def test_value_text_is_parsed_when_there_is_no_date() -> None:
    w = build_world()
    _config(w, value="2026-10-01")
    assert _loaded(w)["SEASON_START"] == date(2026, 10, 1)


def test_date_property_wins_over_value_text() -> None:
    w = build_world()
    _config(w, value="2026-09-01", when=date(2026, 10, 14))
    assert _loaded(w)["SEASON_START"] == date(2026, 10, 14)


def test_invalid_value_text_means_no_season_and_logs_a_warning(caplog) -> None:
    for text in ("next week", "2026-13-01", "16/10/2026"):
        w = build_world()
        _config(w, value=text)
        with caplog.at_level(logging.WARNING, logger="hq.config"):
            caplog.clear()
            assert _loaded(w)["SEASON_START"] is None, text
        assert any("SEASON_START" in r.getMessage() and "invalid" in r.getMessage() for r in caplog.records), text


def test_empty_or_missing_row_means_no_season_without_a_warning(caplog) -> None:
    w = build_world()
    _config(w)  # row exists, Value and Date empty
    with caplog.at_level(logging.WARNING, logger="hq.config"):
        assert _loaded(w)["SEASON_START"] is None
    assert not [r for r in caplog.records if "SEASON_START" in r.getMessage()]
    assert _loaded(build_world())["SEASON_START"] is None, "no row at all"


def test_season_flag_per_run_defaults_to_400() -> None:
    cfg = _loaded(build_world())
    assert cfg["SEASON_FLAG_PER_RUN"] == 400


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------
def test_effective_start_is_the_later_of_global_and_member() -> None:
    a, b = date(2026, 10, 1), date(2026, 10, 14)
    assert effective_start(None, None) is None
    assert effective_start(None, b) == b
    assert effective_start(b, None) == b
    assert effective_start(b, a) == b, "an earlier member start does not reach back before the global start"
    assert effective_start(a, b) == b, "a later member start resets that member"


def test_in_season_rules() -> None:
    s = date(2026, 10, 14)
    assert in_season(None, None) is True
    assert in_season(date(2001, 1, 1), None) is True
    assert in_season(None, s) is False, "no date is not in a season that has started"
    assert in_season(date(2026, 10, 13), s) is False
    assert in_season(s, s) is True


def test_season_start_for_a_member_row_and_a_row_without_one() -> None:
    season = Season(start=date(2026, 10, 14), own_starts={"a": date(2026, 10, 20), "b": date(2026, 10, 1)})
    assert season.start_for("a") == date(2026, 10, 20)
    assert season.start_for("b") == date(2026, 10, 14)
    assert season.start_for(None) == date(2026, 10, 14)
    assert season.start_for("not-a-member") == date(2026, 10, 14)


# ---------------------------------------------------------------------------
# Recompute: flags, season points, reporting
# ---------------------------------------------------------------------------
def test_problem_flags_follow_the_solve_date() -> None:
    w = _setup_with_season()
    _chain(w)
    assert get_checkbox(_problem(w, "CF-1985C"), S.P_Problems.IN_SEASON) is False, "solved 13 Oct"
    assert get_checkbox(_problem(w, "CF-1990A"), S.P_Problems.IN_SEASON) is True, "solved 14 Oct"
    assert get_checkbox(_problem(w, "CF-1999B"), S.P_Problems.IN_SEASON) is True


def test_member_season_points_and_solved_exclude_earlier_solves_but_totals_do_not() -> None:
    w = _setup_with_season()
    _chain(w)
    member = w.notion.page(w.eyad)
    assert abs(get_number(member, S.P_Members.SEASON_POINTS) - _season_points_of_last_two()) < 1e-9
    assert get_number(member, S.P_Members.SEASON_SOLVED) == 2
    assert abs(get_number(member, S.P_Members.TOTAL_POINTS) - _eyad_points()) < 1e-9, "all-time totals unchanged"
    assert get_number(member, S.P_Members.PROBLEMS_TOTAL) == 3


def test_daily_log_flags_follow_the_member_start_and_points_stay_all_time() -> None:
    w = _setup_with_season()
    _chain(w)
    assert get_checkbox(_daily(w, w.eyad, date(2026, 10, 13)), S.P_DailyLog.IN_SEASON) is False
    assert get_checkbox(_daily(w, w.eyad, date(2026, 10, 14)), S.P_DailyLog.IN_SEASON) is True
    assert get_checkbox(_daily(w, w.eyad, date(2026, 10, 15)), S.P_DailyLog.IN_SEASON) is True
    assert get_checkbox(_daily(w, w.eyad, date(2026, 9, 20)), S.P_DailyLog.IN_SEASON) is False, "older window day"
    assert abs(get_number(_daily(w, w.eyad, date(2026, 10, 13)), S.P_DailyLog.POINTS) - round(scoring.points(1200, 1150, "Live Contest"), 2)) < 1e-9


def test_contest_entry_flags_follow_the_member_start() -> None:
    w = _setup_with_season()
    _chain(w)
    entries = [r for r in w.notion.rows(S.DB_CONTEST_ENTRIES) if get_relation(r, S.P_ContestEntries.MEMBER) == [w.eyad]]
    assert entries, "the live contest creates an attended entry for Eyad"
    for entry in entries:
        day = cairo_day_of(get_date(entry, S.P_ContestEntries.DATE))
        assert get_checkbox(entry, S.P_ContestEntries.IN_SEASON) is (day >= SEASON)


def test_team_contest_entries_use_the_global_start_not_a_member_start() -> None:
    w = _setup_with_season()
    w.notion.page(w.eyad)["properties"][S.P_Members.SEASON_START] = nt.p_date(date(2026, 10, 20), with_time=False)
    team_before = w.notion.seed(
        S.DB_CONTEST_ENTRIES,
        {S.P_ContestEntries.TITLE: nt.p_title("Team old"), S.P_ContestEntries.MODE: nt.p_select("Team"),
         S.P_ContestEntries.DATE: nt.p_date(date(2026, 10, 10), with_time=False), S.P_ContestEntries.TEAM: nt.p_relation([w.team])},
    )
    team_after = w.notion.seed(
        S.DB_CONTEST_ENTRIES,
        {S.P_ContestEntries.TITLE: nt.p_title("Team new"), S.P_ContestEntries.MODE: nt.p_select("Team"),
         S.P_ContestEntries.DATE: nt.p_date(date(2026, 10, 15), with_time=False), S.P_ContestEntries.TEAM: nt.p_relation([w.team])},
    )
    _chain(w)
    assert get_checkbox(w.notion.page(team_before), S.P_ContestEntries.IN_SEASON) is False
    assert get_checkbox(w.notion.page(team_after), S.P_ContestEntries.IN_SEASON) is True, "global start 14 Oct, not Eyad's 20 Oct"


def test_member_season_start_later_than_global_resets_that_member() -> None:
    w = _setup_with_season()
    w.notion.page(w.eyad)["properties"][S.P_Members.SEASON_START] = nt.p_date(date(2026, 10, 15), with_time=False)
    _chain(w)
    member = w.notion.page(w.eyad)
    assert get_number(member, S.P_Members.SEASON_SOLVED) == 0
    assert get_number(member, S.P_Members.SEASON_POINTS) == 0
    assert get_checkbox(_problem(w, "CF-1990A"), S.P_Problems.IN_SEASON) is False
    assert get_checkbox(_daily(w, w.eyad, date(2026, 10, 14)), S.P_DailyLog.IN_SEASON) is False
    assert get_checkbox(_daily(w, w.eyad, date(2026, 10, 15)), S.P_DailyLog.IN_SEASON) is True
    assert abs(get_number(member, S.P_Members.TOTAL_POINTS) - _eyad_points()) < 1e-9


def test_member_season_start_earlier_than_global_does_not_reach_back() -> None:
    w = _setup_with_season()
    w.notion.page(w.eyad)["properties"][S.P_Members.SEASON_START] = nt.p_date(date(2026, 10, 1), with_time=False)
    _chain(w)
    assert get_number(w.notion.page(w.eyad), S.P_Members.SEASON_SOLVED) == 2
    assert get_checkbox(_problem(w, "CF-1985C"), S.P_Problems.IN_SEASON) is False


def test_no_season_start_means_everything_is_in_season() -> None:
    w = _setup_with_season(start=None)
    result = _chain(w)
    member = w.notion.page(w.eyad)
    assert get_number(member, S.P_Members.SEASON_SOLVED) == 3
    assert abs(get_number(member, S.P_Members.SEASON_POINTS) - _eyad_points()) < 1e-9
    for row in w.notion.rows(S.DB_PROBLEMS):
        assert get_checkbox(row, S.P_Problems.IN_SEASON) is True
    _every_flag_correct(w, None)
    assert "season start: none; in-season flags changed:" in result.details


def test_details_report_the_season_start_and_flag_count() -> None:
    w = _setup_with_season()
    result = _chain(w)
    assert "season start: 2026-10-14; in-season flags changed:" in result.details
    assert "(capped: no)" in result.details


def test_second_run_writes_no_season_flags() -> None:
    w = _setup_with_season()
    _chain(w)
    w.notion.reset_writes()
    result = recompute.run(w.context())
    assert result.items == 0
    touched = [k for _, upd in w.notion.updates for k in upd if k in FLAG_PROPS]
    assert touched == []
    assert "in-season flags changed: 0 (capped: no)" in result.details


def test_flag_cap_limits_writes_per_run_and_later_runs_finish() -> None:
    w = _setup_with_season()
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("SEASON_FLAG_PER_RUN"), S.P_Config.NUMBER: nt.p_number(1)})
    first = _chain(w)
    assert "in-season flags changed: 1 (capped: yes)" in first.details
    runs = 0
    while True:
        runs += 1
        assert runs < 20, "the cap must not stall the flags forever"
        result = recompute.run(w.context())
        if "(capped: no)" in result.details:
            break
    _every_flag_correct(w, SEASON)


def test_flag_cap_zero_writes_no_flags() -> None:
    w = _setup_with_season()
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("SEASON_FLAG_PER_RUN"), S.P_Config.NUMBER: nt.p_number(0)})
    result = _chain(w)
    assert "in-season flags changed: 0 (capped: yes)" in result.details
    assert get_checkbox(_problem(w, "CF-1990A"), S.P_Problems.IN_SEASON) is False


def test_duplicate_solve_is_not_counted_and_its_row_follows_its_own_date() -> None:
    w = _setup_with_season()
    onboard.run(w.context())
    imports.run(w.context())  # the import adopts a row with the same Problem Key, so seed the duplicate after it
    dup = w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("Permutation Swap again"),
            S.P_Problems.PROBLEM_KEY: nt.p_text("CF-1985C"),
            S.P_Problems.STATUS: nt.p_status("Solved"),
            S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
            S.P_Problems.SOLVED_ON: nt.p_date(datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)),
        },
    )
    recompute.run(w.context())
    member = w.notion.page(w.eyad)
    assert get_number(member, S.P_Members.SEASON_SOLVED) == 2, "the duplicate is the same solve as 1985C"
    assert abs(get_number(member, S.P_Members.SEASON_POINTS) - _season_points_of_last_two()) < 1e-9
    assert get_checkbox(w.notion.page(dup), S.P_Problems.IN_SEASON) is True, "the duplicate row uses its own date"
    original = [r for r in w.notion.rows(S.DB_PROBLEMS) if get_text(r, S.P_Problems.PROBLEM_KEY) == "CF-1985C" and r["id"] != dup]
    assert len(original) == 1
    assert get_checkbox(original[0], S.P_Problems.IN_SEASON) is False


def test_row_without_member_uses_the_global_start() -> None:
    w = _setup_with_season()
    w.notion.page(w.eyad)["properties"][S.P_Members.SEASON_START] = nt.p_date(date(2026, 10, 20), with_time=False)
    memberless = w.notion.seed(
        S.DB_DAILY_LOG,
        {S.P_DailyLog.TITLE: nt.p_title("Team Alpha · 15 Oct"), S.P_DailyLog.TEAM: nt.p_select("Team Alpha"),
         S.P_DailyLog.DATE: nt.p_date(date(2026, 10, 15), with_time=False)},
    )
    _chain(w)
    assert get_checkbox(w.notion.page(memberless), S.P_DailyLog.IN_SEASON) is True


def test_stale_weekly_summary_flag_is_corrected_by_recompute() -> None:
    w = _setup_with_season()  # season 14 Oct; the week of 10 Oct starts before it
    club = w.notion.seed(
        S.DB_WEEKLY_SUMMARY,
        {S.P_WeeklySummary.TITLE: nt.p_title("Club · 2026-10-10 (Sat)"), S.P_WeeklySummary.SCOPE: nt.p_select("Club"),
         S.P_WeeklySummary.WEEK_START: nt.p_date(date(2026, 10, 10), with_time=False),
         S.P_WeeklySummary.SOURCE_KEY: nt.p_text("club:club:2026-10-10"), S.P_WeeklySummary.IN_SEASON: nt.p_checkbox(True)},
    )
    _chain(w)
    assert get_checkbox(w.notion.page(club), S.P_WeeklySummary.IN_SEASON) is False


def test_weekly_summaries_get_the_flag_from_their_scope() -> None:
    w = _setup_with_season(start=date(2026, 10, 10))  # the week of 10 Oct is in season globally
    w.notion.page(w.eyad)["properties"][S.P_Members.SEASON_START] = nt.p_date(date(2026, 10, 17), with_time=False)
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("WEEKLY_BACKFILL_WEEKS"), S.P_Config.NUMBER: nt.p_number(1)})
    onboard.run(w.context(now=NOW_SATURDAY))
    imports.run(w.context(now=NOW_SATURDAY))
    recompute.run(w.context(now=NOW_SATURDAY))
    weekly.run(w.context(now=NOW_SATURDAY))
    rows = {get_text(r, S.P_WeeklySummary.SOURCE_KEY): r for r in w.notion.rows(S.DB_WEEKLY_SUMMARY)}
    week = "2026-10-10"
    assert get_checkbox(rows[f"person:{w.eyad}:{week}"], S.P_WeeklySummary.IN_SEASON) is False, "Eyad restarts on 17 Oct"
    assert get_checkbox(rows[f"person:{w.sara}:{week}"], S.P_WeeklySummary.IN_SEASON) is True, "Sara uses the global start"
    assert get_checkbox(rows[f"team:{w.team}:{week}"], S.P_WeeklySummary.IN_SEASON) is True, "team rows use the global start"
    assert get_checkbox(rows[f"club:club:{week}"], S.P_WeeklySummary.IN_SEASON) is True, "club rows use the global start"
