"""No emoji title prefixes on new rows, the LeetCode daily icon by Source Key, and the title cleanup
that removes the old emoji prefixes from existing Tasks and Weekly Reports (idempotent, capped)."""

from __future__ import annotations

from hq import icons
from hq import notion as nt
from hq import schema as S
from hq.jobs import onboard, weekly
from hq.repo import get_title
from tests.fakes import NOW_SATURDAY, build_world
from tests.test_jobs_daily_weekly import _weekly_setup

LEGACY_PREFIXES = ("🏆 ", "🎯 ", "💡 ", "🔁 ", "📰 ")


def _titles(w, db: str, prop: str) -> list[str]:
    return [get_title(row, prop) for row in w.notion.rows(db)]


def _seed_legacy(w) -> None:
    for title in ("🏆 Codeforces Round 1", "🎯 Greedy: 3 problems", "💡 LeetCode daily: Two Sum", "🔁 Upsolve Round 2"):
        w.notion.seed(S.DB_TASKS, {S.P_Tasks.TITLE: nt.p_title(title)})
    # Left alone: no prefix, an emoji in the middle, and a bare prefix with nothing after it.
    for title in ("Upsolve Round 3", "Read the 🎯 notes", "🏆 "):
        w.notion.seed(S.DB_TASKS, {S.P_Tasks.TITLE: nt.p_title(title)})
    w.notion.seed(S.DB_WEEKLY_REPORTS, {S.P_WeeklyReports.TITLE: nt.p_title("📰 Week of 2026-10-10")})
    w.notion.seed(S.DB_WEEKLY_REPORTS, {S.P_WeeklyReports.TITLE: nt.p_title("Week of 2026-10-17")})
    # Another database: its titles are never touched.
    w.notion.seed(S.DB_GOALS, {S.P_Goals.TITLE: nt.p_title("🎯 Solve more")})


def test_weekly_report_title_has_no_emoji() -> None:
    w = _weekly_setup(admin="user-admin")
    weekly.run(w.context(now=NOW_SATURDAY))
    assert _titles(w, S.DB_WEEKLY_REPORTS, S.P_WeeklyReports.TITLE) == ["Week of 2026-10-10"]


def test_lc_daily_icon_is_chosen_by_source_key_not_by_title() -> None:
    new_title = {S.P_Tasks.TYPE: nt.p_select("Custom"), S.P_Tasks.SOURCE_KEY: nt.p_text("lcdaily:2026-10-15:x"),
                 S.P_Tasks.TITLE: nt.p_title("LeetCode daily: Two Sum")}
    old_title = {S.P_Tasks.TYPE: nt.p_select("Custom"), S.P_Tasks.SOURCE_KEY: nt.p_text("lcdaily:2026-10-15:x"),
                 S.P_Tasks.TITLE: nt.p_title("💡 LeetCode daily: Two Sum")}
    plain = {S.P_Tasks.TYPE: nt.p_select("Custom"), S.P_Tasks.SOURCE_KEY: nt.p_text(""),
             S.P_Tasks.TITLE: nt.p_title("LeetCode daily: Two Sum")}
    assert icons.ui("leetcode_daily") is not None
    assert icons.icon_for(S.DB_TASKS, new_title) == icons.ui("leetcode_daily")
    assert icons.icon_for(S.DB_TASKS, old_title) == icons.ui("leetcode_daily")
    assert icons.icon_for(S.DB_TASKS, plain) == icons.ui("checklist")


def test_cleanup_removes_only_the_old_prefixes_from_tasks_and_weekly_reports() -> None:
    w = build_world()
    _seed_legacy(w)
    result = onboard.run(w.context())
    assert "emoji title prefixes removed: 5" in result.details
    assert _titles(w, S.DB_TASKS, S.P_Tasks.TITLE)[:4] == [
        "Codeforces Round 1",
        "Greedy: 3 problems",
        "LeetCode daily: Two Sum",
        "Upsolve Round 2",
    ]
    assert _titles(w, S.DB_TASKS, S.P_Tasks.TITLE)[4:] == ["Upsolve Round 3", "Read the 🎯 notes", "🏆 "]
    assert _titles(w, S.DB_WEEKLY_REPORTS, S.P_WeeklyReports.TITLE) == ["Week of 2026-10-10", "Week of 2026-10-17"]
    assert _titles(w, S.DB_GOALS, S.P_Goals.TITLE) == ["🎯 Solve more"], "other databases keep their titles"


def test_cleanup_is_idempotent() -> None:
    w = build_world()
    _seed_legacy(w)
    onboard.run(w.context())
    before = _titles(w, S.DB_TASKS, S.P_Tasks.TITLE)
    w.notion.reset_writes()
    result = onboard.run(w.context())
    assert "emoji title prefixes removed: 0" in result.details
    assert _titles(w, S.DB_TASKS, S.P_Tasks.TITLE) == before
    assert all(ds != w.notion.ids[S.DB_TASKS] for ds, _ in w.notion.updates), "a rerun renames nothing"


def test_cleanup_respects_the_per_run_cap() -> None:
    w = build_world()
    _seed_legacy(w)
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("TITLE_FIX_PER_RUN"), S.P_Config.NUMBER: nt.p_number(2)})
    first = onboard.run(w.context())
    assert "emoji title prefixes removed: 2" in first.details
    second = onboard.run(w.context())
    assert "emoji title prefixes removed: 2" in second.details
    third = onboard.run(w.context())
    assert "emoji title prefixes removed: 1" in third.details
    titles = _titles(w, S.DB_TASKS, S.P_Tasks.TITLE) + _titles(w, S.DB_WEEKLY_REPORTS, S.P_WeeklyReports.TITLE)
    # Only the bare "🏆 " (nothing after the prefix, left alone on purpose) may still start with a prefix.
    assert not [t for t in titles if t.startswith(LEGACY_PREFIXES) and t.strip() not in {p.strip() for p in LEGACY_PREFIXES}]


def test_cleanup_off_when_the_cap_is_zero() -> None:
    w = build_world()
    _seed_legacy(w)
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("TITLE_FIX_PER_RUN"), S.P_Config.NUMBER: nt.p_number(0)})
    result = onboard.run(w.context())
    assert "emoji title prefixes removed: 0" in result.details
    assert "🏆 Codeforces Round 1" in _titles(w, S.DB_TASKS, S.P_Tasks.TITLE)


def test_dry_run_reports_nothing_is_written() -> None:
    w = build_world()
    _seed_legacy(w)
    w.notion.reset_writes()
    onboard.run(w.context(dry_run=True))
    assert "🏆 Codeforces Round 1" in _titles(w, S.DB_TASKS, S.P_Tasks.TITLE)
