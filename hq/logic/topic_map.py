"""Map a Codeforces problem's tags and rating to Topics.

For each tag (in the order given) the candidate topics are those whose
``cf_tags`` contain that tag. The chosen topic is the candidate whose rating
band contains ``rating``. If none contains it, the candidate with the nearest
band wins. Ties break on lower ``level_no``, then lower ``order``. Duplicate
topic ids are dropped, keeping first-seen order.

Tag and cf_tags comparison is case-insensitive and ignores surrounding spaces.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


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
