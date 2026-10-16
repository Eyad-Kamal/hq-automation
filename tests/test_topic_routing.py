"""Same-band topic routing in roadmap order: the pure router (topic_map.route_topics_sequential),
mastery.solve_points, and recompute's ownership rules, writes, cap and missing-property handling."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from hq import notion as nt
from hq import schema as S
from hq.jobs import recompute
from hq.logic import mastery
from hq.logic.topic_map import RouteProblem, TopicInfo, map_topics, route_topics_sequential
from hq.repo import get_number, get_relation, get_text
from tests.fakes import NOW, build_world

WHEN = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
NT1 = TopicInfo("nt1", "Number Theory I", ("number theory", "math"), 1000, 1400, level_no=2, order=7)
NT2 = TopicInfo("nt2", "Number Theory II", ("number theory", "math"), 1000, 1400, level_no=2, order=8)


def factor(rating, low, high, mode):
    return mastery.solve_points(rating, low, high, mode)


def nt_problems(n: int, prefix: str = "p", rating: int = 1200, mode: str = "Practice") -> list[RouteProblem]:
    return [RouteProblem(f"{prefix}{i}", ("number theory",), rating, mode) for i in range(n)]


def route(problems, topics=(NT1, NT2), overrides=None):
    return route_topics_sequential(problems, list(topics), 12, factor, overrides)


# ---- pure routing ----------------------------------------------------------------

def test_first_same_band_topic_fills_to_competent_then_the_next() -> None:
    result = route(nt_problems(14))
    for i in range(12):
        assert result[f"p{i}"] == ["nt1"], i
    assert result["p12"] == ["nt2"]
    assert result["p13"] == ["nt2"]


def test_twelfth_solve_is_still_on_the_first_topic() -> None:
    # Before the 12th solve NT I has 11 points (< 12), so the 12th goes to NT I too.
    result = route(nt_problems(12))
    assert all(v == ["nt1"] for v in result.values())


def test_all_competent_routes_to_the_last_topic() -> None:
    assert route(nt_problems(1), overrides={"nt1": "Competent", "nt2": "Competent"})["p0"] == ["nt2"]


def test_all_competent_by_fixed_rows_routes_to_the_last_topic() -> None:
    fixed = [RouteProblem(f"f{i}", (), 1200, "Practice", fixed=("nt1", "nt2")) for i in range(12)]
    result = route(fixed + [RouteProblem("x", ("number theory",), 1200, "Practice")])
    assert result["x"] == ["nt2"]


def test_coach_override_skips_a_topic() -> None:
    result = route(nt_problems(1), overrides={"nt1": "Proficient"})
    assert result["p0"] == ["nt2"]


def test_coach_override_below_competent_does_not_skip() -> None:
    result = route(nt_problems(1), overrides={"nt1": "Novice", "nt2": "Apprentice"})
    assert result["p0"] == ["nt1"]


def test_single_candidate_matches_map_topics() -> None:
    topics = [
        TopicInfo("impl", "Implementation", ("implementation",), 800, 1200, 1, 1),
        TopicInfo("math-basics", "Math basics", ("math", "number theory"), 1000, 1400, 2, 2),
        TopicInfo("nt", "Number theory", ("number theory",), 1400, 1800, 3, 3),
        TopicInfo("dp", "DP", ("dp",), 1200, 1600, 2, 4),
    ]
    cases = [(("implementation",), 1000), (("number theory",), 600), (("number theory",), 1100),
             (("number theory",), 1500), (("number theory",), 2000), (("math", "dp"), 1300)]
    for tags, rating in cases:
        problems = [RouteProblem("only", tags, rating, "Practice")]
        assert route_topics_sequential(problems, topics, 12, factor)["only"] == map_topics(tags, rating, topics), (tags, rating)


def test_topics_outside_the_tie_group_are_not_considered() -> None:
    # A level-3 topic with the same band is not a tie candidate, so NT I is still first.
    nt3 = TopicInfo("nt3", "Number Theory III", ("number theory",), 1000, 1400, level_no=3, order=1)
    result = route(nt_problems(1), topics=(NT1, NT2, nt3))
    assert result["p0"] == ["nt1"]


def test_distance_decides_the_group_before_order() -> None:
    lower = TopicInfo("lo", "Low", ("number theory",), 1000, 1400, 2, 9)
    higher = TopicInfo("hi", "High", ("number theory",), 1400, 1800, 2, 1)
    # rating 1450 is inside "hi" only: no tie, so "hi" wins even though "lo" has the lower order.
    result = route_topics_sequential([RouteProblem("p", ("number theory",), 1450, "Practice")], [lower, higher], 12, factor)
    assert result["p"] == ["hi"]


def test_order_of_the_input_list_is_the_chronology() -> None:
    forward = route(nt_problems(14))
    backward = route(list(reversed(nt_problems(14))))
    assert forward["p13"] == ["nt2"]
    assert backward["p13"] == ["nt1"], "the first problem in the list fills NT I first"


def test_points_use_the_mastery_factors() -> None:
    # 9 Upsolve (9 * 0.7 = 6.3) + 5 Live (5) = 11.3 < 12: the 15th problem still goes to NT I.
    # Counting every solve as 1 point would have sent it to NT II already (14 >= 12).
    problems = [RouteProblem(f"u{i}", ("number theory",), 1200, "Upsolve") for i in range(9)]
    problems += [RouteProblem(f"l{i}", ("number theory",), 1200, "Live Contest") for i in range(5)]
    problems.append(RouteProblem("last", ("number theory",), 1200, "Practice"))  # 11.3 before it, now 12.3
    problems.append(RouteProblem("after", ("number theory",), 1200, "Practice"))
    result = route(problems)
    assert result["last"] == ["nt1"]
    assert result["after"] == ["nt2"]


def test_solve_points_factors() -> None:
    assert mastery.solve_points(1200, 1000, 1400, "Practice") == 1.0
    assert mastery.solve_points(900, 1000, 1400, "Practice") == 0.25
    assert mastery.solve_points(1500, 1000, 1400, "Upsolve") == pytest.approx(1.05)
    assert mastery.solve_points(1200, 1000, 1400, "With Editorial/Hint") == 0.5


# ---- recompute: fixtures ---------------------------------------------------------

def _topics(w) -> tuple[str, str]:
    ids = []
    for title, order in (("Number Theory I", 7), ("Number Theory II", 8)):
        ids.append(
            w.notion.seed(
                S.DB_TOPICS,
                {
                    S.P_Topics.TITLE: nt.p_title(title),
                    S.P_Topics.ORDER: nt.p_number(order),
                    S.P_Topics.BAND_LOW: nt.p_number(1000),
                    S.P_Topics.BAND_HIGH: nt.p_number(1400),
                    S.P_Topics.CORE: nt.p_checkbox(True),
                    S.P_Topics.CF_TAGS: nt.p_multi(["number theory", "math"]),
                    S.P_Topics.LEVEL: nt.p_relation([w.levels["L2"]]),
                },
            )
        )
    return ids[0], ids[1]


def _add(w, key: str, when: datetime, *, topics=(), auto: str | None = None, imported: bool = False,
         tags=("number theory",), rating: int = 1200) -> str:
    props = {
        S.P_Problems.TITLE: nt.p_title(key),
        S.P_Problems.MEMBER: nt.p_relation([w.eyad]),
        S.P_Problems.PROBLEM_KEY: nt.p_text(key),
        S.P_Problems.RATING: nt.p_number(rating),
        S.P_Problems.CF_TAGS: nt.p_multi(list(tags)),
        S.P_Problems.SOLVED_ON: nt.p_date(when),
        S.P_Problems.MODE: nt.p_select("Practice"),
        S.P_Problems.STATUS: nt.p_status("Solved"),
        S.P_Problems.TOPICS: nt.p_relation(list(topics)),
        S.P_Problems.IMPORTED: nt.p_checkbox(imported),
    }
    if auto is not None:
        props[S.P_Problems.AUTO_TOPICS] = nt.p_text(auto)
    return w.notion.seed(S.DB_PROBLEMS, props)


def _queue(w, topic_id: str, override: str | None = None) -> None:
    props = {
        S.P_LearningQueue.TITLE: nt.p_title("queue"),
        S.P_LearningQueue.MEMBER: nt.p_relation([w.eyad]),
        S.P_LearningQueue.TOPIC: nt.p_relation([topic_id]),
        S.P_LearningQueue.QUEUE_STATUS: nt.p_status("Queued"),
    }
    if override:
        props[S.P_LearningQueue.COACH_OVERRIDE] = nt.p_select(override)
    w.notion.seed(S.DB_LEARNING_QUEUE, props)


def _count(w, topic_id: str) -> int:
    rows = [r for r in w.notion.rows(S.DB_LEARNING_QUEUE) if get_relation(r, S.P_LearningQueue.TOPIC) == [topic_id]]
    assert len(rows) == 1
    return int(get_number(rows[0], S.P_LearningQueue.PROBLEMS_COUNT) or 0)


def _page(w, page_id: str) -> dict:
    page = w.notion.page(page_id)
    assert page is not None
    return page


def _topic_ids(w, page_id: str) -> list[str]:
    return get_relation(_page(w, page_id), S.P_Problems.TOPICS)


def _auto(w, page_id: str) -> str:
    return get_text(_page(w, page_id), S.P_Problems.AUTO_TOPICS)


def _problem_writes(w) -> list[tuple[str, dict]]:
    ids = {r["id"] for r in w.notion.rows(S.DB_PROBLEMS)}
    return [(pid, props) for pid, props in w.notion.updates if pid in ids and (S.P_Problems.TOPICS in props or S.P_Problems.AUTO_TOPICS in props)]


def _run(w, now=NOW):
    return recompute.run(w.context(now=now))


# ---- recompute: routing, ownership, writes ----------------------------------------

def test_recompute_routes_in_solved_on_order_not_insertion_order() -> None:
    w = build_world()
    nt1, nt2 = _topics(w)
    latest = _add(w, "CF-LATE", WHEN + timedelta(days=12))  # inserted first, but solved last
    earlier = [_add(w, f"CF-E{i:02d}", WHEN + timedelta(hours=i)) for i in range(12)]
    _run(w)
    assert _topic_ids(w, latest) == [nt2]
    assert _topic_ids(w, earlier[0]) == [nt1]


def test_equal_solved_on_is_ordered_by_stable_key() -> None:
    w = build_world()
    nt1, nt2 = _topics(w)
    ids = [_add(w, f"CF-T{i:02d}", WHEN) for i in range(13)]
    _run(w)
    assert _topic_ids(w, ids[12]) == [nt2], "the last created row (highest id) is the 13th solve"
    assert _topic_ids(w, ids[0]) == [nt1]


def test_ownership_rules_and_user_edited_rows() -> None:
    w = build_world()
    nt1, nt2 = _topics(w)
    _queue(w, nt1)
    _queue(w, nt2)
    # Twelve user-owned rows on NT I (Topics set by hand, no Auto Topics, not imported): they count.
    fillers = [_add(w, f"CF-F{i:02d}", WHEN + timedelta(hours=i), topics=[nt1]) for i in range(12)]
    later = WHEN + timedelta(days=3)
    a = _add(w, "CF-A", later, topics=[nt1], auto=nt1)                        # (a) Auto == Topics
    b = _add(w, "CF-B", later + timedelta(hours=1), topics=[nt1], auto="", imported=True)  # (b) adopt import
    c = _add(w, "CF-C", later + timedelta(hours=2), topics=[], auto=None)     # (c) no Topics, has CF tags
    u_dp = _add(w, "CF-U", later + timedelta(hours=3), topics=[w.topics["dp"]], auto=nt1)  # edited by hand
    _run(w)

    assert _topic_ids(w, a) == [nt2] and _auto(w, a) == nt2
    assert _topic_ids(w, b) == [nt2] and _auto(w, b) == nt2
    assert _topic_ids(w, c) == [nt2] and _auto(w, c) == nt2
    assert _topic_ids(w, u_dp) == [w.topics["dp"]], "a user-edited row keeps its Topics"
    touched = {pid for pid, _ in _problem_writes(w)}
    assert touched == {a, b, c}, "fillers and the user-edited row are never written"
    assert all(pid not in touched for pid in fillers)
    assert _count(w, nt1) == 12, "user rows count toward mastery with their current Topics (12 fillers)"
    assert _count(w, nt2) == 3


def test_user_row_with_topics_counts_toward_running_mastery() -> None:
    w = build_world()
    nt1, nt2 = _topics(w)
    _queue(w, nt1)
    _queue(w, nt2)
    # A user-owned row on NT I (no Auto, not imported) counts toward NT I. Without it the last
    # row would see 11 points and stay on NT I; with it, it sees 12 and goes to NT II.
    _add(w, "CF-U1", WHEN, topics=[nt1])
    for i in range(11):  # 1 user point + 11 owned = 12 before the last one
        _add(w, f"CF-O{i:02d}", WHEN + timedelta(hours=i + 1), tags=("number theory",))
    last = _add(w, "CF-O99", WHEN + timedelta(hours=20))
    _run(w)
    assert _topic_ids(w, last) == [nt2]


def test_only_changed_values_are_written() -> None:
    w = build_world()
    nt1, nt2 = _topics(w)
    _add(w, "CF-C1", WHEN, topics=[])
    _add(w, "CF-C2", WHEN + timedelta(hours=1), topics=[])
    _run(w)
    assert _problem_writes(w), "first run routes and writes"
    w.notion.reset_writes()
    _run(w)
    assert _problem_writes(w) == [], "second run: nothing differs, nothing written"


def test_user_edit_after_routing_takes_ownership_away() -> None:
    w = build_world()
    nt1, nt2 = _topics(w)
    row = _add(w, "CF-E1", WHEN, topics=[], auto=None)
    _run(w)
    assert _auto(w, row) == nt1
    # The user changes Topics by hand (Auto no longer matches): the row is user-owned now.
    page = _page(w, row)
    page["properties"][S.P_Problems.TOPICS] = nt.p_relation([w.topics["dp"]])
    w.notion.reset_writes()
    _run(w)
    assert _topic_ids(w, row) == [w.topics["dp"]]
    assert not _problem_writes(w)


def test_write_cap_limits_rows_per_run_and_later_runs_finish() -> None:
    w = build_world()
    nt1, nt2 = _topics(w)
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("TOPIC_ROUTE_PER_RUN"), S.P_Config.NUMBER: nt.p_number(1)})
    rows = [_add(w, f"CF-K{i:02d}", WHEN + timedelta(hours=i), topics=[]) for i in range(13)]
    first = _run(w)
    assert "topics routed: 1 (capped: yes)" in first.details
    assert len(_problem_writes(w)) == 1
    runs = 0
    while True:
        runs += 1
        assert runs < 30, "the cap must not stall the routing"
        result = _run(w)
        if re.search(r"topics routed: \d+ \(capped: no\)", result.details):
            break
    assert _topic_ids(w, rows[11]) == [nt1]
    assert _topic_ids(w, rows[12]) == [nt2]
    assert _auto(w, rows[12]) == nt2


def test_missing_auto_topics_skips_writes_with_a_warning_and_still_routes_mastery() -> None:
    w = build_world()
    nt1, nt2 = _topics(w)
    w.notion.declared.get(w.notion.ids[S.DB_PROBLEMS], set()).discard(S.P_Problems.AUTO_TOPICS)
    _queue(w, nt1)
    _queue(w, nt2)
    rows = [_add(w, f"CF-M{i:02d}", WHEN + timedelta(hours=i), topics=[]) for i in range(13)]
    result = _run(w)
    assert 'Problems property "Auto Topics" is missing' in result.details
    assert "topics routed: 0" in result.details
    assert not _problem_writes(w)
    assert _topic_ids(w, rows[0]) == [], "no write: the stored Topics stay as they were"
    assert _count(w, nt1) == 12 and _count(w, nt2) == 1, "mastery uses the routed topics"


def test_coach_override_in_recompute_skips_the_topic() -> None:
    w = build_world()
    nt1, nt2 = _topics(w)
    _queue(w, nt1, override="Competent")
    _queue(w, nt2)
    row = _add(w, "CF-OV", WHEN, topics=[])
    _run(w)
    assert _topic_ids(w, row) == [nt2]
    assert _count(w, nt2) == 1 and _count(w, nt1) == 0


def test_topic_route_per_run_default_is_300() -> None:
    from hq.config import DEFAULTS

    assert DEFAULTS["TOPIC_ROUTE_PER_RUN"] == 300
    assert S.ConfigKey.TOPIC_ROUTE_PER_RUN == "TOPIC_ROUTE_PER_RUN"
    assert S.P_Problems.AUTO_TOPICS == "Auto Topics"
