"""Goal progress: map a Goals "Metric" to its current value from member_stats.

member_stats is the dict built by the orchestration layer. Its keys are listed
in build/automation-contract.md. A missing or None numeric key counts as 0.

Topic and level metrics also need the goal's links (Goals "Linked Topics" and
"Linked Level"), which the caller passes in. A goal that uses one of these
metrics without the link it needs raises ValueError, so the caller skips it
with a warning.

Topic tiers are ordinals (see ``TIER_ORDER``): Unseen 0 ... Master 6.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from hq import schema as S

_METRIC_KEYS: dict[str, str] = {
    "Problems Solved": "problems_total",
    "Points": "points_total",
    "CF Rating": "cf_rating",
    "Level Reached": "levels_complete",
    "Topics ≥ Competent": "topics_competent",
    "Streak Days": "current_streak",
    "Contests Attended": "contests_total",
}

PROBLEMS_AT_RATING = S.GoalMetric.PROBLEMS_AT_RATING
TOPIC_PROBLEMS = S.GoalMetric.TOPIC_PROBLEMS
TOPIC_TIER = S.GoalMetric.TOPIC_TIER
LEVEL_TOPICS_COMPETENT = S.GoalMetric.LEVEL_TOPICS_COMPETENT

# Metrics that need the goal's Linked Topics (TOPIC_PROBLEMS, TOPIC_TIER) or Linked Level (LEVEL_TOPICS_COMPETENT).
TOPIC_METRICS: frozenset[str] = frozenset({TOPIC_PROBLEMS, TOPIC_TIER, LEVEL_TOPICS_COMPETENT})

# Effective mastery tiers, lowest first. The ordinal is the value of "Topic Tier".
TIER_ORDER: tuple[str, ...] = ("Unseen", "Theory", "Novice", "Apprentice", "Competent", "Proficient", "Master")
TIER_RANK: dict[str, int] = {name: rank for rank, name in enumerate(TIER_ORDER)}
COMPETENT_RANK: int = TIER_RANK["Competent"]


def tier_rank(tier: str | None) -> int:
    """Ordinal of a mastery tier (0 = Unseen; a missing or unknown tier counts as Unseen)."""
    return TIER_RANK.get(tier or "", 0)


def _as_number(value: Any) -> float:
    return 0.0 if value is None else float(value)


def _need(metric: str, what: str, value: list[str] | None) -> list[str]:
    """The linked ids, or ValueError when the goal has no link (None, or an empty list for topics)."""
    if value is None or not value:
        raise ValueError(f"{metric!r} requires {what}")
    return value


def current_value(
    metric: str,
    member_stats: Mapping[str, Any],
    threshold_rating: int | None = None,
    *,
    topic_ids: Iterable[str] | None = None,
    level_topic_ids: Iterable[str] | None = None,
) -> float:
    """Return the current value of a goal metric.

    ``"Problems ≥ Rating"`` counts entries in ``member_stats["solved_ratings"]``
    (a list of ints) that are >= ``threshold_rating``, so it requires
    ``threshold_rating``. Unknown metrics raise ValueError.

    ``"Topic Problems"`` counts the solved problems (``member_stats["solved_topics"]``, one
    list of topic ids per solved problem) that have any of ``topic_ids`` (the goal's Linked
    Topics). ``"Topic Tier"`` is the best tier rank among ``topic_ids`` (``member_stats["topic_tiers"]``,
    topic id -> effective tier). Both require ``topic_ids``.

    ``"Level Topics ≥ Competent"`` counts the topics of ``level_topic_ids`` (the topics of the
    goal's Linked Level) whose effective tier is Competent or above. It requires ``level_topic_ids``.
    """
    if metric in TOPIC_METRICS:
        return _topic_value(metric, member_stats, topic_ids, level_topic_ids)

    if metric == PROBLEMS_AT_RATING:
        if threshold_rating is None:
            raise ValueError("'Problems ≥ Rating' requires threshold_rating")
        ratings = member_stats.get("solved_ratings") or []
        return float(sum(1 for r in ratings if r is not None and r >= threshold_rating))

    key = _METRIC_KEYS.get(metric)
    if key is None:
        raise ValueError(f"unknown goal metric: {metric!r}")
    return _as_number(member_stats.get(key))


def _topic_value(
    metric: str,
    member_stats: Mapping[str, Any],
    topic_ids: Iterable[str] | None,
    level_topic_ids: Iterable[str] | None,
) -> float:
    tiers: Mapping[str, str] = member_stats.get("topic_tiers") or {}
    if metric == LEVEL_TOPICS_COMPETENT:
        if level_topic_ids is None:
            raise ValueError(f"{metric!r} requires a Linked Level")
        # A linked level with no topics yet counts 0 (not a skip).
        return float(sum(1 for topic in set(level_topic_ids) if tier_rank(tiers.get(topic)) >= COMPETENT_RANK))

    wanted = set(_need(metric, "Linked Topics", list(topic_ids) if topic_ids is not None else None))
    if metric == TOPIC_PROBLEMS:
        solved: list[list[str]] = member_stats.get("solved_topics") or []
        return float(sum(1 for topics in solved if wanted.intersection(topics or [])))
    # TOPIC_TIER
    return float(max(tier_rank(tiers.get(topic)) for topic in wanted))
