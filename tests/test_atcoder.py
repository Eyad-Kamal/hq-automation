"""AtCoder source client (offline, fixture-driven)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import requests

from hq._net import RateLimiter
from hq.models import Contest, RatingChange, Solve
from hq.schema import Judge, Mode
from hq.sources import atcoder as at
from tests.helpers import FakeClock, FakeResponse, FakeSession, fixture_json, fixture_text

SINCE = datetime(2026, 9, 21, 14, 13, 20, tzinfo=timezone.utc)  # epoch 1790000000
PROBLEMS_URL = "https://kenkoooo.com/atcoder/resources/problems.json"
MODELS_URL = "https://kenkoooo.com/atcoder/resources/problem-models.json"
SUBMISSIONS_URL = "https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions"
CONTESTS_URL = "https://kenkoooo.com/atcoder/resources/contests.json"


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(at, "_limiter", RateLimiter(1.1, clock=clock, sleep=clock.sleep))
    return clock


def kenkoooo_router(
    submissions: Any = None,
    problems: Any = None,
    models: Any = None,
    contests: Any = None,
) -> FakeSession:
    """Routes the four kenkoooo endpoints to fixture-like payloads."""
    if submissions is None:
        submissions = fixture_json("atcoder/submissions.json")
    if problems is None:
        problems = fixture_json("atcoder/problems.json")
    if models is None:
        models = fixture_json("atcoder/problem_models.json")
    if contests is None:
        contests = fixture_json("atcoder/contests_resource.json")

    def handler(_m: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        if url == SUBMISSIONS_URL:
            start = kwargs["params"]["from_second"]
            rows = [s for s in submissions if s["epoch_second"] >= start][:500]
            return FakeResponse(200, rows)
        if url == PROBLEMS_URL:
            return FakeResponse(200, problems)
        if url == MODELS_URL:
            return FakeResponse(200, models)
        if url == CONTESTS_URL:
            return FakeResponse(200, contests)
        return FakeResponse(404)

    return FakeSession(handler)


# ---------------------------------------------------------------------------
# Difficulty clipping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "offset", "expected"),
    [
        (None, 0, None),
        (0, 0, 147),  # 400 / e
        (120, 0, 199),
        (399, 0, 399),
        (400, 0, 400),
        (800, 0, 800),
        (800, 100, 900),
        (1350, 100, 1450),
        (0, -500, 0),  # clamped at zero
    ],
)
def test_clip_difficulty(raw: float | None, offset: int, expected: int | None) -> None:
    assert at.clip_difficulty(raw, offset) == expected


# ---------------------------------------------------------------------------
# Upcoming contests (HTML)
# ---------------------------------------------------------------------------
def test_parse_upcoming_table_picks_only_the_upcoming_table() -> None:
    rows = at.parse_upcoming_table(fixture_text("atcoder/contests.html"))
    assert [r["href"] for r in rows] == [
        "/contests/abc500",
        "/contests/ahc073",
        "/contests/arc210",
        "/contests/agc080",
    ]
    assert rows[0] == {
        "start": "2026-10-18 21:00:00+0900",
        "name": "Ⓐ◉ AtCoder Beginner Contest 500",  # raw page text, glyphs included
        "duration": "01:40",
        "href": "/contests/abc500",
    }
    assert all("abc499" not in r["href"] for r in rows)  # Recent Contests table is ignored


def test_parse_upcoming_table_without_heading_returns_nothing() -> None:
    assert at.parse_upcoming_table("<html><body><table><tr><td>x</td></tr></table></body></html>") == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ⓐ◉ AtCoder Beginner Contest 480", "AtCoder Beginner Contest 480"),
        ("Ⓗ◉ AtCoder Heuristic Contest 073", "AtCoder Heuristic Contest 073"),
        ("  ◉  AtCoder Regular Contest 210", "AtCoder Regular Contest 210"),
        ("AtCoder Grand Contest 080", "AtCoder Grand Contest 080"),  # nothing to strip
        ("(1) Mystery", "1) Mystery"),  # only leading symbols go; the digit starts the name
        ("◉◉ ", ""),  # no ASCII letter or digit at all
    ],
)
def test_clean_contest_name_strips_leading_decoration(raw: str, expected: str) -> None:
    assert at.clean_contest_name(raw) == expected


def test_upcoming_contest_names_have_no_decorative_prefix() -> None:
    session = FakeSession(lambda *_: FakeResponse(200, text=fixture_text("atcoder/contests.html")))
    names = [c.name for c in at.fetch_upcoming_contests(session)]
    assert names == [
        "AtCoder Beginner Contest 500",
        "AtCoder Heuristic Contest 073",
        "AtCoder Regular Contest 210",
    ]
    assert all(name[0].isascii() and name[0].isalnum() for name in names)


def test_upcoming_contests_parsed_with_utc_start_and_duration() -> None:
    session = FakeSession(lambda *_: FakeResponse(200, text=fixture_text("atcoder/contests.html")))
    contests = at.fetch_upcoming_contests(session)
    assert session.urls() == ["https://atcoder.jp/contests/"]
    # malformed AGC row (slash-style date) is skipped; the AHC row is kept
    assert [c.external_id for c in contests] == ["AT-abc500", "AT-ahc073", "AT-arc210"]

    abc = contests[0]
    assert isinstance(abc, Contest)
    assert abc.platform == "AtCoder"
    assert abc.name == "AtCoder Beginner Contest 500"
    assert abc.start_utc == datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)  # 21:00 at +0900
    assert abc.duration_min == 100
    assert abc.link == "https://atcoder.jp/contests/abc500"

    ahc = contests[1]
    assert ahc.name == "AtCoder Heuristic Contest 073"
    assert ahc.start_utc == datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)  # 15:00 at +0900
    assert ahc.duration_min == 240
    assert ahc.link == "https://atcoder.jp/contests/ahc073"

    arc = contests[2]
    assert arc.start_utc == datetime(2026, 10, 25, 11, 0, tzinfo=timezone.utc)
    assert arc.duration_min == 120


def test_upcoming_contests_empty_when_table_missing() -> None:
    session = FakeSession(lambda *_: FakeResponse(200, text="<html>maintenance</html>"))
    assert at.fetch_upcoming_contests(session) == []


# ---------------------------------------------------------------------------
# Solves
# ---------------------------------------------------------------------------
def test_solves_first_ac_attempts_and_since_filter() -> None:
    solves = at.fetch_solves(kenkoooo_router(), "tester", SINCE, 0)
    # abc100_a is before `since`; ahc073_a is a heuristic contest and is excluded
    assert [s.problem_key for s in solves] == ["AT-abc300_d", "AT-abc380_a", "AT-abc380_b"]
    by_key = {s.problem_key: s for s in solves}

    d = by_key["AT-abc300_d"]
    assert isinstance(d, Solve)
    assert d.judge == Judge.ATCODER
    assert d.name == "Sample Problem Delta"
    assert d.link == "https://atcoder.jp/contests/abc300/tasks/abc300_d"
    assert d.solved_at_utc == datetime.fromtimestamp(1790000200, tz=timezone.utc)  # first AC, not the later one
    assert d.attempts == 2  # one WA, then the first AC
    assert d.contest_external_id == "AT-abc300"
    assert d.mode == Mode.LIVE  # 1790000200 is inside abc300's window
    assert d.tags == ()
    assert d.difficulty is None
    assert by_key["AT-abc380_a"].mode == Mode.PRACTICE  # abc380 window starts after the AC


def test_low_difficulty_is_clipped_and_offset_applied() -> None:
    by_key = {s.problem_key: s for s in at.fetch_solves(kenkoooo_router(), "tester", SINCE, 0)}
    assert by_key["AT-abc300_d"].rating == 199  # raw 120 -> clipped to 199
    assert by_key["AT-abc380_a"].rating == 1350  # above 400: unchanged

    shifted = {s.problem_key: s for s in at.fetch_solves(kenkoooo_router(), "tester", SINCE, 100)}
    assert shifted["AT-abc300_d"].rating == 299
    assert shifted["AT-abc380_a"].rating == 1450


def test_missing_difficulty_gives_none_rating_and_counts_retries() -> None:
    by_key = {s.problem_key: s for s in at.fetch_solves(kenkoooo_router(), "tester", SINCE, 0)}
    b = by_key["AT-abc380_b"]
    assert b.rating is None  # present in problems.json but no difficulty value
    assert b.attempts == 2  # TLE then AC
    assert b.solved_at_utc == datetime.fromtimestamp(1790000600, tz=timezone.utc)


def test_solves_are_sorted_by_solve_time() -> None:
    keys = [s.problem_key for s in at.fetch_solves(kenkoooo_router(), "tester", SINCE, 0)]
    times = [s.solved_at_utc for s in at.fetch_solves(kenkoooo_router(), "tester", SINCE, 0)]
    assert times == sorted(times)
    assert keys[0] == "AT-abc300_d"


def test_paging_follows_last_epoch_second_plus_one() -> None:
    """1100 submissions at one per second, all rejected except the very last one.
    Three calls (500, 500, 100) are needed; attempts must equal 1100."""
    base = 1790000000
    subs = [
        {"id": i, "epoch_second": base + i, "problem_id": "abc500_a", "contest_id": "abc500", "result": "AC" if i == 1099 else "WA"}
        for i in range(1100)
    ]
    session = kenkoooo_router(submissions=subs, problems={}, models={})
    solves = at.fetch_solves(session, "tester", datetime.fromtimestamp(base, tz=timezone.utc), 0)
    assert len(solves) == 1
    assert solves[0].attempts == 1100
    sub_calls = [c for c in session.calls if c["url"] == SUBMISSIONS_URL]
    assert [c["params"]["from_second"] for c in sub_calls] == [0, base + 500, base + 1000]


def test_kenkoooo_calls_are_at_least_1_1_seconds_apart(_fresh_limiter: FakeClock) -> None:
    session = kenkoooo_router()
    at.fetch_solves(session, "tester", SINCE, 0)
    assert len(session.calls) == 4  # submissions, problems, models, contests
    assert len(_fresh_limiter.sleeps) == 3
    assert all(s == pytest.approx(1.1) for s in _fresh_limiter.sleeps)


def test_contests_json_is_fetched_once_per_call() -> None:
    session = kenkoooo_router()
    at.fetch_solves(session, "tester", SINCE, 0)
    assert session.urls().count(CONTESTS_URL) == 1


def test_names_and_ratings_degrade_when_resources_fail() -> None:
    def handler(_m: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        if url == SUBMISSIONS_URL:
            return FakeResponse(200, fixture_json("atcoder/submissions.json"))
        return FakeResponse(500)

    solves = at.fetch_solves(FakeSession(handler), "tester", SINCE, 0)
    by_key = {s.problem_key: s for s in solves}
    assert by_key["AT-abc300_d"].name == "abc300_d"  # falls back to the problem id
    assert by_key["AT-abc300_d"].rating is None
    assert by_key["AT-abc300_d"].contest_external_id == "AT-abc300"  # from the submission row
    # contests.json failed too, so the mode falls back to Practice
    assert by_key["AT-abc300_d"].mode == Mode.PRACTICE


def test_contest_window_is_inclusive_at_both_ends() -> None:
    contests = {"abc1": (1000, 1500)}  # (start, end) window, both inclusive
    base = {"id": 1, "problem_id": "abc1_a", "contest_id": "abc1", "result": "AC"}
    epoch_zero = datetime.fromtimestamp(0, tz=timezone.utc)  # small epochs must not be filtered by `since`
    for epoch, expected in [(999, Mode.PRACTICE), (1000, Mode.LIVE), (1500, Mode.LIVE), (1501, Mode.PRACTICE)]:
        solves = at.solves_from_submissions(
            [{**base, "epoch_second": epoch}], epoch_zero, 0, {}, {}, contests
        )
        assert solves[0].mode == expected, epoch


def test_heuristic_contest_is_excluded_from_solves() -> None:
    solves = at.fetch_solves(kenkoooo_router(), "tester", SINCE, 0)
    keys = [s.problem_key for s in solves]
    assert not any("ahc" in key for key in keys)
    # the AHC problem has difficulty 3884 in the models fixture, which must not leak in
    assert all(s.rating is None or s.rating < 3000 for s in solves)


def test_heuristic_detection_by_contest_or_problem_id() -> None:
    base = {"epoch_second": 1790000200, "result": "AC", "id": 1}
    by_contest = [{**base, "problem_id": "xyz_a", "contest_id": "ahc999"}]
    by_problem = [{**base, "problem_id": "ahc999_b", "contest_id": "abc999"}]
    assert at.solves_from_submissions(by_contest, SINCE, 0, {}, {}) == []
    assert at.solves_from_submissions(by_problem, SINCE, 0, {}, {}) == []


def test_malformed_contest_windows_are_skipped_not_fatal() -> None:
    rows = [
        {"id": "abc300", "start_epoch_second": 1790000000, "duration_second": 3600},
        {"id": "abc301", "start_epoch_second": "bad", "duration_second": 3600},
        {"id": "abc302", "duration_second": 3600},
    ]
    session = kenkoooo_router(contests=rows)
    by_key = {s.problem_key: s for s in at.fetch_solves(session, "tester", SINCE, 0)}
    assert by_key["AT-abc300_d"].mode == Mode.LIVE
    assert by_key["AT-abc380_a"].mode == Mode.PRACTICE


def test_submissions_failure_returns_empty() -> None:
    session = FakeSession(lambda *_: FakeResponse(503))
    assert at.fetch_solves(session, "tester", SINCE, 0) == []


def test_pure_solves_from_submissions_ignores_non_ac() -> None:
    subs = [{"id": 1, "epoch_second": 1790000100, "problem_id": "abc1_a", "contest_id": "abc1", "result": "TLE"}]
    assert at.solves_from_submissions(subs, SINCE, 0, {}, {}) == []


# ---------------------------------------------------------------------------
# Rating history
# ---------------------------------------------------------------------------
def test_rating_history_keeps_rated_contests_only() -> None:
    session = FakeSession(lambda *_: FakeResponse(200, fixture_json("atcoder/history.json")))
    changes = at.fetch_rating_history(session, "tester")
    assert session.urls() == ["https://atcoder.jp/users/tester/history/json"]
    assert len(changes) == 2
    first = changes[0]
    assert isinstance(first, RatingChange)
    assert first.platform == "AtCoder"
    assert first.contest_name == "AtCoder Beginner Contest 300"
    assert first.date_utc == datetime(2024, 1, 20, 12, 0, tzinfo=timezone.utc)
    assert (first.old, first.new, first.rank) == (1500, 1520, 1234)
    assert changes[1].contest_name == "AtCoder Regular Contest 170"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------
FAILURES: list[Any] = [
    pytest.param(lambda *_: requests.ConnectionError("timeout"), id="network-error"),
    pytest.param(lambda *_: FakeResponse(500, text="boom"), id="http-500"),
    pytest.param(lambda *_: FakeResponse(200, text="not json"), id="non-json"),
    pytest.param(lambda *_: FakeResponse(200, {"unexpected": True}), id="wrong-shape"),
]


@pytest.mark.parametrize("handler", FAILURES)
def test_failures_return_empty(handler: Any) -> None:
    session = FakeSession(handler)
    assert at.fetch_upcoming_contests(session) == []
    assert at.fetch_solves(session, "tester", SINCE, 0) == []
    assert at.fetch_rating_history(session, "tester") == []
