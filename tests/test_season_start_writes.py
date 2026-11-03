"""Jobs do not create or rewrite Daily Log or Weekly Summary rows before the season start.

The rule lives in ``Season.blocks`` and is used by recompute (Daily Log window), the daily job
(``ensure_daily_log``) and the weekly summary writer (every path, including the backfill).
With no SEASON_START nothing is blocked.
"""

from __future__ import annotations

from datetime import date, timedelta

from hq import notion as nt
from hq import schema as S
from hq.jobs import imports, onboard, recompute, weekly
from hq.jobs.weekly import backfill_run
from hq.logic.season import Season
from hq.repo import cairo_day_of, get_date, get_relation, get_text
from tests.fakes import NOW, NOW_SATURDAY, build_world, seed_eyad_solves

SEASON = date(2026, 10, 14)  # NOW (15 Oct) is in season; 1 Oct is not


def _config(w, *, when=None, key: str = "SEASON_START", number: int | None = None) -> None:
    props = {S.P_Config.TITLE: nt.p_title(key)}
    if when is not None:
        props[S.P_Config.DATE] = nt.p_date(when, with_time=False)
    if number is not None:
        props[S.P_Config.NUMBER] = nt.p_number(number)
    w.notion.seed(S.DB_CONFIG, props)


def _world(start: date | None):
    w = build_world()
    seed_eyad_solves(w)
    if start is not None:
        _config(w, when=start)
    return w


def _chain(w, now=NOW):
    onboard.run(w.context(now=now))
    imports.run(w.context(now=now))
    return recompute.run(w.context(now=now))


def _daily_dates(w, member_id: str) -> list[date]:
    out = []
    for row in w.notion.rows(S.DB_DAILY_LOG):
        if member_id in get_relation(row, S.P_DailyLog.MEMBER):
            out.append(cairo_day_of(get_date(row, S.P_DailyLog.DATE)))
    return sorted(d for d in out if d is not None)


def _summary_weeks(w, scope: str) -> list[date]:
    out = []
    for row in w.notion.rows(S.DB_WEEKLY_SUMMARY):
        if get_text(row, S.P_WeeklySummary.SOURCE_KEY).split(":")[0] == scope:
            out.append(cairo_day_of(get_date(row, S.P_WeeklySummary.WEEK_START)))
    return sorted({d for d in out if d is not None})


def test_season_blocks_only_dates_before_a_set_start() -> None:
    assert Season(start=None).blocks(date(2000, 1, 1)) is False
    assert Season(start=SEASON).blocks(date(2026, 10, 13)) is True
    assert Season(start=SEASON).blocks(SEASON) is False
    assert Season(start=SEASON).blocks(date(2026, 10, 15)) is False
    assert Season(start=SEASON).blocks(None) is False


def test_recompute_creates_no_pre_season_daily_log_row_and_keeps_on_or_after() -> None:
    w = _world(SEASON)
    _chain(w)
    dates = _daily_dates(w, w.eyad)
    assert dates, "rows on or after the season start are written"
    assert all(d >= SEASON for d in dates), [d for d in dates if d < SEASON]
    assert SEASON in dates and NOW.date() in dates


def test_recompute_with_no_season_writes_the_full_window_as_before() -> None:
    w = _world(None)
    _chain(w)
    dates = _daily_dates(w, w.eyad)
    assert dates[0] == date(2026, 10, 15) - timedelta(days=60)
    assert len(dates) == 61  # DAILY_LOG_DAYS default 60 plus today


def test_ensure_daily_log_writes_nothing_before_the_season_start() -> None:
    w = _world(SEASON)
    ctx = w.context(now=NOW)
    eyad = next(m for m in ctx.repo.members() if m.id == w.eyad)
    assert recompute.ensure_daily_log(ctx, eyad, date(2026, 10, 13)) is None
    assert _daily_dates(w, w.eyad) == [], "no Daily Log row was created for the blocked day"
    assert recompute.ensure_daily_log(ctx, eyad, SEASON) is not None


def test_weekly_run_writes_no_pre_season_summary_and_backfill_skips_older_weeks() -> None:
    w = _world(SEASON)
    _config(w, number=4, key="WEEKLY_BACKFILL_WEEKS")
    _chain(w, now=NOW_SATURDAY)
    weekly.run(w.context(now=NOW_SATURDAY))  # summarises the week of 10 Oct, which is before the start
    assert _summary_weeks(w, "person") == []
    assert _summary_weeks(w, "team") == []
    assert _summary_weeks(w, "club") == []


def test_backfill_writes_on_or_after_the_start_only() -> None:
    w = _world(date(2026, 10, 3))
    _config(w, number=4, key="WEEKLY_BACKFILL_WEEKS")
    backfill_run(w.context(now=NOW_SATURDAY))  # weeks of 10 Oct, 3 Oct, 26 Sep, 19 Sep
    weeks = _summary_weeks(w, "club")
    assert weeks == [date(2026, 10, 3), date(2026, 10, 10)]
    assert _summary_weeks(w, "person") == [date(2026, 10, 3), date(2026, 10, 10)]


def test_backfill_with_no_season_writes_every_week() -> None:
    w = _world(None)
    _config(w, number=4, key="WEEKLY_BACKFILL_WEEKS")
    backfill_run(w.context(now=NOW_SATURDAY))
    assert _summary_weeks(w, "club") == [date(2026, 9, 19), date(2026, 9, 26), date(2026, 10, 3), date(2026, 10, 10)]
