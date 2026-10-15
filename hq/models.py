"""Plain data models shared by the source clients, the Notion layer and jobs.

All datetimes are timezone-aware UTC. Cairo-day logic lives in ``hq.timeutil``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Contest:
    """An upcoming or running contest on one of the supported platforms."""

    platform: str  # "Codeforces" | "AtCoder" | "LeetCode"
    external_id: str  # e.g. "CF-2050", "AT-abc380", "LC-weekly-contest-420"
    name: str
    start_utc: datetime
    duration_min: int
    link: str
    rated: bool = True


@dataclass(frozen=True)
class Solve:
    """A first accepted solution of one problem by one handle."""

    judge: str  # "Codeforces" | "CF Gym" | "AtCoder" | "LeetCode"
    problem_key: str  # "CF-1985C", "GYM-104114A", "AT-abc300_d", "LC-two-sum"
    name: str
    link: str
    rating: int | None  # CF rating, AtCoder difficulty (+offset, clipped >=0), None if unknown
    difficulty: str | None  # "Easy" | "Medium" | "Hard" (LeetCode) else None
    tags: tuple[str, ...]  # CF-tag vocabulary (lowercase); LeetCode tags mapped best-effort
    solved_at_utc: datetime  # first AC time
    mode: str  # "Live Contest" | "Virtual" | "Practice"
    contest_external_id: str | None
    attempts: int  # submissions on this problem up to and incl. first AC


@dataclass(frozen=True)
class RatingChange:
    """One rating update from a platform's rating history."""

    platform: str
    contest_name: str
    date_utc: datetime
    old: int
    new: int
    rank: int | None
