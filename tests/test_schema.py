"""Sanity checks on the schema constants (titles and property names)."""

from __future__ import annotations

import inspect

from hq import schema


def _property_classes() -> list[type]:
    return [obj for name, obj in vars(schema).items() if name.startswith("P_") and inspect.isclass(obj)]


def test_nineteen_database_titles_and_unique() -> None:
    assert len(schema.DATABASE_TITLES) == 19
    assert len(set(schema.DATABASE_TITLES)) == 19


def test_every_property_class_maps_to_a_known_title() -> None:
    titles = set(schema.DATABASE_TITLES)
    class_titles = {
        schema.P_Levels: schema.DB_LEVELS,
        schema.P_Topics: schema.DB_TOPICS,
        schema.P_Members: schema.DB_MEMBERS,
        schema.P_Teams: schema.DB_TEAMS,
        schema.P_Problems: schema.DB_PROBLEMS,
        schema.P_Contests: schema.DB_CONTESTS,
        schema.P_ContestEntries: schema.DB_CONTEST_ENTRIES,
        schema.P_Tasks: schema.DB_TASKS,
        schema.P_LearningQueue: schema.DB_LEARNING_QUEUE,
        schema.P_Goals: schema.DB_GOALS,
        schema.P_Resources: schema.DB_RESOURCES,
        schema.P_CodeTemplates: schema.DB_CODE_TEMPLATES,
        schema.P_Achievements: schema.DB_ACHIEVEMENTS,
        schema.P_WeeklyReports: schema.DB_WEEKLY_REPORTS,
        schema.P_DailyLog: schema.DB_DAILY_LOG,
        schema.P_WeeklySummary: schema.DB_WEEKLY_SUMMARY,
        schema.P_RatingHistory: schema.DB_RATING_HISTORY,
        schema.P_Config: schema.DB_CONFIG,
        schema.P_SyncLog: schema.DB_SYNC_LOG,
    }
    assert set(class_titles.values()) == titles
    assert len(_property_classes()) == len(titles)


def test_property_names_are_unique_within_each_database() -> None:
    for cls in _property_classes():
        values = [v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str)]
        assert len(values) == len(set(values)), f"duplicate property name in {cls.__name__}"


def test_known_spec_names_are_verbatim() -> None:
    assert schema.P_Problems.TITLE == "Problem"
    assert schema.P_Problems.TIME_MIN == "Time (min)"
    assert schema.P_ContestEntries.RATING_DELTA == "Rating Δ"
    assert schema.P_Members.WEEK_DELTA == "Week Δ"
    assert schema.P_RatingHistory.DELTA == "Δ"
    assert schema.P_LearningQueue.TIER == "Tier"
    assert schema.P_Contests.STARTS_IN == "Starts In"
    assert schema.P_SyncLog.TITLE == "Run"


def test_option_values_match_spec() -> None:
    assert schema.Mode.LIVE == "Live Contest"
    assert schema.Mode.EDITORIAL == "With Editorial/Hint"
    assert schema.Judge.CF_GYM == "CF Gym"
    assert "dp" in schema.CF_TAGS and "2-sat" in schema.CF_TAGS
    assert len(schema.CF_TAGS) == len(set(schema.CF_TAGS)) == 37
    assert len(schema.BADGES) == 24
