from hq.logic import autotasks


def prob(key, rating, tags=("dp",), name=None):
    return {
        "key": key,
        "name": name or key,
        "link": f"https://codeforces.com/problemset/problem/{key}",
        "rating": rating,
        "tags": list(tags),
    }


POOL = [
    prob("1A", 1000, ("dp",)),
    prob("1B", 1100, ("dp", "greedy")),
    prob("1C", 1200, ("math",)),
    prob("1D", 1300, ("dp",)),
    prob("1E", 1400, ("graphs",)),
    prob("1F", None, ("dp",)),       # unrated -> never picked
    prob("1G", 1150, ("DP",)),       # case-insensitive tag match
]


def keys(result):
    return [p["key"] for p in result]


def test_n_zero_or_negative_returns_empty():
    assert autotasks.pick_problems(POOL, [], ["dp"], 1000, 1200, 0, 1) == []
    assert autotasks.pick_problems(POOL, [], ["dp"], 1000, 1200, -2, 1) == []


def test_filters_by_band_and_tag():
    # Exactly 3 in-band dp candidates (1A, 1B, 1G) so n=3 needs no widening.
    result = autotasks.pick_problems(POOL, [], ["dp"], 1000, 1200, 3, seed=1)
    assert set(keys(result)) == {"1A", "1B", "1G"}  # 1D (1300) out of band, 1C/1E wrong tag


def test_excludes_solved_keys():
    # Only 1G remains in band after excluding 1A and 1B; n=1 needs no widening.
    result = autotasks.pick_problems(POOL, ["1A", "1B"], ["dp"], 1000, 1200, 1, seed=1)
    assert keys(result) == ["1G"]


def test_unrated_problems_are_excluded():
    result = autotasks.pick_problems(POOL, [], ["dp"], 1000, 1500, 10, seed=1)
    assert "1F" not in keys(result)


def test_tag_match_is_case_insensitive_on_both_sides():
    # Band 1150-1150 holds only 1G (tag "DP"); n=1 needs no widening.
    result = autotasks.pick_problems(POOL, [], ["DP"], 1150, 1150, 1, seed=1)
    assert keys(result) == ["1G"]


def test_returns_n_items_sorted_by_rating_ascending():
    result = autotasks.pick_problems(POOL, [], ["dp"], 1000, 1300, 3, seed=7)
    assert len(result) == 3
    ratings = [p["rating"] for p in result]
    assert ratings == sorted(ratings)


def test_deterministic_for_same_seed():
    a = autotasks.pick_problems(POOL, [], ["dp"], 1000, 1300, 2, seed=42)
    b = autotasks.pick_problems(POOL, [], ["dp"], 1000, 1300, 2, seed=42)
    assert keys(a) == keys(b)


def test_deterministic_regardless_of_input_order():
    shuffled_pool = list(reversed(POOL))
    a = autotasks.pick_problems(POOL, [], ["dp"], 1000, 1300, 2, seed=42)
    b = autotasks.pick_problems(shuffled_pool, [], ["dp"], 1000, 1300, 2, seed=42)
    assert keys(a) == keys(b)


def test_seed_drives_the_selection():
    # Many candidates: at least two seeds should give different picks.
    pool = [prob(f"K{i}", 1000 + i, ("dp",)) for i in range(30)]
    picks = {tuple(keys(autotasks.pick_problems(pool, [], ["dp"], 1000, 1100, 3, seed=s))) for s in range(10)}
    assert len(picks) > 1


def test_widening_by_100_finds_nearby_problem():
    # Band 1000-1200 has only 1A(1000); widening to 900-1300 adds 1250.
    pool = [prob("W1", 1000, ("dp",)), prob("W2", 1250, ("dp",))]
    result = autotasks.pick_problems(pool, [], ["dp"], 1000, 1200, 2, seed=1)
    assert keys(result) == ["W1", "W2"]


def test_widening_is_capped_at_three_steps():
    # Band 1000-1200. Widened to 1500 (3 steps) includes 1500; 1501 is never reached.
    pool = [prob("E1", 1000, ("dp",)), prob("E2", 1500, ("dp",)), prob("E3", 1501, ("dp",))]
    result = autotasks.pick_problems(pool, [], ["dp"], 1000, 1200, 3, seed=1)
    assert set(keys(result)) == {"E1", "E2"}


def test_widening_reaches_exactly_three_steps_edge():
    pool = [prob("F1", 1500, ("dp",))]
    result = autotasks.pick_problems(pool, [], ["dp"], 1000, 1200, 1, seed=1)
    assert keys(result) == ["F1"]


def test_returns_fewer_than_n_when_pool_exhausted():
    pool = [prob("G1", 1000, ("dp",))]
    result = autotasks.pick_problems(pool, [], ["dp"], 1000, 1200, 5, seed=1)
    assert keys(result) == ["G1"]


def test_no_candidates_returns_empty():
    assert autotasks.pick_problems(POOL, [], ["flows"], 1000, 1200, 2, seed=1) == []


def test_no_tags_returns_empty():
    assert autotasks.pick_problems(POOL, [], [], 1000, 1200, 2, seed=1) == []


def test_widening_counts_all_candidates_not_only_picked():
    # Two candidates inside the band already satisfy n=2 -> no widening needed.
    pool = [prob("H1", 1000, ("dp",)), prob("H2", 1100, ("dp",)), prob("H3", 1400, ("dp",))]
    result = autotasks.pick_problems(pool, [], ["dp"], 1000, 1200, 2, seed=1)
    assert set(keys(result)) == {"H1", "H2"}


def test_input_problem_dicts_are_not_mutated():
    before = [dict(p) for p in POOL]
    autotasks.pick_problems(POOL, [], ["dp"], 1000, 1200, 2, seed=1)
    assert POOL == before
