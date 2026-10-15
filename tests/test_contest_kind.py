import pytest

from hq.logic.contest_kind import classify, subscription_matches

# Exact Contests."Kind" options from build/schema-spec.md.
KIND_OPTIONS = {
    "CF Div. 1", "CF Div. 2", "CF Div. 1+2", "CF Div. 3", "CF Div. 4",
    "CF Educational", "CF Other",
    "AtCoder ABC", "AtCoder ARC", "AtCoder AGC", "AtCoder Other",
    "LC Weekly", "LC Biweekly",
    "ICPC Mirror", "Gym", "Other",
}


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Codeforces Round 990 (Div. 1)", "CF Div. 1"),
        ("Codeforces Round 990 (Div. 2)", "CF Div. 2"),
        ("Codeforces Round 990 (Div. 1 + Div. 2)", "CF Div. 1+2"),
        ("Pinely Round 4 (Div. 1 + Div. 2)", "CF Div. 1+2"),
        ("Codeforces Round 1000 (Div. 3)", "CF Div. 3"),
        ("Codeforces Round 1000 (Div. 4)", "CF Div. 4"),
        ("Educational Codeforces Round 170 (Rated for Div. 2)", "CF Educational"),
        ("Codeforces Global Round 27", "CF Other"),
        ("Good Bye 2025", "CF Other"),
    ],
)
def test_codeforces_names(name, expected):
    assert classify("Codeforces", name) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("AtCoder Beginner Contest 380", "AtCoder ABC"),
        ("ABC380", "AtCoder ABC"),
        ("AtCoder Regular Contest 180", "AtCoder ARC"),
        ("ARC180", "AtCoder ARC"),
        ("AtCoder Grand Contest 065", "AtCoder AGC"),
        ("AGC065", "AtCoder AGC"),
        ("AtCoder Japan Open 2024", "AtCoder Other"),
    ],
)
def test_atcoder_names(name, expected):
    assert classify("AtCoder", name) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Weekly Contest 420", "LC Weekly"),
        ("Biweekly Contest 140", "LC Biweekly"),
        ("Some LeetCode Event", "Other"),
    ],
)
def test_leetcode_names(name, expected):
    assert classify("LeetCode", name) == expected


def test_gym_platform_is_gym():
    assert classify("CF Gym", "Some Gym Contest") == "Gym"


def test_icpc_name_is_icpc_mirror():
    name = "2024-2025 ICPC, NERC, Southern and Volga Russian Regional Contest"
    assert classify("Codeforces", name) == "ICPC Mirror"


def test_unknown_platform_is_other():
    assert classify("Vjudge", "Some Contest") == "Other"
    assert classify("CodeChef", "Starters 150") == "Other"


def test_every_kind_option_is_producible():
    samples = [
        ("Codeforces", "R (Div. 1)"),
        ("Codeforces", "R (Div. 2)"),
        ("Codeforces", "R (Div. 1 + Div. 2)"),
        ("Codeforces", "R (Div. 3)"),
        ("Codeforces", "R (Div. 4)"),
        ("Codeforces", "Educational Codeforces Round 170 (Rated for Div. 2)"),
        ("Codeforces", "Global Round 27"),
        ("AtCoder", "AtCoder Beginner Contest 380"),
        ("AtCoder", "AtCoder Regular Contest 180"),
        ("AtCoder", "AtCoder Grand Contest 065"),
        ("AtCoder", "AtCoder Japan Open 2024"),
        ("LeetCode", "Weekly Contest 420"),
        ("LeetCode", "Biweekly Contest 140"),
        ("CF Gym", "Gym"),
        ("Codeforces", "ICPC regional mirror"),
        ("Vjudge", "Other"),
    ]
    produced = {classify(p, n) for p, n in samples}
    assert produced == KIND_OPTIONS


def test_every_output_is_a_valid_kind_option():
    for platform in ["Codeforces", "AtCoder", "LeetCode", "CF Gym", "Vjudge", ""]:
        for name in ["", "Div. 2", "ABC", "Weekly Contest", "Biweekly Contest", "ICPC"]:
            assert classify(platform, name) in KIND_OPTIONS


def test_subscription_matches_exact_name():
    assert subscription_matches("CF Div. 2", ["CF Div. 2", "AtCoder ABC"]) is True


def test_subscription_no_match():
    assert subscription_matches("CF Div. 3", ["CF Div. 2"]) is False


def test_subscription_other_and_atcoder_other_never_match():
    assert subscription_matches("Other", ["Other"]) is False
    assert subscription_matches("AtCoder Other", ["AtCoder Other"]) is False


def test_subscription_empty_list():
    assert subscription_matches("CF Div. 2", []) is False
