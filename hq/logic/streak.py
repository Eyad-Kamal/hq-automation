"""Daily streak with freezes.

Rules (walk every calendar day from the earliest active day to ``today``):

* An active day adds +1 to ``current``. Each time ``current`` reaches a
  multiple of ``freeze_every`` (7, 14, ...) the member earns 1 freeze, up to
  ``freeze_max``.
* A missed day before ``today`` consumes 1 freeze if one is available. That
  day goes into ``frozen_days``, and the streak survives without increasing.
* A missed day with no freeze breaks the streak: ``current`` resets to 0 and
  the freezes reset to 0.
* ``today`` not being active yet does NOT break anything (the day is still in
  progress).
* ``best`` is the highest value ``current`` ever reached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class StreakResult:
    """Result of ``compute``.

    current: active days in the current streak (frozen days do not count).
    best: maximum ``current`` ever reached.
    freezes: freezes still available.
    frozen_days: missed days that a freeze covered.
    """

    current: int
    best: int
    freezes: int
    frozen_days: set[date] = field(default_factory=set)


def compute(
    active_days: set[date],
    today: date,
    freeze_every: int = 7,
    freeze_max: int = 2,
) -> StreakResult:
    """Compute the streak state as of ``today``.

    Days after ``today`` are ignored. An empty set returns all zeros.
    """
    if freeze_every < 1:
        raise ValueError("freeze_every must be >= 1")

    days = [d for d in active_days if d <= today]
    if not days:
        return StreakResult(current=0, best=0, freezes=0, frozen_days=set())

    active = set(days)
    start = min(days)

    current = 0
    best = 0
    freezes = 0
    frozen: set[date] = set()

    day = start
    while day <= today:
        if day in active:
            current += 1
            best = max(best, current)
            if current % freeze_every == 0 and freezes < freeze_max:
                freezes += 1
        elif day < today and current > 0:
            # A missed past day while a streak is alive.
            if freezes > 0:
                freezes -= 1
                frozen.add(day)
            else:
                current = 0
                freezes = 0
        # Missed `today` is in progress: no effect.
        day += timedelta(days=1)

    return StreakResult(current=current, best=best, freezes=freezes, frozen_days=frozen)
