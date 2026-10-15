"""Cairo day/week helpers.

Internal datetimes are timezone-aware UTC. Conversion to Africa/Cairo happens
only for "which day / which week" decisions. A day is the Cairo calendar day
(00:00-23:59 Cairo). A week starts on Saturday.

Pure functions only: ``now_utc`` reads the clock, nothing else does I/O.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CAIRO_TZ = ZoneInfo("Africa/Cairo")

# date.weekday(): Monday=0 ... Saturday=5 ... Sunday=6
_SATURDAY = 5


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def cairo_date(dt_utc: datetime) -> date:
    """Return the Africa/Cairo calendar date of an aware datetime.

    Raises ValueError for naive datetimes, because guessing their zone would
    silently shift days.
    """
    if dt_utc.tzinfo is None or dt_utc.utcoffset() is None:
        raise ValueError("cairo_date requires a timezone-aware datetime")
    return dt_utc.astimezone(CAIRO_TZ).date()


def week_start(d: date) -> date:
    """Return the Saturday on or before ``d`` (the start of its week)."""
    days_back = (d.weekday() - _SATURDAY) % 7
    return d - timedelta(days=days_back)


def week_label(d: date) -> str:
    """Return the label ``"YYYY-MM-DD (Sat)"`` of the week containing ``d``.

    The label always names the Saturday that starts that week, so the "(Sat)"
    suffix is always accurate, even when ``d`` is mid-week.
    """
    start = week_start(d)
    return f"{start.isoformat()} (Sat)"
