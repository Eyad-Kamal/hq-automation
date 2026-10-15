from hq.logic.topic_map import TopicInfo, map_topics

IMPL = TopicInfo(
    id="impl", name="Implementation", cf_tags=("implementation",),
    band_low=800, band_high=1200, level_no=1, order=1,
)
MATH = TopicInfo(
    id="math-basics", name="Math basics", cf_tags=("math", "number theory"),
    band_low=1000, band_high=1400, level_no=2, order=2,
)
NT = TopicInfo(
    id="nt", name="Number theory", cf_tags=("number theory",),
    band_low=1400, band_high=1800, level_no=3, order=3,
)
DP = TopicInfo(
    id="dp", name="DP", cf_tags=("dp",),
    band_low=1200, band_high=1600, level_no=2, order=4,
)
TOPICS = [IMPL, MATH, NT, DP]


def test_single_tag_single_topic():
    assert map_topics(["implementation"], 1000, TOPICS) == ["impl"]


def test_no_matching_tag_returns_empty():
    assert map_topics(["flows"], 1500, TOPICS) == []


def test_empty_tags_returns_empty():
    assert map_topics([], 1500, TOPICS) == []


def test_empty_topic_list_returns_empty():
    assert map_topics(["dp"], 1500, []) == []


def test_candidate_whose_band_contains_rating_wins():
    # "number theory": MATH band 1000-1400 and NT band 1400-1800.
    # rating 1500 is inside NT only -> NT wins.
    assert map_topics(["number theory"], 1500, TOPICS) == ["nt"]


def test_rating_inside_first_band_picks_that_band():
    # rating 1100 is inside MATH only -> MATH.
    assert map_topics(["number theory"], 1100, TOPICS) == ["math-basics"]


def test_nearest_band_when_no_band_contains_rating():
    # rating 600: MATH band low 1000 (distance 400) vs NT band low 1400 (distance 800).
    assert map_topics(["number theory"], 600, TOPICS) == ["math-basics"]


def test_nearest_band_above_all_bands():
    # rating 2000: NT high 1800 (distance 200) vs MATH high 1400 (distance 600).
    assert map_topics(["number theory"], 2000, TOPICS) == ["nt"]


def test_tie_on_distance_breaks_by_lower_level():
    a = TopicInfo("a", "A", ("x",), 1000, 1200, level_no=2, order=1)
    b = TopicInfo("b", "B", ("x",), 1400, 1600, level_no=1, order=9)
    # rating 1300: both are 100 away from their band edge; level 1 (b) wins.
    assert map_topics(["x"], 1300, [a, b]) == ["b"]


def test_tie_on_distance_and_level_breaks_by_lower_order():
    a = TopicInfo("a", "A", ("x",), 1000, 1200, level_no=1, order=5)
    b = TopicInfo("b", "B", ("x",), 1400, 1600, level_no=1, order=2)
    assert map_topics(["x"], 1300, [a, b]) == ["b"]


def test_result_is_unique_and_first_seen_order():
    tags = ["math", "implementation", "number theory", "implementation"]
    # math -> math-basics; implementation -> impl; number theory (rating 1100) -> math-basics (dup)
    assert map_topics(tags, 1100, TOPICS) == ["math-basics", "impl"]


def test_first_seen_order_follows_tag_order():
    assert map_topics(["implementation", "math"], 1100, TOPICS) == ["impl", "math-basics"]
    assert map_topics(["math", "implementation"], 1100, TOPICS) == ["math-basics", "impl"]


def test_tag_matching_is_case_insensitive():
    assert map_topics(["Implementation"], 1000, TOPICS) == ["impl"]
    assert map_topics(["  NUMBER THEORY "], 1500, TOPICS) == ["nt"]


def test_topic_cf_tags_matching_is_case_insensitive():
    upper = TopicInfo("u", "U", ("DP",), 1000, 1400, 1, 1)
    assert map_topics(["dp"], 1100, [upper]) == ["u"]


def test_multiple_tags_to_different_topics():
    assert map_topics(["dp", "implementation"], 1300, TOPICS) == ["dp", "impl"]
