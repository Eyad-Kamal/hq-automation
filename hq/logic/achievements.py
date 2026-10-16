"""Badge evaluation.

``evaluate(member_stats, already)`` returns every badge whose rule is met and
which is not in ``already``. Badge names are the exact Achievements."Badge"
option names from build/schema-spec.md. Each rule is documented in
``BADGE_RULES`` (shown in the Guide). A rule is a single threshold on one
member_stats key, so BADGE_RULES is the authoritative description.

"Only once" badges (for example Topic Mastered) are handled by the caller
passing them in ``already`` after they are first awarded.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple


class Rule(NamedTuple):
    """One threshold rule for a badge."""

    stat: str        # member_stats key to test
    threshold: float  # awarded when stat >= threshold
    tier: str        # Bronze | Silver | Gold | Legendary
    text: str        # human-readable rule, used in BADGE_RULES


# Ordered: evaluation (and the returned list) follows this order.
_RULES: dict[str, list[Rule]] = {
    "First Blood": [Rule("problems_total", 1, "Bronze", "Solve 1 problem")],
    "Century (100)": [Rule("problems_total", 100, "Silver", "Solve 100 problems")],
    "250 Club": [Rule("problems_total", 250, "Silver", "Solve 250 problems")],
    "500 Club": [Rule("problems_total", 500, "Gold", "Solve 500 problems")],
    "1000 Club": [Rule("problems_total", 1000, "Legendary", "Solve 1000 problems")],
    "Week Warrior (7🔥)": [
        Rule("best_streak", 7, "Bronze", "Reach a 7-day streak")
    ],
    "Iron Habit (30🔥)": [
        Rule("best_streak", 30, "Gold", "Reach a 30-day streak")
    ],
    "Unbreakable (100🔥)": [
        Rule("best_streak", 100, "Legendary", "Reach a 100-day streak")
    ],
    "Stretch 1200": [
        Rule("max_rating_solved", 1200, "Bronze", "Solve a problem rated 1200 or higher")
    ],
    "Stretch 1400": [
        Rule("max_rating_solved", 1400, "Silver", "Solve a problem rated 1400 or higher (Specialist level)")
    ],
    "Stretch 1600": [
        Rule("max_rating_solved", 1600, "Silver", "Solve a problem rated 1600 or higher")
    ],
    "Stretch 2000": [
        Rule("max_rating_solved", 2000, "Gold", "Solve a problem rated 2000 or higher")
    ],
    "Topic Mastered": [
        Rule("topics_mastered", 1, "Silver", "Have at least 1 topic mastered (awarded once)")
    ],
    "Level Complete": [
        Rule("levels_complete", 1, "Gold", "Complete at least 1 level (awarded once)")
    ],
    "Pupil Reached": [
        Rule("cf_rating", 1200, "Bronze", "Reach Codeforces rating 1200 (Pupil)")
    ],
    "Specialist Reached": [
        Rule("cf_rating", 1400, "Silver", "Reach Codeforces rating 1400 (Specialist)")
    ],
    "Expert Reached": [
        Rule("cf_rating", 1600, "Gold", "Reach Codeforces rating 1600 (Expert)")
    ],
    "CM Reached": [
        Rule("cf_rating", 1900, "Legendary", "Reach Codeforces rating 1900 (Candidate Master)")
    ],
    "Weekly MVP": [
        Rule("weekly_mvp", 1, "Gold", "Be club MVP of a week")
    ],
    "Contest Regular": [
        Rule("contests_month", 4, "Silver", "Attend 4 contests in a calendar month")
    ],
    "Upsolve Hero": [
        Rule("upsolved_full_contests", 1, "Silver", "Fully upsolve 1 contest")
    ],
    "Template Smith": [
        Rule("templates_added", 5, "Silver", "Add 5 code templates")
    ],
    "Librarian": [
        Rule("resources_added", 5, "Bronze", "Add 5 resources")
    ],
    "Team Player": [
        Rule("team_contests", 10, "Silver", "Take part in 10 team contests")
    ],
}

# Human-readable rules for the Guide page: badge -> "rule (tier)".
BADGE_RULES: dict[str, str] = {
    badge: "; ".join(f"{r.text} ({r.tier})" for r in rules)
    for badge, rules in _RULES.items()
}


def _as_number(value: Any) -> float:
    return 0.0 if value is None else float(value)


def evaluate(
    member_stats: Mapping[str, Any],
    already: set[str],
) -> list[tuple[str, str, str]]:
    """Return ``(badge, tier, detail)`` for every newly earned badge.

    Badges in ``already`` are never returned. A missing stat counts as 0.
    """
    earned: list[tuple[str, str, str]] = []
    for badge, rules in _RULES.items():
        if badge in already:
            continue
        for rule in rules:
            value = _as_number(member_stats.get(rule.stat))
            if value >= rule.threshold:
                detail = f"{rule.text}: {rule.stat}={value:g}"
                earned.append((badge, rule.tier, detail))
                break
    return earned
