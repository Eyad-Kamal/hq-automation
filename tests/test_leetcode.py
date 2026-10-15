"""LeetCode source client (offline, fixture-driven)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import requests

from hq.models import Contest, Solve
from hq.schema import Judge, Mode
from hq.sources import leetcode as lc
from tests.helpers import FakeResponse, FakeSession, fixture_json

SINCE = datetime(2026, 9, 21, 14, 13, 20, tzinfo=timezone.utc)  # epoch 1790000000


def graphql_router(
    upcoming: Any = None,
    recent: Any = None,
    questions: Any = None,
) -> FakeSession:
    """Dispatches on the GraphQL query text, the way the real endpoint would."""
    upcoming = fixture_json("leetcode/upcoming.json") if upcoming is None else upcoming
    recent = fixture_json("leetcode/recent_ac.json") if recent is None else recent
    questions = fixture_json("leetcode/questions.json") if questions is None else questions

    def handler(_method: str, _url: str, kwargs: dict[str, Any]) -> FakeResponse:
        body = kwargs["json"]
        query = body["query"]
        if "upcomingContests" in query:
            return FakeResponse(200, upcoming)
        if "recentAcSubmissionList" in query:
            assert body["variables"]["username"] == "tester"
            return FakeResponse(200, recent)
        if "question(" in query:
            slug = body["variables"]["titleSlug"]
            data = questions.get(slug)
            return FakeResponse(200, {"data": {"question": data}})
        return FakeResponse(400, {"errors": [{"message": "unknown query"}]})

    return FakeSession(handler)


# ---------------------------------------------------------------------------
# Tag mapping
# ---------------------------------------------------------------------------
def test_map_tags_maps_known_names_and_drops_unknown() -> None:
    mapped = lc.map_tags(["Dynamic Programming", "Hash Table", "Unknown Tag", "Greedy", "Memoization"])
    assert mapped == ("dp", "data structures", "greedy")


def test_map_tags_deduplicates_and_is_case_insensitive() -> None:
    assert lc.map_tags(["String", "string", "Hash Table", "Hash Table"]) == ("strings", "data structures")


@pytest.mark.parametrize(
    ("lc_name", "cf_tag"),
    [
        ("Backtracking", "brute force"),
        ("Simulation", "implementation"),
        ("Union Find", "dsu"),
        ("Bit Manipulation", "bitmasks"),
        ("Shortest Path", "shortest paths"),
        ("Divide and Conquer", "divide and conquer"),
        ("Number Theory", "number theory"),
        ("Combinatorics", "combinatorics"),
        ("Geometry", "geometry"),
        ("Two Pointers", "two pointers"),
        ("Sorting", "sortings"),
        ("Binary Search", "binary search"),
        ("Graph", "graphs"),
        ("Tree", "trees"),
        ("Math", "math"),
    ],
)
def test_each_documented_mapping(lc_name: str, cf_tag: str) -> None:
    assert lc.map_tags([lc_name]) == (cf_tag,)


def test_map_tags_empty() -> None:
    assert lc.map_tags([]) == ()


# ---------------------------------------------------------------------------
# Upcoming contests
# ---------------------------------------------------------------------------
def test_upcoming_contests_map_fields() -> None:
    session = graphql_router()
    contests = lc.fetch_upcoming_contests(session)
    assert session.urls() == ["https://leetcode.com/graphql"]
    assert [c.external_id for c in contests] == ["LC-weekly-contest-470", "LC-biweekly-contest-170"]
    first = contests[0]
    assert isinstance(first, Contest)
    assert first.platform == "LeetCode"
    assert first.name == "Weekly Contest 470"
    assert first.start_utc == datetime(2026, 10, 20, 12, 40, tzinfo=timezone.utc)
    assert first.duration_min == 90  # 5400 seconds, assumed unit
    assert first.link == "https://leetcode.com/contest/weekly-contest-470"


def test_upcoming_contests_empty_on_graphql_errors() -> None:
    session = FakeSession(lambda *_: FakeResponse(200, {"errors": [{"message": "rate limited"}], "data": None}))
    assert lc.fetch_upcoming_contests(session) == []


# ---------------------------------------------------------------------------
# Solves
# ---------------------------------------------------------------------------
def test_solves_keep_first_ac_since_and_sort() -> None:
    solves = lc.fetch_solves(graphql_router(), "tester", SINCE)
    assert [s.problem_key for s in solves] == ["LC-two-sum", "LC-word-break", "LC-mystery-problem"]
    assert "LC-old-problem" not in [s.problem_key for s in solves]  # before `since`


def test_first_ac_timestamp_and_fields() -> None:
    by_key = {s.problem_key: s for s in lc.fetch_solves(graphql_router(), "tester", SINCE)}
    two_sum = by_key["LC-two-sum"]
    assert isinstance(two_sum, Solve)
    assert two_sum.judge == Judge.LEETCODE
    assert two_sum.solved_at_utc == datetime.fromtimestamp(1790000100, tz=timezone.utc)  # earliest of the two
    assert two_sum.name == "Two Sum"
    assert two_sum.link == "https://leetcode.com/problems/two-sum/"
    assert two_sum.difficulty == "Easy"
    assert two_sum.rating is None
    assert two_sum.tags == ("data structures",)  # "Array" is unmapped and dropped
    assert two_sum.mode == Mode.PRACTICE
    assert two_sum.contest_external_id is None


def test_medium_problem_tags_are_mapped() -> None:
    by_key = {s.problem_key: s for s in lc.fetch_solves(graphql_router(), "tester", SINCE)}
    word_break = by_key["LC-word-break"]
    assert word_break.difficulty == "Medium"
    assert word_break.tags == ("data structures", "strings", "dp")  # Trie and Memoization dropped


def test_question_lookup_failure_keeps_the_solve_without_details() -> None:
    by_key = {s.problem_key: s for s in lc.fetch_solves(graphql_router(), "tester", SINCE)}
    mystery = by_key["LC-mystery-problem"]  # not in the questions fixture
    assert mystery.difficulty is None
    assert mystery.tags == ()
    assert mystery.name == "Mystery Problem"


def test_solves_empty_on_graphql_errors() -> None:
    session = FakeSession(lambda *_: FakeResponse(200, {"errors": [{"message": "bad"}], "data": None}))
    assert lc.fetch_solves(session, "tester", SINCE) == []


def test_unknown_difficulty_string_is_ignored() -> None:
    questions = {"two-sum": {"difficulty": "Impossible", "topicTags": []}}
    by_key = {s.problem_key: s for s in lc.fetch_solves(graphql_router(questions=questions), "tester", SINCE)}
    assert by_key["LC-two-sum"].difficulty is None


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------
FAILURES: list[Any] = [
    pytest.param(lambda *_: requests.ConnectionError("dns"), id="network-error"),
    pytest.param(lambda *_: FakeResponse(500, text="upstream"), id="http-500"),
    pytest.param(lambda *_: FakeResponse(200, text="<html>blocked</html>"), id="non-json"),
    pytest.param(lambda *_: FakeResponse(200, {"data": "not-a-dict-of-fields"}), id="wrong-shape"),
]


@pytest.mark.parametrize("handler", FAILURES)
def test_failures_return_empty(handler: Any) -> None:
    session = FakeSession(handler)
    assert lc.fetch_upcoming_contests(session) == []
    assert lc.fetch_solves(session, "tester", SINCE) == []
