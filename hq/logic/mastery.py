"""Per-topic mastery: weighted points and tier.

Each solve contributes ``band_factor * mode_factor``:

* band factor: below band (rating < band_low) -> BAND_BELOW_FACTOR (0.25),
  in band -> BAND_IN_FACTOR (1), above band (rating > band_high) ->
  BAND_ABOVE_FACTOR (1.5).
* mode factor: "Upsolve" -> MODE_UPSOLVE (0.7), "With Editorial/Hint" ->
  MODE_EDITORIAL (0.5), anything else -> 1.

Tier (checked in this order):

* Master: points >= TIER_MASTER and above >= TIER_MASTER_ABOVE
* Proficient: points >= TIER_PROFICIENT and above >= TIER_PROFICIENT_ABOVE
* Competent: points >= TIER_COMPETENT
* Apprentice: points >= TIER_APPRENTICE
* Novice: points >= TIER_NOVICE
* Theory: theory_done
* Unseen: otherwise

``cfg`` is the Config table as a dict keyed by Config key. Missing or None
keys fall back to the defaults in build/schema-spec.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

DEFAULT_CFG: dict[str, float] = {
    "TIER_NOVICE": 1,
    "TIER_APPRENTICE": 5,
    "TIER_COMPETENT": 12,
    "TIER_PROFICIENT": 25,
    "TIER_PROFICIENT_ABOVE": 3,
    "TIER_MASTER": 40,
    "TIER_MASTER_ABOVE": 8,
    "BAND_BELOW_FACTOR": 0.25,
    "BAND_IN_FACTOR": 1,
    "BAND_ABOVE_FACTOR": 1.5,
    "MODE_UPSOLVE": 0.7,
    "MODE_EDITORIAL": 0.5,
}

# Accumulated float sums are rounded to this many decimals before tier
# comparisons, so boundaries such as exactly 5.0 are not missed by float noise.
_POINT_DECIMALS = 6


@dataclass(frozen=True)
class MasteryResult:
    """Result of ``compute``.

    count: number of solves.
    below / in_band / above: solve counts per band position.
    max_rating: highest solved rating (0 if no solves).
    points: weighted mastery points, rounded to 6 decimals.
    tier: "Master" | "Proficient" | "Competent" | "Apprentice" | "Novice" |
          "Theory" | "Unseen".
    """

    count: int
    below: int
    in_band: int
    above: int
    max_rating: int
    points: float
    tier: str


def _cfg_value(cfg: Mapping[str, float] | None, key: str) -> float:
    """Return ``cfg[key]``, or the spec default if the key or value is missing."""
    if cfg is not None:
        value = cfg.get(key)
        if value is not None:
            return float(value)
    return float(DEFAULT_CFG[key])


def mode_factor(mode: str | None, cfg: Mapping[str, float] | None = None) -> float:
    """Return the multiplier for a solve mode."""
    if mode == "Upsolve":
        return _cfg_value(cfg, "MODE_UPSOLVE")
    if mode == "With Editorial/Hint":
        return _cfg_value(cfg, "MODE_EDITORIAL")
    return 1.0


def tier_for(
    points: float,
    above: int,
    theory_done: bool,
    cfg: Mapping[str, float] | None = None,
) -> str:
    """Return the mastery tier name for the given points and above-band count."""
    if points >= _cfg_value(cfg, "TIER_MASTER") and above >= _cfg_value(cfg, "TIER_MASTER_ABOVE"):
        return "Master"
    if points >= _cfg_value(cfg, "TIER_PROFICIENT") and above >= _cfg_value(cfg, "TIER_PROFICIENT_ABOVE"):
        return "Proficient"
    if points >= _cfg_value(cfg, "TIER_COMPETENT"):
        return "Competent"
    if points >= _cfg_value(cfg, "TIER_APPRENTICE"):
        return "Apprentice"
    if points >= _cfg_value(cfg, "TIER_NOVICE"):
        return "Novice"
    if theory_done:
        return "Theory"
    return "Unseen"


def compute(
    solves: Iterable[tuple[int, str | None]],
    band_low: int,
    band_high: int,
    theory_done: bool,
    cfg: Mapping[str, float] | None = None,
) -> MasteryResult:
    """Compute mastery for one member on one topic.

    ``solves`` is an iterable of ``(rating, mode)`` pairs. ``rating`` should be
    the effective rating (see ``scoring.effective_rating``).
    """
    below_factor = _cfg_value(cfg, "BAND_BELOW_FACTOR")
    in_factor = _cfg_value(cfg, "BAND_IN_FACTOR")
    above_factor = _cfg_value(cfg, "BAND_ABOVE_FACTOR")

    count = 0
    below = 0
    in_band = 0
    above = 0
    max_rating = 0
    total = 0.0

    for rating, mode in solves:
        count += 1
        if rating < band_low:
            band_factor = below_factor
            below += 1
        elif rating > band_high:
            band_factor = above_factor
            above += 1
        else:
            band_factor = in_factor
            in_band += 1
        total += band_factor * mode_factor(mode, cfg)
        if rating > max_rating:
            max_rating = rating

    points = round(total, _POINT_DECIMALS)
    tier = tier_for(points, above, theory_done, cfg)
    return MasteryResult(
        count=count,
        below=below,
        in_band=in_band,
        above=above,
        max_rating=max_rating,
        points=points,
        tier=tier,
    )
