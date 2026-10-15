"""Pick practice problems for an auto-generated Task.

Candidates are problems that:

* are not in ``solved_keys``,
* have a rating inside [band_low, band_high] (problems with no rating are
  skipped),
* share at least one tag (case-insensitive) with ``tags``.

Candidates are sorted by ``key`` first, then shuffled with
``random.Random(seed)``, so the same inputs always give the same picks. If
fewer than ``n`` candidates remain, the band is widened by 100 rating points
on each side, up to 3 times. The result is the first ``n`` shuffled candidates,
sorted by rating ascending (ties by key).
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping
from typing import Any

WIDEN_STEP = 100
MAX_WIDENINGS = 3


def _norm_tags(tags: Iterable[str] | None) -> set[str]:
    return {t.strip().lower() for t in (tags or []) if t and t.strip()}


def pick_problems(
    problemset: Iterable[Mapping[str, Any]],
    solved_keys: Iterable[str],
    tags: Iterable[str],
    band_low: int,
    band_high: int,
    n: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Return up to ``n`` problems from ``problemset`` for a practice task.

    Returns fewer than ``n`` items only if there are not enough candidates
    even after widening. Returns [] when ``n`` <= 0.
    """
    if n <= 0:
        return []

    solved = set(solved_keys)
    wanted_tags = _norm_tags(tags)
    pool = [dict(p) for p in problemset]

    def candidates_for(low: int, high: int) -> list[dict[str, Any]]:
        found = []
        for p in pool:
            if p.get("key") in solved:
                continue
            rating = p.get("rating")
            if rating is None or not (low <= rating <= high):
                continue
            if not (_norm_tags(p.get("tags")) & wanted_tags):
                continue
            found.append(p)
        found.sort(key=lambda p: str(p.get("key")))
        return found

    widenings = 0
    candidates = candidates_for(band_low, band_high)
    while len(candidates) < n and widenings < MAX_WIDENINGS:
        widenings += 1
        widen = WIDEN_STEP * widenings
        candidates = candidates_for(band_low - widen, band_high + widen)

    rng = random.Random(seed)
    rng.shuffle(candidates)
    picked = candidates[:n]
    picked.sort(key=lambda p: (p["rating"], str(p.get("key"))))
    return picked
