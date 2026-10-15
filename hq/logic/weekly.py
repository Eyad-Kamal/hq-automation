"""Weekly summary from daily rows.

``daily_rows`` items are dicts with keys ``date``, ``problems``, ``points``,
``contests``, ``max_rating`` and ``active``. ``prev_points`` is the previous
week's points total.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


def summarize(daily_rows: Iterable[Mapping[str, Any]], prev_points: float) -> dict[str, Any]:
    """Summarize one week's daily rows.

    Returns a dict with keys: points, problems, active_days, avg_rating,
    max_rating, contests, points_delta.

    * points: sum of daily points, rounded to 2 decimals.
    * active_days: number of rows with ``active`` truthy.
    * avg_rating: mean ``max_rating`` over rows with problems > 0 (and a
      non-None max_rating), rounded half up. 0 if there are none.
    * max_rating: highest ``max_rating`` in any row (0 if none).
    * points_delta: points minus ``prev_points``, rounded to 2 decimals.
    """
    rows = list(daily_rows)

    points = sum(float(r.get("points") or 0) for r in rows)
    problems = sum(int(r.get("problems") or 0) for r in rows)
    active_days = sum(1 for r in rows if r.get("active"))
    contests = sum(int(r.get("contests") or 0) for r in rows)

    all_ratings = [int(r["max_rating"]) for r in rows if r.get("max_rating") is not None]
    max_rating = max(all_ratings) if all_ratings else 0

    solved_ratings = [
        int(r["max_rating"])
        for r in rows
        if int(r.get("problems") or 0) > 0 and r.get("max_rating") is not None
    ]
    if solved_ratings:
        avg_rating = math.floor(sum(solved_ratings) / len(solved_ratings) + 0.5)
    else:
        avg_rating = 0

    points_total = round(points, 2)
    return {
        "points": points_total,
        "problems": problems,
        "active_days": active_days,
        "avg_rating": int(avg_rating),
        "max_rating": max_rating,
        "contests": contests,
        "points_delta": round(points_total - float(prev_points), 2),
    }
