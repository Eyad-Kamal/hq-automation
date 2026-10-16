"""A renamed title column (Problems "Problem" -> "Problem name") must not break syncing.

The Repo finds each database's live title property from its schema and translates
between the live name (what Notion sees) and the configured name (what the code and
cache use). The configured names in schema.py are the fallback.
"""

from __future__ import annotations

import logging

import pytest

from hq import notion as nt
from hq import schema as S
from hq.jobs import imports
from hq.notion import NotionError
from hq.repo import Repo, get_text, get_title
from tests.fakes import build_world, seed_eyad_solves

LIVE = "Problem name"


def _renamed_world():
    w = build_world()
    w.notion.title_props[w.ids[S.DB_PROBLEMS]] = LIVE
    seed_eyad_solves(w)
    return w


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "title property" in r.getMessage()]


def test_import_creates_problem_rows_under_the_live_title_key(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="hq.repo")
    w = _renamed_world()

    assert imports.run(w.context()).result == "OK"

    rows = w.notion.rows(S.DB_PROBLEMS)
    assert len(rows) == 3
    for row in rows:
        assert LIVE in row["properties"], "the stored row carries the live title key"
        assert S.P_Problems.TITLE not in row["properties"], "the configured name is never sent to Notion"
    assert sorted(get_title_live(r) for r in rows) == sorted(["Permutation Swap", "Trust Nobody", "Dp Task"])
    assert len(_warnings(caplog)) == 1, "one warning for the Problems database"


def test_rerun_after_rename_writes_nothing_new() -> None:
    w = _renamed_world()
    imports.run(w.context())
    w.notion.reset_writes()

    imports.run(w.context())

    assert not [c for c in w.notion.creates if c[0] == w.ids[S.DB_PROBLEMS]]
    assert not [u for u in w.notion.updates if u[0] in {r["id"] for r in w.notion.rows(S.DB_PROBLEMS)}]


def test_rows_read_back_under_the_configured_name() -> None:
    w = _renamed_world()
    w.notion.seed(S.DB_PROBLEMS, {LIVE: nt.p_title("Existing problem"), S.P_Problems.PROBLEM_KEY: nt.p_text("CF-1")})

    repo = Repo(w.notion, w.ids)
    rows = repo.rows(S.DB_PROBLEMS)

    assert get_title(rows[0], S.P_Problems.TITLE) == "Existing problem"
    found = repo.find(S.DB_PROBLEMS, S.P_Problems.TITLE, "Existing problem")
    assert found is not None and found["id"] == rows[0]["id"]


def test_update_and_create_send_the_live_key(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="hq.repo")
    w = _renamed_world()
    page_id = w.notion.seed(S.DB_PROBLEMS, {LIVE: nt.p_title("Old name")})
    repo = Repo(w.notion, w.ids)
    page = repo.find(S.DB_PROBLEMS, S.P_Problems.TITLE, "Old name")
    assert page is not None and page["id"] == page_id

    repo.sync(S.DB_PROBLEMS, page, {S.P_Problems.TITLE: nt.p_title("New name")})
    created = repo.create(S.DB_PROBLEMS, {S.P_Problems.TITLE: nt.p_title("Made now")})

    assert w.notion.page(page_id)["properties"][LIVE] == nt.p_title("New name")
    assert w.notion.page(created["id"])["properties"][LIVE] == nt.p_title("Made now")
    assert len(_warnings(caplog)) == 1


def test_normal_schema_is_unchanged_and_logs_nothing(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="hq.repo")
    w = build_world()
    seed_eyad_solves(w)

    assert imports.run(w.context()).result == "OK"

    rows = w.notion.rows(S.DB_PROBLEMS)
    assert len(rows) == 3
    assert all(S.P_Problems.TITLE in r["properties"] for r in rows)
    assert _warnings(caplog) == []


def test_unreadable_schema_falls_back_to_the_configured_name(caplog, monkeypatch) -> None:
    caplog.set_level(logging.WARNING, logger="hq.repo")
    w = build_world()
    seed_eyad_solves(w)

    def broken(ds_id: str):
        raise NotionError("GET /data_sources -> HTTP 503 (injected)", status=503)

    monkeypatch.setattr(w.notion, "retrieve_data_source", broken)
    assert imports.run(w.context()).result == "OK"

    assert all(S.P_Problems.TITLE in r["properties"] for r in w.notion.rows(S.DB_PROBLEMS))
    assert any("could not read the Problems schema" in m for m in caplog.messages)
    assert not [m for m in _warnings(caplog) if "using the live name" in m], "no live-name warning when the schema cannot be read"


def get_title_live(page: dict) -> str:
    return nt.r_title(page["properties"][LIVE])
