"""Map a Codeforces problem's tags and rating to Topics.

For each tag (in the order given) the candidate topics are those whose
``cf_tags`` contain that tag. The chosen topic is the candidate whose rating
band contains ``rating``. If none contains it, the candidate with the nearest
band wins. Ties break on lower ``level_no``, then lower ``order``. Duplicate
topic ids are dropped, keeping first-seen order.

Tag and cf_tags comparison is case-insensitive and ignores surrounding spaces.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

# Tiers that count as "Competent or better" for the sequential router.
COMPETENT_OR_MORE = frozenset({"Competent", "Proficient", "Master"})


@dataclass(frozen=True)
class TopicInfo:
    """A Topic row as needed for mapping.

    id: Notion page id (or stable key) of the topic.
    name: display name.
    cf_tags: Codeforces tags that route to this topic.
    band_low / band_high: rating band of the topic.
    level_no: level number (1..4) the topic belongs to.
    order: sort order inside the roadmap.
    """

    id: str
    name: str
    cf_tags: tuple[str, ...]
    band_low: int
    band_high: int
    level_no: int
    order: int


def _norm(tag: str) -> str:
    return tag.strip().lower()


def _band_distance(rating: int, band_low: int, band_high: int) -> int:
    """Return 0 if rating is inside the band, else the distance to its nearest edge."""
    if rating < band_low:
        return band_low - rating
    if rating > band_high:
        return rating - band_high
    return 0


def map_topics(
    tags: Iterable[str],
    rating: int,
    topics: Sequence[TopicInfo],
) -> list[str]:
    """Return the unique topic ids a problem maps to, in first-seen order."""
    result: list[str] = []
    seen: set[str] = set()

    for raw_tag in tags:
        tag = _norm(raw_tag)
        candidates = [t for t in topics if tag in {_norm(x) for x in t.cf_tags}]
        if not candidates:
            continue
        best = min(
            candidates,
            key=lambda t: (
                _band_distance(rating, t.band_low, t.band_high),
                t.level_no,
                t.order,
            ),
        )
        if best.id not in seen:
            seen.add(best.id)
            result.append(best.id)

    return result


@dataclass(frozen=True)
class RouteProblem:
    """One solved problem for ``route_topics_sequential``.

    row_id: Problems row id (the key of the result).
    tags: CF tags of the problem.
    rating: effective rating (see ``scoring.effective_rating``).
    mode: solve mode (Mastery factors are looked up from it).
    fixed: when not None, the problem is user-owned: these topic ids are kept as they are
        (no routing) but still count toward running mastery.
    """

    row_id: str
    tags: tuple[str, ...]
    rating: int
    mode: str | None
    fixed: tuple[str, ...] | None = None


def _tie_group(tag: str, rating: int, topics: Sequence[TopicInfo]) -> list[TopicInfo]:
    """Candidates for one normalized tag that share the minimal (band distance, level_no),
    ordered by ``order``. ``map_topics`` picks the first of them."""
    candidates = [t for t in topics if tag in {_norm(x) for x in t.cf_tags}]
    if not candidates:
        return []
    best = min((_band_distance(rating, t.band_low, t.band_high), t.level_no) for t in candidates)
    group = [t for t in candidates if (_band_distance(rating, t.band_low, t.band_high), t.level_no) == best]
    return sorted(group, key=lambda t: t.order)


def route_topics_sequential(
    problems: Sequence[RouteProblem],
    topics: Sequence[TopicInfo],
    tier_threshold: float,
    factor_fn: Callable[[int, int, int, str | None], float],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    """Route each problem to topics, filling same-band topics in roadmap order.

    ``problems`` must already be in chronological order. For each tag, the tie group is
    the candidates with the minimal (band distance, level_no). A group of one routes as
    ``map_topics`` does. A larger group routes the problem to its first topic (by
    ``order``) that the member has not reached Competent in yet, else to its last topic.

    A topic counts as reached when its running mastery points (sum of
    ``factor_fn(rating, band_low, band_high, mode)`` over the problems already assigned to it,
    rounded to 6 decimals) are >= ``tier_threshold``, or when ``overrides`` gives it a
    Competent/Proficient/Master coach tier.

    Returns {row_id: [topic ids]} with unique ids in first-seen order.
    """
    competent_override = {tid for tid, tier in (overrides or {}).items() if tier in COMPETENT_OR_MORE}
    by_id = {t.id: t for t in topics}
    points: dict[str, float] = {}
    result: dict[str, list[str]] = {}

    for problem in problems:
        if problem.fixed is not None:
            chosen = list(dict.fromkeys(problem.fixed))
        else:
            chosen = []
            for raw_tag in problem.tags:
                group = _tie_group(_norm(raw_tag), problem.rating, topics)
                if not group:
                    continue
                pick = group[-1]
                if len(group) > 1:
                    for topic in group:
                        reached = topic.id in competent_override or points.get(topic.id, 0.0) >= tier_threshold
                        if not reached:
                            pick = topic
                            break
                if pick.id not in chosen:
                    chosen.append(pick.id)
        result[problem.row_id] = chosen

        for topic_id in chosen:
            topic = by_id.get(topic_id)
            if topic is None:
                continue
            gained = factor_fn(problem.rating, topic.band_low, topic.band_high, problem.mode)
            points[topic_id] = round(points.get(topic_id, 0.0) + gained, 6)

    return result
