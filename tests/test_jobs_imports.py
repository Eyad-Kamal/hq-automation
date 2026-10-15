"""Jobs imports: upsert idempotency, fill-only updates, upsolve detection, contest entries."""

from __future__ import annotations

from datetime import timedelta

from hq import notion as nt
from hq import schema as S
from hq.jobs import imports
from hq.repo import get_checkbox, get_number, get_relation, get_select, get_status, get_text
from tests.fakes import NOW, build_world, seed_eyad_solves


def _problem(w, key: str):
    matches = [
        p
        for p in w.notion.rows(S.DB_PROBLEMS)
        if get_text(p, S.P_Problems.PROBLEM_KEY) == key and get_relation(p, S.P_Problems.MEMBER) == [w.eyad]
    ]
    assert len(matches) == 1, f"expected exactly one Problems row for {key}"
    return matches[0]


def _setup(*, seed_contest: bool = False):
    w = build_world()
    seed_eyad_solves(w, seed_contest=seed_contest)
    return w


def test_first_import_creates_problems_with_modes() -> None:
    w = _setup()
    assert imports.run(w.context()).result == "OK"

    assert len(w.notion.rows(S.DB_PROBLEMS)) == 3
    assert get_select(_problem(w, "CF-1985C"), S.P_Problems.MODE) == "Live Contest"
    assert get_select(_problem(w, "CF-1999B"), S.P_Problems.MODE) == "Practice"
    assert get_select(_problem(w, "CF-1990A"), S.P_Problems.MODE) == "Upsolve", "practice solve of a contest the member did live"
    assert get_status(_problem(w, "CF-1985C"), S.P_Problems.STATUS) == "Solved"
    assert get_number(_problem(w, "CF-1985C"), S.P_Problems.RATING_AT_SOLVE) == 1150, "rating just before the solve"
    assert get_text(_problem(w, "CF-1985C"), S.P_Problems.SOURCE_KEY) == f"CF-1985C:{w.eyad}"


def test_topics_are_mapped_from_cf_tags_and_rating() -> None:
    w = _setup()
    imports.run(w.context())
    assert get_relation(_problem(w, "CF-1999B"), S.P_Problems.TOPICS) == [w.topics["dp"]]
    assert get_relation(_problem(w, "CF-1985C"), S.P_Problems.TOPICS) == [w.topics["greedy"]]
    assert get_relation(_problem(w, "CF-1990A"), S.P_Problems.TOPICS) == [w.topics["strings"]]


def test_contest_entry_and_placeholder_contest_for_live_solves() -> None:
    w = _setup()
    imports.run(w.context())

    entries = w.notion.rows(S.DB_CONTEST_ENTRIES)
    assert len(entries) == 1
    entry = entries[0]
    assert get_relation(entry, S.P_ContestEntries.MEMBER) == [w.eyad]
    assert get_select(entry, S.P_ContestEntries.PARTICIPATION) == "Rated Live"
    assert get_status(entry, S.P_ContestEntries.STATUS) == "Attended"
    assert get_number(entry, S.P_ContestEntries.SOLVED) == 1, "only the live solve counts"
    assert len(get_relation(entry, S.P_ContestEntries.PROBLEMS)) == 1

    contest = w.notion.page(get_relation(entry, S.P_ContestEntries.CONTEST)[0])
    assert get_text(contest, S.P_Contests.EXTERNAL_ID) == "CF-1985"
    assert get_select(contest, S.P_Contests.PHASE) == "Finished"
    assert get_select(contest, S.P_Contests.PLATFORM) == "Codeforces"


def test_rating_history_and_member_ratings_are_written() -> None:
    w = _setup()
    imports.run(w.context())
    rows = w.notion.rows(S.DB_RATING_HISTORY)
    assert len(rows) == 1
    assert get_text(rows[0], S.P_RatingHistory.SOURCE_KEY) == f"codeforces:Codeforces Round 1985 (Div. 2):2026-10-12:{w.eyad}"
    assert get_number(rows[0], S.P_RatingHistory.NEW) == 1150

    member = w.notion.page(w.eyad)
    assert get_number(member, S.P_Members.CF_RATING) == 1150
    assert get_number(member, S.P_Members.CF_MAX_RATING) == 1150
    assert member["properties"][S.P_Members.LAST_SYNC]["date"]["start"] == NOW.date().isoformat()


def test_rerun_is_idempotent_and_writes_no_problem_rows() -> None:
    w = _setup()
    imports.run(w.context())
    problem_ids = {p["id"] for p in w.notion.rows(S.DB_PROBLEMS)}
    counts = {db: len(w.notion.rows(db)) for db in (S.DB_PROBLEMS, S.DB_CONTEST_ENTRIES, S.DB_RATING_HISTORY, S.DB_CONTESTS)}

    w.notion.reset_writes()
    assert imports.run(w.context()).result == "OK"
    for db, before in counts.items():
        assert len(w.notion.rows(db)) == before, f"{db} gained rows on a rerun"
    assert not [pid for pid, _ in w.notion.updates if pid in problem_ids], "unchanged problems are not rewritten"
    assert not any(ds == w.notion.ids[S.DB_PROBLEMS] for ds, _ in w.notion.creates)


def test_user_edited_fields_are_never_overwritten() -> None:
    w = _setup()
    imports.run(w.context())
    problem = _problem(w, "CF-1999B")
    w.notion.update_page(
        problem["id"],
        {
            S.P_Problems.NOTES: nt.p_text("my own notes"),
            S.P_Problems.TIME_MIN: nt.p_number(25),
            S.P_Problems.MODE: nt.p_select("With Editorial/Hint"),
        },
    )
    # The source now reports a different rating for the same solve.
    seed_eyad_solves(w, rating_1999b=1400)
    imports.run(w.context(now=NOW + timedelta(minutes=30)))

    problem = _problem(w, "CF-1999B")
    assert get_number(problem, S.P_Problems.RATING) == 1300, "a filled rating is not overwritten"
    assert get_text(problem, S.P_Problems.NOTES) == "my own notes"
    assert get_number(problem, S.P_Problems.TIME_MIN) == 25
    assert get_select(problem, S.P_Problems.MODE) == "With Editorial/Hint", "a user-changed Mode is kept"


def test_solved_status_is_restored_when_the_source_has_the_solve() -> None:
    w = _setup()
    imports.run(w.context())
    problem = _problem(w, "CF-1999B")
    w.notion.update_page(problem["id"], {S.P_Problems.STATUS: nt.p_status("Stuck")})
    imports.run(w.context(now=NOW + timedelta(minutes=30)))
    assert get_status(_problem(w, "CF-1999B"), S.P_Problems.STATUS) == "Solved"


def test_blank_fields_are_filled_on_later_runs() -> None:
    w = _setup()
    imports.run(w.context())
    problem = _problem(w, "CF-1999B")
    w.notion.update_page(problem["id"], {S.P_Problems.DIFFICULTY: nt.p_select(None), S.P_Problems.TOPICS: nt.p_relation([])})
    imports.run(w.context(now=NOW + timedelta(minutes=30)))
    problem = _problem(w, "CF-1999B")
    assert get_relation(problem, S.P_Problems.TOPICS) == [w.topics["dp"]], "an empty Topics relation is filled"


def test_attended_contest_task_becomes_attended() -> None:
    w = _setup(seed_contest=True)
    contest_row = w.notion.rows(S.DB_CONTESTS)[0]
    w.notion.seed(
        S.DB_TASKS,
        {
            S.P_Tasks.TITLE: nt.p_title("🏆 Codeforces Round 1985 (Div. 2)"),
            S.P_Tasks.ASSIGNEE: nt.p_relation([w.eyad]),
            S.P_Tasks.TYPE: nt.p_select("Contest"),
            S.P_Tasks.STATUS: nt.p_status("Planned"),
            S.P_Tasks.CONTEST: nt.p_relation([contest_row["id"]]),
        },
    )
    imports.run(w.context())
    task = w.notion.rows(S.DB_TASKS)[0]
    assert get_status(task, S.P_Tasks.STATUS) == "Attended"
    assert len(get_relation(task, S.P_Tasks.CONTEST_ENTRY)) == 1
    entry = w.notion.rows(S.DB_CONTEST_ENTRIES)[0]
    assert get_relation(entry, S.P_ContestEntries.CONTEST) == [contest_row["id"]], "an existing contest row is reused"
    assert get_checkbox(contest_row, S.P_Contests.TASKS_CREATED) is True


def test_one_failing_member_does_not_stop_the_others() -> None:
    w = _setup()
    real = w.sources.cf_solves

    def flaky(handle, since, **kwargs):
        if handle == "eyad_cf":
            raise RuntimeError("source exploded")
        return real(handle, since, **kwargs)

    w.sources.cf_solves = flaky
    result = imports.run(w.context())
    assert result.result == "Warning"
    assert "Eyad" in result.details
    sara = w.notion.page(w.sara)  # Sara has no CF solves but must still be synced
    assert sara["properties"][S.P_Members.LAST_SYNC]["date"] is not None, "the other member was still synced"


def test_import_due_check_uses_the_25_minute_guard() -> None:
    w = _setup()
    assert imports.imports_due(w.context())
    imports.run(w.context())
    assert not imports.imports_due(w.context(now=NOW + timedelta(minutes=10)))
    assert imports.imports_due(w.context(now=NOW + timedelta(minutes=26)))
