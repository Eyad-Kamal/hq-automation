"""Classify a contest into a Contests."Kind" option from its platform and name.

The returned strings are exactly the Kind options in build/schema-spec.md, so
the classification can be written straight into Notion and compared with the
Members "Contest Subscriptions" options.
"""

from __future__ import annotations

import re

_DIV_1_PLUS_2 = re.compile(r"div\.?\s*1\s*(?:\+|&|and)\s*div\.?\s*2", re.IGNORECASE)
_DIV_N = re.compile(r"div\.?\s*([1-4])\b", re.IGNORECASE)
_ICPC = re.compile(r"\bicpc\b", re.IGNORECASE)
_ABC = re.compile(r"\bABC\d*\b", re.IGNORECASE)
_ARC = re.compile(r"\bARC\d*\b", re.IGNORECASE)
_AGC = re.compile(r"\bAGC\d*\b", re.IGNORECASE)
_BEGINNER = re.compile(r"atcoder beginner", re.IGNORECASE)
_REGULAR = re.compile(r"atcoder regular", re.IGNORECASE)
_GRAND = re.compile(r"grand contest", re.IGNORECASE)
_LC_BIWEEKLY = re.compile(r"\bbiweekly\b", re.IGNORECASE)
_LC_WEEKLY = re.compile(r"\bweekly\b", re.IGNORECASE)
_EDUCATIONAL = re.compile(r"\beducational\b", re.IGNORECASE)

# Kind options that a member can subscribe to. "AtCoder Other" and "Other"
# are never subscribable.
_NEVER_SUBSCRIBED = frozenset({"AtCoder Other", "Other"})


def _classify_codeforces(name: str) -> str:
    if _EDUCATIONAL.search(name):
        return "CF Educational"
    if _DIV_1_PLUS_2.search(name):
        return "CF Div. 1+2"
    match = _DIV_N.search(name)
    if match:
        return f"CF Div. {match.group(1)}"
    return "CF Other"


def _classify_atcoder(name: str) -> str:
    if _BEGINNER.search(name) or _ABC.search(name):
        return "AtCoder ABC"
    if _REGULAR.search(name) or _ARC.search(name):
        return "AtCoder ARC"
    if _GRAND.search(name) or _AGC.search(name):
        return "AtCoder AGC"
    return "AtCoder Other"


def _classify_leetcode(name: str) -> str:
    # Check "biweekly" first: "Biweekly Contest" also contains "weekly".
    if _LC_BIWEEKLY.search(name):
        return "LC Biweekly"
    if _LC_WEEKLY.search(name):
        return "LC Weekly"
    return "Other"


def classify(platform: str, name: str) -> str:
    """Return the Contests."Kind" option for a contest.

    Rules, in order:

    1. platform "CF Gym" (or "Gym") -> "Gym".
    2. name mentions ICPC -> "ICPC Mirror" (heuristic, any platform).
    3. platform "Codeforces": Educational -> "CF Educational";
       "Div. 1 + Div. 2" -> "CF Div. 1+2"; "Div. 1..4" -> "CF Div. N";
       otherwise "CF Other" (Global Rounds etc.).
    4. platform "AtCoder": Beginner/ABC -> "AtCoder ABC"; Regular/ARC ->
       "AtCoder ARC"; Grand/AGC -> "AtCoder AGC"; otherwise "AtCoder Other".
    5. platform "LeetCode": "Biweekly" -> "LC Biweekly"; "Weekly" -> "LC Weekly".
    6. anything else -> "Other".
    """
    plat = platform.strip().lower()
    if plat in {"cf gym", "gym"}:
        return "Gym"
    if _ICPC.search(name):
        return "ICPC Mirror"
    if plat == "codeforces":
        return _classify_codeforces(name)
    if plat == "atcoder":
        return _classify_atcoder(name)
    if plat == "leetcode":
        return _classify_leetcode(name)
    return "Other"


def subscription_matches(kind: str, subscriptions: list[str]) -> bool:
    """Return True if a member subscribed to ``kind``.

    "AtCoder Other" and "Other" never match, even if listed.
    """
    if kind in _NEVER_SUBSCRIBED:
        return False
    return kind in subscriptions
