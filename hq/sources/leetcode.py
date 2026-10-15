"""LeetCode source client (unofficial GraphQL at https://leetcode.com/graphql).

Best-effort: LeetCode does not publish this API, and field names can change
without notice. Every call logs and degrades to an empty result on failure.

Limits of the recent-submissions query: it returns only the last 50 accepted
submissions, and it carries no attempt counts, so ``attempts`` is set to 1 as
a placeholder.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import requests

from hq._net import from_epoch, get_json, guarded
from hq.models import Contest, Solve
from hq.schema import Difficulty, Judge, Mode, Platform

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://leetcode.com/graphql"
RECENT_LIMIT = 50

UPCOMING_QUERY = "query upcomingContests { upcomingContests { title titleSlug startTime duration } }"
RECENT_QUERY = (
    "query recentAcSubmissions($username: String!, $limit: Int!) "
    "{ recentAcSubmissionList(username: $username, limit: $limit) { id title titleSlug timestamp } }"
)
QUESTION_QUERY = (
    "query questionData($titleSlug: String!) "
    "{ question(titleSlug: $titleSlug) { difficulty topicTags { name slug } } }"
)

# LeetCode topic tag name -> CF tag vocabulary. Unmapped tags are dropped.
LC_TAG_MAP: dict[str, str] = {
    "Dynamic Programming": "dp",
    "Greedy": "greedy",
    "Binary Search": "binary search",
    "Graph": "graphs",
    "Tree": "trees",
    "String": "strings",
    "Math": "math",
    "Two Pointers": "two pointers",
    "Bit Manipulation": "bitmasks",
    "Hash Table": "data structures",
    "Sorting": "sortings",
    "Union Find": "dsu",
    "Shortest Path": "shortest paths",
    "Number Theory": "number theory",
    "Combinatorics": "combinatorics",
    "Geometry": "geometry",
    "Divide and Conquer": "divide and conquer",
    "Backtracking": "brute force",
    "Simulation": "implementation",
}
_LC_TAG_FOLD: dict[str, str] = {k.casefold(): v for k, v in LC_TAG_MAP.items()}

_DIFFICULTIES = {Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD}


def map_tags(names: Iterable[str]) -> tuple[str, ...]:
    """Map LeetCode topic names to CF-style tags, dropping unknown ones and duplicates."""
    seen: list[str] = []
    for name in names:
        mapped = _LC_TAG_FOLD.get(str(name).casefold())
        if mapped and mapped not in seen:
            seen.append(mapped)
    return tuple(seen)


def _graphql(
    session: requests.Session,
    query: str,
    variables: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """POST one GraphQL query. Returns ``data`` or None (logged) on any failure."""
    body = get_json(
        session,
        "POST",
        GRAPHQL_URL,
        json_body={"query": query, "variables": variables or {}},
        headers={"Content-Type": "application/json", "Referer": "https://leetcode.com", **(headers or {})},
    )
    if not isinstance(body, dict):
        return None
    if body.get("errors"):
        log.warning("LeetCode GraphQL errors: %s", body["errors"])
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


DAILY_QUERY = "query { activeDailyCodingChallengeQuestion { date link question { title titleSlug difficulty topicTags { name } } } }"
# LeetCode may refuse non-browser clients for this query, so it is sent with a browser-like User-Agent.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class DailyQuestion:
    """LeetCode's daily challenge: the date it is for, and the problem."""

    day: date
    title: str
    slug: str
    difficulty: str | None
    topic_names: tuple[str, ...]  # LeetCode's own topic names, as published

    @property
    def problem_key(self) -> str:
        return f"LC-{self.slug}"

    @property
    def link(self) -> str:
        return f"https://leetcode.com/problems/{self.slug}/"


@guarded(lambda: None)
def fetch_daily_question_or_none(session: requests.Session) -> DailyQuestion | None:
    """Today's LeetCode daily challenge, or None when it cannot be read (logged).

    Live shape: {"data": {"activeDailyCodingChallengeQuestion": {"date": "YYYY-MM-DD",
    "link": "/problems/<slug>/", "question": {"title", "titleSlug", "difficulty",
    "topicTags": [{"name"}, ...]}}}}.
    """
    data = _graphql(session, DAILY_QUERY, headers={"User-Agent": BROWSER_USER_AGENT})
    if data is None:
        return None
    daily = data.get("activeDailyCodingChallengeQuestion")
    question = daily.get("question") if isinstance(daily, dict) else None
    if not isinstance(question, dict):
        log.warning("LeetCode daily: no question in the response")
        return None
    title = str(question.get("title") or "").strip()
    slug = str(question.get("titleSlug") or "").strip()
    try:
        day = date.fromisoformat(str(daily.get("date")))
    except (TypeError, ValueError):
        log.warning("LeetCode daily: unreadable date %r", daily.get("date"))
        return None
    if not title or not slug:
        log.warning("LeetCode daily: title or slug is missing")
        return None
    difficulty = question.get("difficulty")
    names = tuple(str(t.get("name")) for t in question.get("topicTags") or [] if isinstance(t, dict) and t.get("name"))
    return DailyQuestion(
        day=day,
        title=title,
        slug=slug,
        difficulty=difficulty if difficulty in _DIFFICULTIES else None,
        topic_names=names,
    )


@guarded(list)
def fetch_upcoming_contests(session: requests.Session) -> list[Contest]:
    """Upcoming LeetCode contests. ``duration`` is assumed to be seconds (not verified)."""
    data = _graphql(session, UPCOMING_QUERY)
    if data is None:
        return []
    contests: list[Contest] = []
    for row in data.get("upcomingContests") or []:
        slug = row["titleSlug"]
        contests.append(
            Contest(
                platform=Platform.LEETCODE,
                external_id=f"LC-{slug}",
                name=row["title"],
                start_utc=from_epoch(row["startTime"]),
                duration_min=int(row["duration"]) // 60,
                link=f"https://leetcode.com/contest/{slug}",
            )
        )
    return contests


def _question_fields(session: requests.Session, slug: str) -> dict[str, Any] | None:
    data = _graphql(session, QUESTION_QUERY, {"titleSlug": slug})
    if data is None:
        return None
    question = data.get("question")
    return question if isinstance(question, dict) else None


@guarded(list)
def fetch_solves(session: requests.Session, username: str, since_utc: datetime) -> list[Solve]:
    """Recent accepted LeetCode solves at or after ``since_utc`` (first AC per slug).

    If a question's details cannot be fetched, that solve is kept with
    ``difficulty=None`` and no tags. Empty on failure.
    """
    return fetch_solves_or_none(session, username, since_utc) or []


@guarded(lambda: None)
def fetch_solves_or_none(session: requests.Session, username: str, since_utc: datetime) -> list[Solve] | None:
    """Like ``fetch_solves`` but None when the recent-submissions call failed."""
    data = _graphql(session, RECENT_QUERY, {"username": username, "limit": RECENT_LIMIT})
    if data is None:
        return None
    since_ts = since_utc.replace(tzinfo=timezone.utc).timestamp() if since_utc.tzinfo is None else since_utc.timestamp()

    first_ac: dict[str, dict[str, Any]] = {}
    for sub in sorted(data.get("recentAcSubmissionList") or [], key=lambda s: int(s["timestamp"])):
        ts = int(sub["timestamp"])
        if ts < since_ts or sub["titleSlug"] in first_ac:
            continue
        first_ac[sub["titleSlug"]] = sub

    solves: list[Solve] = []
    for slug, sub in first_ac.items():
        question = _question_fields(session, slug)
        difficulty = None
        tags: tuple[str, ...] = ()
        if question is not None:
            if question.get("difficulty") in _DIFFICULTIES:
                difficulty = question["difficulty"]
            tags = map_tags(t.get("name", "") for t in question.get("topicTags") or [])
        solves.append(
            Solve(
                judge=Judge.LEETCODE,
                problem_key=f"LC-{slug}",
                name=sub["title"],
                link=f"https://leetcode.com/problems/{slug}/",
                rating=None,
                difficulty=difficulty,
                tags=tags,
                solved_at_utc=from_epoch(int(sub["timestamp"])),
                mode=Mode.PRACTICE,
                contest_external_id=None,
                attempts=1,
            )
        )
    return sorted(solves, key=lambda s: s.solved_at_utc)
