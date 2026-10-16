"""Form bridge: the "Log a problem" form's helper columns are copied into the real Problems columns.

Each rule fills only an empty target. A helper column missing from the schema is skipped.
The bridge runs before the Who/Team fill and the member grouping, so a form row is scored in
the same run it is bridged in.
"""

from __future__ import annotations

from datetime import timedelta

from hq import notion as nt
from hq import schema as S
from hq.jobs import imports, onboard, recompute
from hq.repo import get_number, get_relation, get_select, get_status, get_text
from tests.fakes import NOW, build_world, seed_eyad_solves

HELPERS = (S.P_Problems.FORM_WHO, S.P_Problems.FORM_HOW, S.P_Problems.FORM_RATING, S.P_Problems.FORM_DIFFICULTY)
UNMATCHED = "form rows without a matching member: 1 (fill Members → Person)"


def _world(*, helpers: tuple[str, ...] = HELPERS):
    w = build_world()
    seed_eyad_solves(w)
    problems = w.ids[S.DB_PROBLEMS]
    w.notion.declared.setdefault(problems, set()).update(helpers)
    w.notion.reset_writes()
    return w


def _form_row(w, key: str, **answers) -> str:
    """A form row: a Problems row with Problem Key, Solved On and the helper answers given."""
    props = {
        S.P_Problems.TITLE: nt.p_title(f"Form {key}"),
        S.P_Problems.PROBLEM_KEY: nt.p_text(key),
        S.P_Problems.SOLVED_ON: nt.p_date(NOW - timedelta(days=1)),
    }
    props.update(answers)
    return w.notion.seed(S.DB_PROBLEMS, props)


def _who(user: str = "user-eyad"):
    return nt.p_people([user])


def _how(*options: str):
    return nt.p_multi(list(options))


def _row(w, page_id: str) -> dict:
    return w.notion.page(page_id)


def _run(w) -> object:
    return recompute.run(w.context())


def test_person_fills_member_and_who_and_team_in_the_same_run() -> None:
    w = _world()
    row = _form_row(w, "form-person", **{S.P_Problems.FORM_WHO: _who()})
    result = _run(w)

    page = _row(w, row)
    assert get_relation(page, S.P_Problems.MEMBER) == [w.eyad]
    assert get_select(page, S.P_Problems.WHO) == "Eyad", "Who is set from the member bridged in this run"
    assert get_select(page, S.P_Problems.TEAM) == "Team Alpha"
    assert "form rows bridged: 1" in result.details


def test_bridged_form_row_is_scored_in_the_same_run() -> None:
    w = _world()
    onboard.run(w.context())
    imports.run(w.context())
    recompute.run(w.context())
    before = get_number(w.notion.page(w.eyad), S.P_Members.PROBLEMS_TOTAL)
    before_points = get_number(w.notion.page(w.eyad), S.P_Members.TOTAL_POINTS)

    _form_row(w, "form-scored", **{S.P_Problems.FORM_WHO: _who(), S.P_Problems.FORM_RATING: nt.p_number(1500)})
    recompute.run(w.context())

    member = w.notion.page(w.eyad)
    assert get_number(member, S.P_Members.PROBLEMS_TOTAL) == before + 1, "the bridged row counts in the same run"
    assert get_number(member, S.P_Members.TOTAL_POINTS) > before_points


def test_mode_option_is_normalized_ignoring_spaces_around_slash() -> None:
    w = _world()
    row = _form_row(w, "form-mode", **{S.P_Problems.FORM_HOW: _how("With Editorial / Hint")})
    _run(w)
    assert get_select(_row(w, row), S.P_Problems.MODE) == "With Editorial/Hint"


def test_mode_match_is_case_insensitive_and_uses_the_first_option() -> None:
    w = _world()
    row = _form_row(w, "form-mode-first", **{S.P_Problems.FORM_HOW: _how("  live   CONTEST ", "Virtual")})
    _run(w)
    assert get_select(_row(w, row), S.P_Problems.MODE) == "Live Contest"


def test_unknown_mode_option_is_skipped() -> None:
    w = _world()
    row = _form_row(w, "form-mode-unknown", **{S.P_Problems.FORM_HOW: _how("Blitz")})
    _run(w)
    assert get_select(_row(w, row), S.P_Problems.MODE) is None


def test_rating_is_copied_from_rating_1() -> None:
    w = _world()
    row = _form_row(w, "form-rating", **{S.P_Problems.FORM_RATING: nt.p_number(1450)})
    _run(w)
    assert get_number(_row(w, row), S.P_Problems.RATING) == 1450


def test_difficulty_text_fills_the_select_when_no_rating() -> None:
    w = _world()
    row = _form_row(w, "form-diff", **{S.P_Problems.FORM_DIFFICULTY: nt.p_text("  medium ")})
    _run(w)
    page = _row(w, row)
    assert get_select(page, S.P_Problems.DIFFICULTY) == "Medium"
    assert get_number(page, S.P_Problems.RATING) is None


def test_numeric_difficulty_text_becomes_the_rating() -> None:
    w = _world()
    row = _form_row(w, "form-diff-number", **{S.P_Problems.FORM_DIFFICULTY: nt.p_text("1500")})
    _run(w)
    page = _row(w, row)
    assert get_number(page, S.P_Problems.RATING) == 1500
    assert get_select(page, S.P_Problems.DIFFICULTY) is None


def test_difficulty_text_is_ignored_when_rating_is_already_set() -> None:
    w = _world()
    row = _form_row(
        w,
        "form-diff-rating",
        **{S.P_Problems.FORM_RATING: nt.p_number(1200), S.P_Problems.FORM_DIFFICULTY: nt.p_text("Hard")},
    )
    _run(w)
    page = _row(w, row)
    assert get_number(page, S.P_Problems.RATING) == 1200
    assert get_select(page, S.P_Problems.DIFFICULTY) is None, "Rating (1) wins; the text is not used"


def test_status_becomes_solved_on_form_rows() -> None:
    w = _world()
    row = _form_row(w, "form-status", **{S.P_Problems.FORM_RATING: nt.p_number(900)})
    _run(w)
    assert get_status(_row(w, row), S.P_Problems.STATUS) == "Solved"


def test_no_rule_overwrites_a_filled_target() -> None:
    w = _world()
    row = _form_row(
        w,
        "form-filled",
        **{
            S.P_Problems.MEMBER: nt.p_relation([w.sara]),
            S.P_Problems.MODE: nt.p_select("Virtual"),
            S.P_Problems.RATING: nt.p_number(1000),
            S.P_Problems.DIFFICULTY: nt.p_select("Hard"),
            S.P_Problems.STATUS: nt.p_status("Trying"),
            S.P_Problems.FORM_WHO: _who(),
            S.P_Problems.FORM_HOW: _how("Upsolve"),
            S.P_Problems.FORM_RATING: nt.p_number(1700),
            S.P_Problems.FORM_DIFFICULTY: nt.p_text("Easy"),
        },
    )
    w.notion.reset_writes()
    result = _run(w)

    page = _row(w, row)
    assert get_relation(page, S.P_Problems.MEMBER) == [w.sara]
    assert get_select(page, S.P_Problems.MODE) == "Virtual"
    assert get_number(page, S.P_Problems.RATING) == 1000
    assert get_select(page, S.P_Problems.DIFFICULTY) == "Hard"
    assert get_status(page, S.P_Problems.STATUS) == "Trying"
    assert not [u for u in w.notion.updates if u[0] == row and any(k in u[1] for k in (S.P_Problems.MEMBER, S.P_Problems.MODE, S.P_Problems.RATING, S.P_Problems.STATUS))]
    assert "form rows bridged: 0" in result.details


def test_missing_helper_columns_are_skipped_silently() -> None:
    w = _world(helpers=(S.P_Problems.FORM_RATING,))  # only "Rating (1)" exists in the schema
    row = _form_row(w, "form-partial", **{S.P_Problems.FORM_RATING: nt.p_number(1100)})
    result = _run(w)

    page = _row(w, row)
    assert get_number(page, S.P_Problems.RATING) == 1100, "the existing helper still bridges"
    assert get_select(page, S.P_Problems.MODE) is None, "a missing helper is not read"
    assert get_status(page, S.P_Problems.STATUS) == "Solved", "Status needs no helper"
    assert "form rows bridged: 1" in result.details
    assert UNMATCHED not in result.details, "no Who helper means no unmatched-member count"


def test_no_helper_columns_bridge_nothing() -> None:
    w = _world(helpers=())
    row = _form_row(w, "form-none")
    result = _run(w)
    page = _row(w, row)
    assert get_status(page, S.P_Problems.STATUS) is None, "a row with no form answers is not a form row"
    assert "form rows bridged: 0" in result.details


def test_unmatched_user_leaves_member_empty_and_is_reported() -> None:
    w = _world()
    row = _form_row(w, "form-unmatched", **{S.P_Problems.FORM_WHO: _who("user-stranger"), S.P_Problems.FORM_RATING: nt.p_number(1000)})
    result = _run(w)

    page = _row(w, row)
    assert get_relation(page, S.P_Problems.MEMBER) == []
    assert get_number(page, S.P_Problems.RATING) == 1000, "other rules still bridge"
    assert UNMATCHED in result.details


def test_person_match_ignores_dashes_and_case() -> None:
    w = _world()
    dash = w.notion.seed(
        S.DB_MEMBERS,
        {
            S.P_Members.TITLE: nt.p_title("Dash"),
            S.P_Members.PERSON: nt.p_people(["AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"]),
            S.P_Members.ACTIVE: nt.p_checkbox(True),
        },
    )
    row = _form_row(w, "form-dash", **{S.P_Problems.FORM_WHO: _who("aaaaaaaa111122223333bbbbbbbbbbbb")})
    _run(w)
    assert get_relation(_row(w, row), S.P_Problems.MEMBER) == [dash]


def test_rerun_writes_nothing_for_bridged_rows() -> None:
    w = _world()
    row = _form_row(
        w,
        "form-idem",
        **{
            S.P_Problems.FORM_WHO: _who(),
            S.P_Problems.FORM_HOW: _how("Live Contest"),
            S.P_Problems.FORM_RATING: nt.p_number(1300),
            S.P_Problems.FORM_DIFFICULTY: nt.p_text("Hard"),
        },
    )
    first = _run(w)
    assert "form rows bridged: 1" in first.details
    w.notion.reset_writes()

    second = _run(w)

    assert second.items == 0
    assert "form rows bridged: 0" in second.details
    assert not [u for u in w.notion.updates if u[0] == row]
    page = _row(w, row)
    assert get_select(page, S.P_Problems.MODE) == "Live Contest"
    assert get_select(page, S.P_Problems.DIFFICULTY) is None, "Rating(1) is set, so the text does not fill Difficulty"
    assert get_text(page, S.P_Problems.PROBLEM_KEY) == "form-idem"
