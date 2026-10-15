from datetime import date, datetime, timedelta, timezone

import pytest

from hq import timeutil


UTC = timezone.utc


def test_now_utc_is_aware_utc():
    now = timeutil.now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_cairo_date_friday_2359_cairo_is_friday():
    # Fri 2026-01-16 23:59 Cairo (UTC+2 in January) = 21:59 UTC
    dt = datetime(2026, 1, 16, 21, 59, tzinfo=UTC)
    assert timeutil.cairo_date(dt) == date(2026, 1, 16)


def test_cairo_date_saturday_0000_cairo_is_saturday():
    # Sat 2026-01-17 00:00 Cairo = 22:00 UTC on Jan 16
    dt = datetime(2026, 1, 16, 22, 0, tzinfo=UTC)
    assert timeutil.cairo_date(dt) == date(2026, 1, 17)


def test_cairo_date_differs_from_utc_date():
    # 22:30 UTC on Fri Jan 16 is already Sat Jan 17 00:30 in Cairo.
    dt = datetime(2026, 1, 16, 22, 30, tzinfo=UTC)
    assert dt.date() == date(2026, 1, 16)
    assert timeutil.cairo_date(dt) == date(2026, 1, 17)


def test_cairo_date_late_utc_evening_rolls_to_next_cairo_day():
    # 23:59 UTC Jan 16 = 01:59 Sat Jan 17 in Cairo
    dt = datetime(2026, 1, 16, 23, 59, tzinfo=UTC)
    assert timeutil.cairo_date(dt) == date(2026, 1, 17)


def test_cairo_date_summer_dst_offset_is_plus_three():
    # Cairo observes DST in summer (UTC+3). 21:30 UTC on Jul 1 is 00:30 Jul 2.
    dt = datetime(2026, 7, 1, 21, 30, tzinfo=UTC)
    assert timeutil.cairo_date(dt) == date(2026, 7, 2)


def test_cairo_date_rejects_naive_datetime():
    with pytest.raises(ValueError):
        timeutil.cairo_date(datetime(2026, 1, 16, 21, 59))


def test_week_start_saturday_is_itself():
    assert timeutil.week_start(date(2026, 1, 17)) == date(2026, 1, 17)


def test_week_start_friday_is_previous_saturday():
    # Fri 2026-01-16 belongs to the week that started Sat 2026-01-10
    assert timeutil.week_start(date(2026, 1, 16)) == date(2026, 1, 10)


def test_week_start_sunday_is_same_week_saturday():
    assert timeutil.week_start(date(2026, 1, 18)) == date(2026, 1, 17)


def test_week_start_monday_and_thursday():
    assert timeutil.week_start(date(2026, 1, 19)) == date(2026, 1, 17)  # Mon
    assert timeutil.week_start(date(2026, 1, 22)) == date(2026, 1, 17)  # Thu


def test_week_boundary_friday_vs_saturday_in_cairo():
    # Fri 23:59 Cairo and Sat 00:00 Cairo fall in different weeks.
    fri_2359 = timeutil.cairo_date(datetime(2026, 1, 16, 21, 59, tzinfo=UTC))
    sat_0000 = timeutil.cairo_date(datetime(2026, 1, 16, 22, 0, tzinfo=UTC))
    assert timeutil.week_start(fri_2359) == date(2026, 1, 10)
    assert timeutil.week_start(sat_0000) == date(2026, 1, 17)


def test_week_boundary_uses_cairo_not_utc():
    # 22:30 UTC Fri: UTC date is Fri (same week as Jan 10), but Cairo date is Sat Jan 17.
    dt = datetime(2026, 1, 16, 22, 30, tzinfo=UTC)
    assert timeutil.week_start(timeutil.cairo_date(dt)) == date(2026, 1, 17)
    assert timeutil.week_start(dt.date()) == date(2026, 1, 10)


def test_week_label_format():
    assert timeutil.week_label(date(2026, 1, 17)) == "2026-01-17 (Sat)"


def test_week_label_mid_week_names_the_saturday():
    assert timeutil.week_label(date(2026, 1, 16)) == "2026-01-10 (Sat)"
