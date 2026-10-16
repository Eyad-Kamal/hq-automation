"""Season rules: which rows count in the current season.

A season start deletes nothing. Rows before it stay in the all-time views. The
"In Season" checkboxes and the Members "Season Points" / "Season Solved" numbers
are what the season views filter on.

* Global start: Config SEASON_START (a date, or None = no season, everything is in season).
* Member start: the member's own "Season Start" (a date, or None).
* Effective start of a member: the later of the two (either may be None).

Pure functions only. Reading the Config and Members rows happens in ``hq.jobs.recompute``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


def effective_start(global_start: date | None, member_start: date | None) -> date | None:
    """The later of the global and the member start. None when both are empty."""
    if global_start is None:
        return member_start
    if member_start is None:
        return global_start
    return max(global_start, member_start)


def in_season(day: date | None, start: date | None) -> bool:
    """True when ``day`` is on or after ``start``. With no start everything is in season;
    with a start, a row that has no date is not in season."""
    if start is None:
        return True
    return day is not None and day >= start


@dataclass(frozen=True)
class Season:
    """The global start plus each member's own Season Start (member id -> date or None)."""

    start: date | None
    own_starts: dict[str, date | None] = field(default_factory=dict)

    def start_for(self, member_id: str | None) -> date | None:
        """Effective start for a row linked to ``member_id``. A row with no member (or a member
        not known to this run) uses the global start."""
        if member_id is None or member_id not in self.own_starts:
            return self.start
        return effective_start(self.start, self.own_starts[member_id])
