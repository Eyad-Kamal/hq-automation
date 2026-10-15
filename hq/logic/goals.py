"""Goal progress: map a Goals "Metric" to its current value from member_stats.

member_stats is the dict built by the orchestration layer. Its keys are listed
in build/automation-contract.md. A missing or None numeric key counts as 0.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_METRIC_KEYS: dict[str, str] = {
    "Problems Solved": "problems_total",
    "Points": "points_total",
    "CF Rating": "cf_rating",
    "Level Reached": "levels_complete",
    "Topics ≥ Competent": "topics_competent",
    "Streak Days": "current_streak",
    "Contests Attended": "contests_total",
}

PROBLEMS_AT_RATING = "Problems ≥ Rating"


def _as_number(value: Any) -> float:
    return 0.0 if value is None else float(value)


def current_value(
    metric: str,
    member_stats: Mapping[str, Any],
    threshold_rating: int | None = None,
) -> float:
    """Return the current value of a goal metric.

    ``"Problems ≥ Rating"`` counts entries in ``member_stats["solved_ratings"]``
    (a list of ints) that are >= ``threshold_rating``, so it requires
    ``threshold_rating``. Unknown metrics raise ValueError.
    """
    if metric == PROBLEMS_AT_RATING:
        if threshold_rating is None:
            raise ValueError("'Problems ≥ Rating' requires threshold_rating")
        ratings = member_stats.get("solved_ratings") or []
        return float(sum(1 for r in ratings if r is not None and r >= threshold_rating))

    key = _METRIC_KEYS.get(metric)
    if key is None:
        raise ValueError(f"unknown goal metric: {metric!r}")
    return _as_number(member_stats.get(key))
