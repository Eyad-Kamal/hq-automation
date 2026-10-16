from hq.logic import achievements

# Exact Achievements."Badge" option names from build/schema-spec.md.
SPEC_BADGES = {
    "First Blood", "Century (100)", "250 Club", "500 Club", "1000 Club",
    "Week Warrior (7🔥)", "Iron Habit (30🔥)", "Unbreakable (100🔥)",
    "Stretch 1200", "Stretch 1400", "Stretch 1600", "Stretch 2000",
    "Topic Mastered", "Level Complete",
    "Pupil Reached", "Specialist Reached", "Expert Reached", "CM Reached",
    "Weekly MVP", "Contest Regular", "Upsolve Hero", "Template Smith",
    "Librarian", "Team Player",
}


def names(result):
    return {badge for badge, _tier, _detail in result}


def tier_of(result, badge):
    for b, t, _d in result:
        if b == badge:
            return t
    return None


def test_every_spec_badge_has_a_rule():
    assert set(achievements.BADGE_RULES) == SPEC_BADGES


def test_badge_rules_are_human_readable_strings():
    for badge, text in achievements.BADGE_RULES.items():
        assert isinstance(text, str) and text.strip(), badge


def test_empty_stats_earns_nothing():
    assert achievements.evaluate({}, set()) == []


def test_first_blood_at_one_problem():
    result = achievements.evaluate({"problems_total": 1}, set())
    assert tier_of(result, "First Blood") == "Bronze"
    assert "Century (100)" not in names(result)


def test_century_threshold_is_100():
    assert "Century (100)" not in names(achievements.evaluate({"problems_total": 99}, set()))
    result = achievements.evaluate({"problems_total": 100}, set())
    assert tier_of(result, "Century (100)") == "Silver"


def test_club_tiers():
    result = achievements.evaluate({"problems_total": 1000}, set())
    assert tier_of(result, "250 Club") == "Silver"
    assert tier_of(result, "500 Club") == "Gold"
    assert tier_of(result, "1000 Club") == "Legendary"


def test_streak_badges_by_best_streak():
    assert "Week Warrior (7🔥)" in names(achievements.evaluate({"best_streak": 7}, set()))
    assert "Week Warrior (7🔥)" not in names(achievements.evaluate({"best_streak": 6}, set()))
    result = achievements.evaluate({"best_streak": 100}, set())
    assert tier_of(result, "Iron Habit (30🔥)") == "Gold"
    assert tier_of(result, "Unbreakable (100🔥)") == "Legendary"


def test_stretch_thresholds_are_cumulative():
    result = achievements.evaluate({"max_rating_solved": 1600}, set())
    assert tier_of(result, "Stretch 1200") == "Bronze"
    assert tier_of(result, "Stretch 1600") == "Silver"
    assert "Stretch 2000" not in names(result)


def test_stretch_1400_awards_at_1400_not_1399():
    result = achievements.evaluate({"max_rating_solved": 1400}, set())
    assert tier_of(result, "Stretch 1400") == "Silver"
    assert tier_of(result, "Stretch 1200") == "Bronze"
    assert "Stretch 1600" not in names(result)
    assert "Stretch 1400" not in names(achievements.evaluate({"max_rating_solved": 1399}, set()))


def test_stretch_2000_is_gold():
    result = achievements.evaluate({"max_rating_solved": 2000}, set())
    assert tier_of(result, "Stretch 2000") == "Gold"


def test_rank_badges_by_cf_rating():
    result = achievements.evaluate({"cf_rating": 1400}, set())
    assert {"Pupil Reached", "Specialist Reached"} <= names(result)
    assert "Expert Reached" not in names(result)

    result = achievements.evaluate({"cf_rating": 1900}, set())
    assert tier_of(result, "Pupil Reached") == "Bronze"
    assert tier_of(result, "Specialist Reached") == "Silver"
    assert tier_of(result, "Expert Reached") == "Gold"
    assert tier_of(result, "CM Reached") == "Legendary"


def test_single_threshold_badges():
    stats = {
        "topics_mastered": 1,
        "levels_complete": 1,
        "weekly_mvp": 1,
        "contests_month": 4,
        "upsolved_full_contests": 1,
        "templates_added": 5,
        "resources_added": 5,
        "team_contests": 10,
    }
    result = achievements.evaluate(stats, set())
    assert tier_of(result, "Topic Mastered") == "Silver"
    assert tier_of(result, "Level Complete") == "Gold"
    assert tier_of(result, "Weekly MVP") == "Gold"
    assert tier_of(result, "Contest Regular") == "Silver"
    assert tier_of(result, "Upsolve Hero") == "Silver"
    assert tier_of(result, "Template Smith") == "Silver"
    assert tier_of(result, "Librarian") == "Bronze"
    assert tier_of(result, "Team Player") == "Silver"


def test_just_below_single_thresholds_earn_nothing():
    stats = {
        "contests_month": 3,
        "templates_added": 4,
        "resources_added": 4,
        "team_contests": 9,
    }
    assert names(achievements.evaluate(stats, set())) == set()


def test_already_awarded_badges_are_never_returned():
    stats = {"problems_total": 1000, "cf_rating": 1900}
    already = {"First Blood", "1000 Club", "Pupil Reached"}
    result = achievements.evaluate(stats, already)
    assert names(result).isdisjoint(already)
    assert "250 Club" in names(result)


def test_each_badge_returned_at_most_once_and_detail_present():
    stats = {"problems_total": 1000, "best_streak": 100, "cf_rating": 1900, "max_rating_solved": 2000}
    result = achievements.evaluate(stats, set())
    badges = [b for b, _t, _d in result]
    assert len(badges) == len(set(badges))
    for _b, _t, detail in result:
        assert detail


def test_evaluate_returns_tuples_of_three_strings():
    for row in achievements.evaluate({"problems_total": 1}, set()):
        assert len(row) == 3
        assert all(isinstance(x, str) for x in row)
