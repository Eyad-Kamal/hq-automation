"""Codeforces source client (offline, fixture-driven)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import requests

from hq._net import RateLimiter
from hq.models import Contest, RatingChange, Solve
from hq.schema import Judge, Mode
from hq.sources import codeforces as cf
from tests.helpers import FakeClock, FakeResponse, FakeSession, fixture_json

SINCE = datetime(2026, 9, 21, 14, 13, 20, tzinfo=timezone.utc)  # epoch 1790000000
BEFORE_SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Every test gets its own clock; the module limiter sleeps on the fake clock."""
    clock = FakeClock()
    monkeypatch.setattr(cf, "_limiter", RateLimiter(2.1, clock=clock, sleep=clock.sleep))
    return clock


def cf_session(payload: Any) -> FakeSession:
    return FakeSession(lambda *_: FakeResponse(200, payload))


def status_ok(result: Any) -> dict[str, Any]:
    return {"status": "OK", "result": result}


# ---------------------------------------------------------------------------
# Upcoming contests
# ---------------------------------------------------------------------------
def test_upcoming_contests_keep_only_before_phase_and_request_non_gym() -> None:
    session = cf_session(fixture_json("codeforces/contest_list.json"))
    contests = cf.fetch_upcoming_contests(session)
    assert session.calls[0]["params"] == {"gym": "false"}
    assert session.urls() == ["https://codeforces.com/api/contest.list"]
    assert [c.external_id for c in contests] == ["CF-2065", "CF-2066"]


def test_upcoming_contest_fields() -> None:
    contests = cf.fetch_upcoming_contests(cf_session(fixture_json("codeforces/contest_list.json")))
    first = contests[0]
    assert isinstance(first, Contest)
    assert first.platform == "Codeforces"
    assert first.name == "Codeforces Round 1000 (Div. 2)"
    assert first.start_utc == datetime(2026, 10, 20, 12, 35, tzinfo=timezone.utc)
    assert first.start_utc.tzinfo is not None
    assert first.duration_min == 120
    assert first.link == "https://codeforces.com/contests/2065"
    assert contests[1].duration_min == 125


def test_upcoming_contests_empty_when_none_scheduled() -> None:
    session = cf_session(status_ok([]))
    assert cf.fetch_upcoming_contests(session) == []


# ---------------------------------------------------------------------------
# Solves: first AC, attempts, mode, gym, since
# ---------------------------------------------------------------------------
def solves_by_key(solves: list[Solve]) -> dict[str, Solve]:
    return {s.problem_key: s for s in solves}


def test_fetch_solves_keeps_first_ac_and_drops_old_and_failed() -> None:
    session = cf_session(fixture_json("codeforces/user_status.json"))
    solves = cf.fetch_solves(session, "tester", SINCE)
    keys = [s.problem_key for s in solves]
    assert keys == ["CF-1985C", "GYM-104114A", "CF-1950D", "CF-1990A"]  # sorted by solve time
    assert "CF-2000B" not in keys  # never accepted
    assert "CF-1500A" not in keys  # first AC before `since`


def test_first_ac_is_the_earliest_accepted_and_attempts_count_failures() -> None:
    solves = solves_by_key(cf.fetch_solves(cf_session(fixture_json("codeforces/user_status.json")), "tester", SINCE))
    solve = solves["CF-1985C"]
    assert solve.solved_at_utc == datetime.fromtimestamp(1790000400, tz=timezone.utc)
    assert solve.attempts == 2  # one WA then the first OK; the later practice OK is ignored
    assert solve.mode == Mode.LIVE
    assert solve.rating == 1200
    assert solve.tags == ("greedy", "math")
    assert solve.judge == Judge.CODEFORCES
    assert solve.link == "https://codeforces.com/contest/1985/problem/C"
    assert solve.contest_external_id == "CF-1985"
    assert solve.difficulty is None


def test_gym_problem_gets_gym_key_judge_and_link() -> None:
    solve = solves_by_key(cf.fetch_solves(cf_session(fixture_json("codeforces/user_status.json")), "tester", SINCE))["GYM-104114A"]
    assert solve.judge == Judge.CF_GYM
    assert solve.link == "https://codeforces.com/gym/104114/problem/A"
    assert solve.contest_external_id == "GYM-104114"
    assert solve.tags == ("dp", "implementation")  # lowercased
    assert solve.mode == Mode.PRACTICE


def test_missing_rating_is_none_and_virtual_mode() -> None:
    solve = solves_by_key(cf.fetch_solves(cf_session(fixture_json("codeforces/user_status.json")), "tester", SINCE))["CF-1990A"]
    assert solve.rating is None
    assert solve.mode == Mode.VIRTUAL
    assert solve.tags == ("implementation",)


def test_out_of_competition_maps_to_practice() -> None:
    solve = solves_by_key(cf.fetch_solves(cf_session(fixture_json("codeforces/user_status.json")), "tester", SINCE))["CF-1950D"]
    assert solve.mode == Mode.PRACTICE
    assert solve.rating == 1700


@pytest.mark.parametrize(
    ("participant", "expected"),
    [
        ("CONTESTANT", Mode.LIVE),
        ("VIRTUAL", Mode.VIRTUAL),
        ("PRACTICE", Mode.PRACTICE),
        ("OUT_OF_COMPETITION", Mode.PRACTICE),
        ("MANAGER", Mode.PRACTICE),
    ],
)
def test_mode_mapping_from_participant_type(participant: str, expected: str) -> None:
    sub = {
        "id": 1,
        "contestId": 1000,
        "creationTimeSeconds": 1790000000,
        "problem": {"contestId": 1000, "index": "A", "name": "P", "tags": []},
        "author": {"participantType": participant},
        "verdict": "OK",
    }
    solves = cf.solves_from_submissions([sub], SINCE)
    assert solves[0].mode == expected


def test_gym_threshold_boundary() -> None:
    base = {"creationTimeSeconds": 1790000000, "verdict": "OK", "author": {}}
    regular = cf.solves_from_submissions([{**base, "id": 1, "problem": {"contestId": 99999, "index": "A", "name": "a", "tags": []}}], SINCE)
    gym = cf.solves_from_submissions([{**base, "id": 2, "problem": {"contestId": 100000, "index": "A", "name": "b", "tags": []}}], SINCE)
    assert regular[0].judge == Judge.CODEFORCES
    assert gym[0].judge == Judge.CF_GYM


def test_submissions_without_problem_index_are_ignored() -> None:
    subs = [{"id": 1, "creationTimeSeconds": 1790000000, "verdict": "OK", "problem": {"name": "no index"}}]
    assert cf.solves_from_submissions(subs, SINCE) == []


def test_paging_fetches_full_history_to_find_the_first_ac() -> None:
    """1200 submissions, newest first. The earliest accepted one (index 700, in the
    second page) is the first AC; a later accepted one (index 10) must be ignored.
    All three pages (500, 500, 200) are needed, and attempts = 500."""
    total = 1200
    base = 1790000000
    accepted_indexes = {700, 10}
    history = []
    for i in range(total):  # index 0 is the newest
        created = base + (total - i) * 10
        verdict = "OK" if i in accepted_indexes else "WRONG_ANSWER"
        history.append(
            {
                "id": 900000 + i,
                "contestId": 1000,
                "creationTimeSeconds": created,
                "problem": {"contestId": 1000, "index": "A", "name": "Long", "tags": ["math"], "rating": 800},
                "author": {"participantType": "PRACTICE"},
                "verdict": verdict,
            }
        )

    def handler(_method: str, _url: str, kwargs: dict[str, Any]) -> FakeResponse:
        start = kwargs["params"]["from"]
        count = kwargs["params"]["count"]
        return FakeResponse(200, status_ok(history[start - 1 : start - 1 + count]))

    session = FakeSession(handler)
    since = datetime.fromtimestamp(base, tz=timezone.utc)
    solves = cf.fetch_solves(session, "tester", since)
    assert len(solves) == 1
    assert solves[0].problem_key == "CF-1000A"
    assert solves[0].attempts == 500  # chronological: 499 failures, then the AC at index 700
    assert solves[0].solved_at_utc == datetime.fromtimestamp(base + (total - 700) * 10, tz=timezone.utc)
    assert [c["params"]["from"] for c in session.calls] == [1, 501, 1001]


def test_partial_paging_failure_returns_empty_not_a_wrong_first_ac() -> None:
    page_one = [
        {"id": 2, "contestId": 1, "creationTimeSeconds": 1790000100, "problem": {"contestId": 1, "index": "A", "name": "A", "tags": []}, "author": {}, "verdict": "WRONG_ANSWER"}
    ] * 500
    responses = [FakeResponse(200, status_ok(page_one)), FakeResponse(500)]
    session = FakeSession(lambda *_: responses.pop(0))
    assert cf.fetch_solves(session, "tester", SINCE) == []


def test_solves_sorted_by_solve_time_and_naive_since_accepted() -> None:
    subs = [
        {"id": 2, "contestId": 5, "creationTimeSeconds": 1790000500, "problem": {"contestId": 5, "index": "B", "name": "B", "tags": []}, "author": {}, "verdict": "OK"},
        {"id": 1, "contestId": 5, "creationTimeSeconds": 1790000200, "problem": {"contestId": 5, "index": "A", "name": "A", "tags": []}, "author": {}, "verdict": "OK"},
    ]
    naive_since = datetime(2026, 9, 21, 14, 13, 20)  # treated as UTC
    solves = cf.solves_from_submissions(subs, naive_since)
    assert [s.problem_key for s in solves] == ["CF-5A", "CF-5B"]


# ---------------------------------------------------------------------------
# Rating history, user info, problemset
# ---------------------------------------------------------------------------
def test_rating_history_maps_fields() -> None:
    session = cf_session(fixture_json("codeforces/user_rating.json"))
    changes = cf.fetch_rating_history(session, "tester")
    assert session.calls[0]["params"] == {"handle": "tester"}
    assert len(changes) == 2
    first = changes[0]
    assert isinstance(first, RatingChange)
    assert first.platform == "Codeforces"
    assert first.contest_name == "Codeforces Round 955 (Div. 2)"
    assert first.date_utc == datetime.fromtimestamp(1790000000, tz=timezone.utc)
    assert (first.old, first.new, first.rank) == (1500, 1550, 1543)


def test_user_info_matches_handles_case_insensitively() -> None:
    session = cf_session(fixture_json("codeforces/user_info.json"))
    info = cf.fetch_user_info(session, ["tourist", "tester"])
    assert session.calls[0]["params"] == {"handles": "tourist;tester"}
    assert info == {
        "tourist": {"rating": 3800, "maxRating": 3900},
        "tester": {"rating": None, "maxRating": 1550},
    }


def test_user_info_with_no_handles_makes_no_call() -> None:
    session = cf_session(status_ok([]))
    assert cf.fetch_user_info(session, []) == {}
    assert session.calls == []


def test_problemset_maps_keys_links_and_skips_malformed() -> None:
    items = cf.fetch_problemset(cf_session(fixture_json("codeforces/problemset.json")))
    assert [i["key"] for i in items] == ["CF-1985C", "CF-1990A"]
    assert items[0] == {
        "key": "CF-1985C",
        "name": "Permutation Swap",
        "link": "https://codeforces.com/problemset/problem/1985/C",
        "rating": 1200,
        "tags": ["greedy", "math"],
    }
    assert items[1]["rating"] is None


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------
def test_calls_are_spaced_at_least_2_1_seconds(_fresh_limiter: FakeClock) -> None:
    clock = _fresh_limiter
    responses = [FakeResponse(200, status_ok([])), FakeResponse(200, status_ok([])), FakeResponse(200, status_ok([]))]
    session = FakeSession(lambda *_: responses.pop(0))
    cf.fetch_upcoming_contests(session)
    cf.fetch_user_info(session, ["tourist"])
    cf.fetch_rating_history(session, "tourist")
    assert len(session.calls) == 3
    assert len(clock.sleeps) == 2
    assert all(s == pytest.approx(2.1) for s in clock.sleeps)


# ---------------------------------------------------------------------------
# Failure paths: never raise, return empty
# ---------------------------------------------------------------------------
FAILURES: list[Any] = [
    pytest.param(lambda *_: requests.ConnectionError("dns down"), id="network-error"),
    pytest.param(lambda *_: FakeResponse(500, text="boom"), id="http-500"),
    pytest.param(lambda *_: FakeResponse(200, text="<html>not json</html>"), id="non-json"),
    pytest.param(lambda *_: FakeResponse(200, {"status": "FAILED", "comment": "handle: User not found"}), id="api-failed"),
    pytest.param(lambda *_: FakeResponse(200, {"status": "OK", "result": "unexpected-shape"}), id="wrong-shape"),
]


@pytest.mark.parametrize("handler", FAILURES)
def test_failures_return_empty_list(handler: Any) -> None:
    session = FakeSession(handler)
    assert cf.fetch_upcoming_contests(session) == []
    assert cf.fetch_solves(session, "tester", SINCE) == []
    assert cf.fetch_rating_history(session, "tester") == []
    assert cf.fetch_problemset(session) == []


@pytest.mark.parametrize("handler", FAILURES)
def test_failures_return_empty_dict_for_user_info(handler: Any) -> None:
    assert cf.fetch_user_info(FakeSession(handler), ["tester"]) == {}


def test_malformed_row_degrades_to_empty_instead_of_raising() -> None:
    payload = status_ok([{"id": 1, "name": "x", "phase": "BEFORE"}])  # no startTimeSeconds: skipped
    assert cf.fetch_upcoming_contests(cf_session(payload)) == []
    broken = status_ok([{"id": 2, "name": "y", "phase": "BEFORE", "startTimeSeconds": 1, "durationSeconds": "n/a"}])
    assert cf.fetch_upcoming_contests(cf_session(broken)) == []  # int() fails inside the guard
