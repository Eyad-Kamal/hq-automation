"""Topic and level goals: "Topic Problems", "Topic Tier", "Level Topics ≥ Competent".

Unit tests on hq.logic.goals, then job tests: the goal's Linked Topics / Linked Level, team
aggregation (Problems and level counts add up, Topic Tier takes the best member), and a goal
without the link it needs is skipped with a warning.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from hq import notion as nt
from hq import schema as S
from hq.jobs import imports, onboard, recompute
from hq.logic import goals
from hq.repo import get_number, get_status, get_title
from tests.fakes import NOW, build_world, seed_eyad_solves

TODAY_CAIRO = date(2026, 10, 15)


# ---------------------------------------------------------------------------
# Unit tests: hq.logic.goals
# ---------------------------------------------------------------------------
def test_tier_ordinals_match_the_spec_scale() -> None:
    expected = {"Unseen": 0, "Theory": 1, "Novice": 2, "Apprentice": 3, "Competent": 4, "Proficient": 5, "Master": 6}
    assert {name: goals.tier_rank(name) for name in expected} == expected
    assert goals.tier_rank(None) == 0
    assert goals.tier_rank("Bogus") == 0


def test_metric_names_are_the_schema_option_values() -> None:
    assert goals.TOPIC_PROBLEMS == "Topic Problems" == S.GoalMetric.TOPIC_PROBLEMS
    assert goals.TOPIC_TIER == "Topic Tier" == S.GoalMetric.TOPIC_TIER
    assert goals.LEVEL_TOPICS_COMPETENT == "Level Topics ≥ Competent" == S.GoalMetric.LEVEL_TOPICS_COMPETENT
    assert goals.TOPIC_METRICS == {"Topic Problems", "Topic Tier", "Level Topics ≥ Competent"}


STATS = {
    "solved_topics": [["greedy"], ["strings"], ["dp", "greedy"], []],
    "topic_tiers": {"greedy": "Proficient", "strings": "Novice", "dp": "Competent", "geo": "Master"},
}


def test_topic_problems_counts_solved_problems_touching_any_linked_topic() -> None:
    assert goals.current_value("Topic Problems", STATS, topic_ids=["greedy"]) == 2.0
    assert goals.current_value("Topic Problems", STATS, topic_ids=["greedy", "strings"]) == 3.0
    assert goals.current_value("Topic Problems", STATS, topic_ids=["geo"]) == 0.0


def test_topic_problems_counts_a_problem_once_even_when_it_matches_two_linked_topics() -> None:
    assert goals.current_value("Topic Problems", STATS, topic_ids=["greedy", "dp"]) == 2.0


def test_topic_tier_is_the_best_rank_among_linked_topics() -> None:
    assert goals.current_value("Topic Tier", STATS, topic_ids=["strings", "dp"]) == 4.0
    assert goals.current_value("Topic Tier", STATS, topic_ids=["greedy", "geo"]) == 6.0
    assert goals.current_value("Topic Tier", STATS, topic_ids=["unknown"]) == 0.0


def test_level_topics_competent_counts_level_topics_at_competent_or_above() -> None:
    level = ["greedy", "strings", "dp", "geo"]
    assert goals.current_value("Level Topics ≥ Competent", STATS, level_topic_ids=level) == 3.0
    assert goals.current_value("Level Topics ≥ Competent", STATS, level_topic_ids=["strings"]) == 0.0
    assert goals.current_value("Level Topics ≥ Competent", STATS, level_topic_ids=[]) == 0.0


@pytest.mark.parametrize("metric", ["Topic Problems", "Topic Tier"])
def test_topic_metrics_without_linked_topics_raise(metric: str) -> None:
    with pytest.raises(ValueError):
        goals.current_value(metric, STATS)
    with pytest.raises(ValueError):
        goals.current_value(metric, STATS, topic_ids=[])


def test_level_metric_without_linked_level_raises() -> None:
    with pytest.raises(ValueError):
        goals.current_value("Level Topics ≥ Competent", STATS)


def test_topic_metrics_with_no_member_data_are_zero() -> None:
    assert goals.current_value("Topic Problems", {}, topic_ids=["greedy"]) == 0.0
    assert goals.current_value("Topic Tier", {}, topic_ids=["greedy"]) == 0.0
    assert goals.current_value("Level Topics ≥ Competent", {}, level_topic_ids=["greedy"]) == 0.0


# ---------------------------------------------------------------------------
# Job tests: recompute writes Goals Current
# ---------------------------------------------------------------------------
def _setup():
    w = build_world()
    seed_eyad_solves(w)
    return w


def _override(w, member_id: str, topic_id: str, tier: str) -> None:
    """A Learning Queue row with a coach tier; onboard then skips this (member, topic) pair."""
    w.notion.seed(
        S.DB_LEARNING_QUEUE,
        {
            S.P_LearningQueue.TITLE: nt.p_title(f"override {tier}"),
            S.P_LearningQueue.MEMBER: nt.p_relation([member_id]),
            S.P_LearningQueue.TOPIC: nt.p_relation([topic_id]),
            S.P_LearningQueue.COACH_OVERRIDE: nt.p_select(tier),
        },
    )


def _goal(w, title: str, metric: str, *, scope: str = "Person", member=None, team=None, topics=(), level=None, target=100) -> str:
    props = {
        S.P_Goals.TITLE: nt.p_title(title),
        S.P_Goals.SCOPE: nt.p_select(scope),
        S.P_Goals.METRIC: nt.p_select(metric),
        S.P_Goals.TARGET: nt.p_number(target),
        S.P_Goals.STATUS: nt.p_status("Active"),
        S.P_Goals.LINKED_TOPICS: nt.p_relation(list(topics)),
    }
    if member is not None:
        props[S.P_Goals.MEMBER] = nt.p_relation([member])
    if team is not None:
        props[S.P_Goals.TEAM] = nt.p_relation([team])
    if level is not None:
        props[S.P_Goals.LINKED_LEVEL] = nt.p_relation([level])
    return w.notion.seed(S.DB_GOALS, props)


def _goal_row(w, title: str) -> dict:
    rows = [g for g in w.notion.rows(S.DB_GOALS) if get_title(g, S.P_Goals.TITLE) == title]
    assert len(rows) == 1, title
    return rows[0]


def _chain(w, now=NOW):
    onboard.run(w.context(now=now))
    imports.run(w.context(now=now))
    return recompute.run(w.context(now=now))


def test_person_goals_use_their_links() -> None:
    w = _setup()
    # Eyad's solves: 1985C greedy, 1990A strings, 1999B dp (topic assignment by CF tag).
    _override(w, w.eyad, w.topics["greedy"], "Proficient")
    _override(w, w.eyad, w.topics["strings"], "Novice")
    _goal(w, "Topic problems greedy+dp", "Topic Problems", member=w.eyad, topics=[w.topics["greedy"], w.topics["dp"]])
    _goal(w, "Topic tier greedy+strings", "Topic Tier", member=w.eyad, topics=[w.topics["greedy"], w.topics["strings"]])
    _goal(w, "L1 competent", "Level Topics ≥ Competent", member=w.eyad, level=w.levels["L1"])
    _chain(w)
    assert get_number(_goal_row(w, "Topic problems greedy+dp"), S.P_Goals.CURRENT) == 2
    assert get_number(_goal_row(w, "Topic tier greedy+strings"), S.P_Goals.CURRENT) == 5
    # Only greedy (Proficient) is at Competent or above among the two L1 topics.
    assert get_number(_goal_row(w, "L1 competent"), S.P_Goals.CURRENT) == 1


def test_level_goal_with_a_level_that_has_no_topics_counts_zero() -> None:
    w = _setup()
    empty_level = w.notion.seed(S.DB_LEVELS, {S.P_Levels.TITLE: nt.p_title("L9 · Empty"), S.P_Levels.LEVEL_NO: nt.p_number(9)})
    _goal(w, "Empty level", "Level Topics ≥ Competent", member=w.eyad, level=empty_level)
    _chain(w)
    assert get_number(_goal_row(w, "Empty level"), S.P_Goals.CURRENT) == 0


def test_topic_goal_without_linked_topics_is_skipped_with_a_warning(caplog) -> None:
    w = _setup()
    _goal(w, "No links", "Topic Problems", member=w.eyad)
    _goal(w, "No level", "Level Topics ≥ Competent", member=w.eyad)
    _goal(w, "No topic tier link", "Topic Tier", member=w.eyad)
    with caplog.at_level(logging.WARNING, logger="hq.jobs.recompute"):
        _chain(w)
    for title in ("No links", "No level", "No topic tier link"):
        goal = _goal_row(w, title)
        assert get_number(goal, S.P_Goals.CURRENT) is None, "a skipped goal gets no Current"
    assert "goal No links skipped" in caplog.text
    assert "goal No level skipped" in caplog.text


def test_team_goals_add_problem_and_level_counts_and_take_the_best_tier() -> None:
    w = _setup()
    # A second team member with one greedy solve, so team sums are visible.
    omar = w.notion.seed(
        S.DB_MEMBERS,
        {
            S.P_Members.TITLE: nt.p_title("Omar"),
            S.P_Members.ACTIVE: nt.p_checkbox(True),
            S.P_Members.LEVEL: nt.p_relation([w.levels["L1"]]),
            S.P_Members.TEAM: nt.p_relation([w.team]),
        },
    )
    w.notion.seed(
        S.DB_PROBLEMS,
        {
            S.P_Problems.TITLE: nt.p_title("CF-1000B"),
            S.P_Problems.STATUS: nt.p_status("Solved"),
            S.P_Problems.MEMBER: nt.p_relation([omar]),
            S.P_Problems.PROBLEM_KEY: nt.p_text("CF-1000B"),
            S.P_Problems.RATING: nt.p_number(1000),
            S.P_Problems.MODE: nt.p_select("Practice"),
            S.P_Problems.SOLVED_ON: nt.p_date(date(2026, 10, 14)),
            S.P_Problems.TOPICS: nt.p_relation([w.topics["greedy"]]),
        },
    )
    # Eyad: greedy Proficient, strings Novice. Omar: greedy Novice, strings Competent.
    _override(w, w.eyad, w.topics["greedy"], "Proficient")
    _override(w, w.eyad, w.topics["strings"], "Novice")
    _override(w, omar, w.topics["greedy"], "Novice")
    _override(w, omar, w.topics["strings"], "Competent")
    _goal(w, "Team topic problems", "Topic Problems", scope="Team", team=w.team, topics=[w.topics["greedy"]])
    _goal(w, "Team topic tier", "Topic Tier", scope="Team", team=w.team, topics=[w.topics["greedy"]])
    _goal(w, "Team level", "Level Topics ≥ Competent", scope="Team", team=w.team, level=w.levels["L1"])
    _chain(w)
    # Problems add up: Eyad's 1985C (greedy) and Omar's solve.
    assert get_number(_goal_row(w, "Team topic problems"), S.P_Goals.CURRENT) == 2
    # Tier takes the best member (5), not the sum (7).
    assert get_number(_goal_row(w, "Team topic tier"), S.P_Goals.CURRENT) == 5
    # Eyad counts 1 (greedy), Omar counts 1 (strings): the sum is 2.
    assert get_number(_goal_row(w, "Team level"), S.P_Goals.CURRENT) == 2


def test_achieved_when_topic_goal_reaches_target() -> None:
    w = _setup()
    _override(w, w.eyad, w.topics["greedy"], "Proficient")
    _goal(w, "Greedy tier", "Topic Tier", member=w.eyad, topics=[w.topics["greedy"]], target=5)
    _chain(w)
    row = _goal_row(w, "Greedy tier")
    assert get_number(row, S.P_Goals.CURRENT) == 5
    assert get_status(row, S.P_Goals.STATUS) == "Achieved"
