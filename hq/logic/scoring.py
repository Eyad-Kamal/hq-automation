"""Problem scoring. Must match the Notion "Points" formula in build/schema-spec.md.

    w = (r / 800) ** 2.2
    s = clamp(2 ** ((r - ref) / 400), 0.5, 2)
    m = 0.7 for "Upsolve", 0.5 for "With Editorial/Hint", else 1
    points = round(w * s * m, 2)

The Notion formula rounds with ``round(x * 100) / 100``. ``round_half_up``
reproduces that exactly for non-negative values, unlike Python's banker's
rounding.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

DEFAULT_LC_MAP: Mapping[str, int] = {"Easy": 1000, "Medium": 1400, "Hard": 1900}
DEFAULT_RATING = 800

MODE_UPSOLVE = "Upsolve"
MODE_EDITORIAL = "With Editorial/Hint"


def effective_rating(
    rating: int | None,
    difficulty: str | None,
    lc_map: Mapping[str, int] = DEFAULT_LC_MAP,
    default: int = DEFAULT_RATING,
) -> int:
    """Return the rating used for scoring.

    Uses ``rating`` when it is set. Otherwise maps a LeetCode-style difficulty
    through ``lc_map``. Otherwise returns ``default``.
    """
    if rating is not None:
        return int(rating)
    if difficulty is not None and difficulty in lc_map:
        return int(lc_map[difficulty])
    return int(default)


def clamp(x: float, lo: float, hi: float) -> float:
    """Clamp ``x`` into the closed interval ``[lo, hi]``."""
    return min(max(x, lo), hi)


def round_half_up(x: float, ndigits: int = 2) -> float:
    """Round half up at ``ndigits`` decimals, matching Notion's ``round(x*100)/100``.

    Written as ``floor(x * 10**n + 0.5) / 10**n``, which for non-negative
    values matches the Notion formula. Python's ``round`` rounds exact halves
    to even, which can differ.
    """
    factor = 10 ** ndigits
    return math.floor(x * factor + 0.5) / factor


def points(
    rating: int,
    ref_rating: int,
    mode: str | None,
    *,
    upsolve: float = 0.7,
    editorial: float = 0.5,
) -> float:
    """Return the points for one solved problem, rounded to 2 decimals.

    ``rating`` is the problem's effective rating. ``ref_rating`` is the
    member's reference rating (or ``Rating @ Solve``). ``mode`` picks the
    multiplier. The Notion formula's ``round(w*s*m*100)/100`` is reproduced by
    ``round_half_up`` with the same multiplication order.
    """
    w = (rating / 800) ** 2.2
    s = clamp(2 ** ((rating - ref_rating) / 400), 0.5, 2.0)
    if mode == MODE_UPSOLVE:
        m = upsolve
    elif mode == MODE_EDITORIAL:
        m = editorial
    else:
        m = 1.0
    return round_half_up(w * s * m, 2)
