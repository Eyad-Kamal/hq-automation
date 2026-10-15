"""Notion client, retries, property builders/readers and id loading (offline)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
import requests

from hq.notion import (
    NOTION_VERSION,
    Notion,
    NotionError,
    chunk_text,
    load_data_source_ids,
    mention_user,
    p_checkbox,
    p_date,
    p_email,
    p_multi,
    p_number,
    p_people,
    p_relation,
    p_select,
    p_status,
    p_text,
    p_title,
    p_url,
    r_checkbox,
    r_date,
    r_formula,
    r_multi,
    r_number,
    r_people,
    r_relation,
    r_select,
    r_status,
    r_text,
    r_title,
    r_url,
    prop,
    require_data_source,
    rich_text,
)
from tests.helpers import FakeResponse, FakeSession

OK_EMPTY = FakeResponse(200, {"object": "list", "results": [], "has_more": False, "next_cursor": None})


def make_client(handler: Any, sleeps: list[float] | None = None) -> tuple[Notion, FakeSession, list[float]]:
    session = FakeSession(handler)
    recorded: list[float] = sleeps if sleeps is not None else []
    client = Notion("secret-test-token", session=session, min_interval=0, sleep=recorded.append)
    return client, session, recorded


# ---------------------------------------------------------------------------
# Client: headers, endpoints and bodies
# ---------------------------------------------------------------------------
def test_headers_carry_token_and_api_version() -> None:
    client, session, _ = make_client(lambda *_: OK_EMPTY)
    client.retrieve_data_source("ds-1")
    headers = session.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer secret-test-token"
    assert headers["Notion-Version"] == NOTION_VERSION == "2025-09-03"


def test_repr_does_not_leak_token() -> None:
    client, _, _ = make_client(lambda *_: OK_EMPTY)
    assert "secret-test-token" not in repr(client)


def test_empty_token_is_rejected() -> None:
    with pytest.raises(ValueError):
        Notion("")


def test_query_paginates_until_has_more_is_false() -> None:
    pages = [
        FakeResponse(200, {"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"}),
        FakeResponse(200, {"results": [{"id": "b"}], "has_more": False, "next_cursor": None}),
    ]
    client, session, sleeps = make_client(lambda *_: pages.pop(0))
    rows = client.query("ds-1")
    assert [r["id"] for r in rows] == ["a", "b"]
    assert session.urls() == [
        "https://api.notion.com/v1/data_sources/ds-1/query",
        "https://api.notion.com/v1/data_sources/ds-1/query",
    ]
    assert session.calls[0]["json"] == {"page_size": 100}
    assert session.calls[1]["json"] == {"page_size": 100, "start_cursor": "c1"}
    assert sleeps == []


def test_query_passes_filter_and_sorts() -> None:
    client, session, _ = make_client(lambda *_: OK_EMPTY)
    flt = {"property": "Who", "select": {"equals": "Eyad"}}
    sorts = [{"property": "Date", "direction": "descending"}]
    client.query("ds-1", filter=flt, sorts=sorts)
    body = session.calls[0]["json"]
    assert body["filter"] == flt
    assert body["sorts"] == sorts


def test_query_returns_empty_list_when_no_rows() -> None:
    client, _, _ = make_client(lambda *_: OK_EMPTY)
    assert client.query("ds-1") == []


def test_create_page_uses_data_source_parent() -> None:
    client, session, _ = make_client(lambda *_: FakeResponse(200, {"id": "page-1"}))
    result = client.create_page("ds-9", {"Name": p_title("x")})
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.notion.com/v1/pages"
    assert call["json"]["parent"] == {"type": "data_source_id", "data_source_id": "ds-9"}
    assert call["json"]["properties"] == {"Name": {"title": [{"type": "text", "text": {"content": "x"}}]}}
    assert "children" not in call["json"]
    assert result == {"id": "page-1"}


def test_create_page_includes_children_when_given() -> None:
    client, session, _ = make_client(lambda *_: FakeResponse(200, {"id": "p"}))
    client.create_page("ds-9", {}, children=[{"object": "block"}])
    assert session.calls[0]["json"]["children"] == [{"object": "block"}]


def test_update_page_patches_properties() -> None:
    client, session, _ = make_client(lambda *_: FakeResponse(200, {"id": "p1"}))
    client.update_page("p1", {"Points": p_number(3)})
    call = session.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == "https://api.notion.com/v1/pages/p1"
    assert call["json"] == {"properties": {"Points": {"number": 3}}}


def test_get_page_and_retrieve_data_source_use_get() -> None:
    client, session, _ = make_client(lambda *_: FakeResponse(200, {"object": "page"}))
    client.get_page("p1")
    client.retrieve_data_source("ds-1")
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == "https://api.notion.com/v1/pages/p1"
    assert session.calls[1]["url"] == "https://api.notion.com/v1/data_sources/ds-1"


def test_append_blocks_chunks_children_by_100() -> None:
    client, session, _ = make_client(lambda *_: FakeResponse(200, {"results": []}))
    children = [{"object": "block", "n": i} for i in range(250)]
    client.append_blocks("page-1", children)
    sizes = [len(c["json"]["children"]) for c in session.calls]
    assert sizes == [100, 100, 50]
    assert all(c["method"] == "PATCH" for c in session.calls)
    assert all(c["url"] == "https://api.notion.com/v1/blocks/page-1/children" for c in session.calls)


def test_append_blocks_with_nothing_makes_no_call() -> None:
    client, session, _ = make_client(lambda *_: OK_EMPTY)
    assert client.append_blocks("page-1", []) == {}
    assert session.calls == []


def test_create_comment_with_user_mention() -> None:
    client, session, _ = make_client(lambda *_: FakeResponse(200, {"id": "c1"}))
    rich = [mention_user("user-123"), *rich_text(" check this")]
    client.create_comment("page-1", rich)
    body = session.calls[0]["json"]
    assert session.calls[0]["url"] == "https://api.notion.com/v1/comments"
    assert body["parent"] == {"page_id": "page-1"}
    assert body["rich_text"][0] == {"type": "mention", "mention": {"type": "user", "user": {"id": "user-123"}}}


# ---------------------------------------------------------------------------
# Client: retries and errors
# ---------------------------------------------------------------------------
def test_429_is_retried_honouring_retry_after() -> None:
    responses = [
        FakeResponse(429, {"object": "error"}, headers={"Retry-After": "3"}),
        FakeResponse(200, {"id": "p"}),
    ]
    client, session, sleeps = make_client(lambda *_: responses.pop(0))
    result = client.get_page("p")
    assert result == {"id": "p"}
    assert len(session.calls) == 2
    assert sleeps == [3.0]


def test_retry_after_is_used_for_529_too() -> None:
    responses = [
        FakeResponse(529, {"object": "error"}, headers={"Retry-After": "7"}),
        FakeResponse(200, {"id": "p"}),
    ]
    client, _, sleeps = make_client(lambda *_: responses.pop(0))
    client.get_page("p")
    assert sleeps == [7.0]


def test_5xx_without_retry_after_uses_exponential_backoff() -> None:
    responses = [FakeResponse(503), FakeResponse(500), FakeResponse(200, {"id": "p"})]
    client, session, sleeps = make_client(lambda *_: responses.pop(0))
    client.get_page("p")
    assert len(session.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_five_tries() -> None:
    client, session, sleeps = make_client(lambda *_: FakeResponse(429, headers={"Retry-After": "0"}))
    with pytest.raises(NotionError, match="failed after 5 tries"):
        client.get_page("p")
    assert len(session.calls) == 5
    assert len(sleeps) == 4  # no sleep after the final failure


def test_network_error_is_retried_then_succeeds() -> None:
    responses: list[Any] = [requests.ConnectionError("reset"), FakeResponse(200, {"id": "p"})]
    client, session, sleeps = make_client(lambda *_: responses.pop(0))
    assert client.get_page("p") == {"id": "p"}
    assert len(session.calls) == 2
    assert sleeps == [1.0]


def test_client_error_is_not_retried() -> None:
    client, session, sleeps = make_client(lambda *_: FakeResponse(400, {"message": "bad property"}, text='{"message": "bad property"}'))
    with pytest.raises(NotionError, match="HTTP 400"):
        client.update_page("p", {})
    assert len(session.calls) == 1
    assert sleeps == []


def test_min_interval_throttles_consecutive_calls() -> None:
    clock_value = [0.0]
    sleeps: list[float] = []
    session = FakeSession(lambda *_: OK_EMPTY)
    client = Notion("t", session=session, min_interval=0.35, sleep=sleeps.append)
    client._limiter._clock = lambda: clock_value[0]  # noqa: SLF001 - test hook
    client.retrieve_data_source("ds-1")
    client.retrieve_data_source("ds-1")
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# Rich text
# ---------------------------------------------------------------------------
def test_chunk_text_respects_limit_and_round_trips() -> None:
    text = "a" * 4500
    chunks = chunk_text(text)
    assert [len(c) for c in chunks] == [2000, 2000, 500]
    assert "".join(chunks) == text


def test_chunk_text_counts_astral_characters_as_two_units() -> None:
    text = "\U0001F600" * 1500  # 1500 code points, 3000 UTF-16 units
    chunks = chunk_text(text)
    assert "".join(chunks) == text
    for chunk in chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 2000
    assert len(chunks) == 2


def test_chunk_text_empty_is_empty() -> None:
    assert chunk_text("") == []
    assert rich_text("") == []


def test_rich_text_items_have_text_content() -> None:
    items = rich_text("hello")
    assert items == [{"type": "text", "text": {"content": "hello"}}]


# ---------------------------------------------------------------------------
# Property builders and readers: round trips
# ---------------------------------------------------------------------------
def as_page(**props: Any) -> dict[str, Any]:
    """Wrap property values as a page the way the Notion API returns them."""
    return {"object": "page", "id": "page-1", "properties": props}


def test_title_round_trip() -> None:
    page = as_page(Name=p_title("Two Sum"))
    assert r_title(prop(page, "Name")) == "Two Sum"


def test_title_reads_api_plain_text_items() -> None:
    api_value = {"type": "title", "title": [{"plain_text": "Hel", "text": {"content": "Hel"}}, {"plain_text": "lo"}]}
    assert r_title(api_value) == "Hello"


def test_text_round_trip_including_long_text() -> None:
    long_text = ("line of notes " * 300).strip()  # > 2000 characters
    value = p_text(long_text)
    assert len(value["rich_text"]) >= 2
    assert all(len(item["text"]["content"]) <= 2000 for item in value["rich_text"])
    assert r_text(value) == long_text


def test_text_empty_and_none() -> None:
    assert r_text(p_text(None)) == ""
    assert r_text(p_text("")) == ""


def test_number_round_trip_and_missing() -> None:
    assert r_number(p_number(3.5)) == 3.5
    assert r_number(p_number(0)) == 0
    assert r_number(p_number(None)) is None
    assert r_number(None) is None


def test_select_round_trip() -> None:
    assert r_select(p_select("Blue")) == "Blue"
    assert r_select(p_select(None)) is None
    assert r_select(None) is None


def test_multi_select_round_trip() -> None:
    assert r_multi(p_multi(["dp", "graphs"])) == ["dp", "graphs"]
    assert r_multi(p_multi([])) == []


def test_status_round_trip() -> None:
    assert r_status(p_status("Solved")) == "Solved"
    assert r_status(p_status(None)) is None


def test_checkbox_round_trip() -> None:
    assert r_checkbox(p_checkbox(True)) is True
    assert r_checkbox(p_checkbox(False)) is False
    assert r_checkbox(None) is False


def test_url_and_email_builders() -> None:
    assert r_url(p_url("https://codeforces.com")) == "https://codeforces.com"
    assert r_url(p_url(None)) is None
    assert p_email("a@b.co") == {"email": "a@b.co"}
    assert p_email(None) == {"email": None}


def test_relation_and_people_round_trip() -> None:
    assert r_relation(p_relation(["r1", "r2"])) == ["r1", "r2"]
    assert r_relation(p_relation([])) == []
    assert r_people(p_people(["u1"])) == ["u1"]
    assert p_people(["u1"]) == {"people": [{"object": "user", "id": "u1"}]}


def test_date_aware_datetime_round_trip_in_utc() -> None:
    cairo = timezone(timedelta(hours=2))
    original = datetime(2026, 10, 18, 23, 30, 0, tzinfo=cairo)
    value = p_date(original)
    assert value["date"]["end"] is None
    assert value["date"]["start"] == "2026-10-18T21:30:00+00:00"
    restored = r_date(value)
    assert isinstance(restored, datetime)
    assert restored == original
    assert restored.utcoffset() == timedelta(0)


def test_date_naive_datetime_is_taken_as_utc() -> None:
    value = p_date(datetime(2026, 10, 18, 12, 0, 0))
    assert value["date"]["start"] == "2026-10-18T12:00:00+00:00"


def test_date_only_round_trip() -> None:
    value = p_date(date(2026, 10, 18), with_time=False)
    assert value["date"]["start"] == "2026-10-18"
    assert r_date(value) == date(2026, 10, 18)


def test_date_with_time_false_uses_utc_calendar_day() -> None:
    late = datetime(2026, 10, 18, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert p_date(late, with_time=False)["date"]["start"] == "2026-10-19"


def test_date_none_and_empty() -> None:
    assert p_date(None) == {"date": None}
    assert r_date(p_date(None)) is None
    assert r_date(None) is None


def test_formula_readers_by_result_type() -> None:
    assert r_formula({"type": "formula", "formula": {"type": "number", "number": 12.5}}) == 12.5
    assert r_formula({"type": "formula", "formula": {"type": "string", "string": "▲ 3"}}) == "▲ 3"
    assert r_formula({"type": "formula", "formula": {"type": "boolean", "boolean": True}}) is True
    assert r_formula({"type": "formula", "formula": {"type": "date", "date": {"start": "2026-10-18"}}}) == date(2026, 10, 18)
    assert r_formula({"type": "formula", "formula": {"type": "date", "date": None}}) is None
    assert r_formula({"type": "formula", "formula": {"type": "number", "number": None}}) is None
    assert r_formula(None) is None


def test_mention_user_shape() -> None:
    assert mention_user("abc") == {"type": "mention", "mention": {"type": "user", "user": {"id": "abc"}}}


def test_prop_helper_handles_missing_page_and_name() -> None:
    page = as_page(Name=p_title("x"))
    assert prop(page, "Name") == p_title("x")
    assert prop(page, "Missing") is None
    assert prop(None, "Name") is None


# ---------------------------------------------------------------------------
# Data source ids
# ---------------------------------------------------------------------------
def test_ids_from_env_json() -> None:
    env = {"HQ_IDS_JSON": json.dumps({"Members": "ds-m", "Problems": "ds-p"})}
    assert load_data_source_ids(env=env) == {"Members": "ds-m", "Problems": "ds-p"}


def test_ids_from_file_when_env_missing(tmp_path) -> None:
    path = tmp_path / "notion-ids.json"
    path.write_text(json.dumps({"Contests": "ds-c"}), encoding="utf-8")
    assert load_data_source_ids(env={}, path=path) == {"Contests": "ds-c"}


def test_env_takes_precedence_over_file(tmp_path) -> None:
    path = tmp_path / "notion-ids.json"
    path.write_text(json.dumps({"Contests": "from-file"}), encoding="utf-8")
    env = {"HQ_IDS_JSON": json.dumps({"Contests": "from-env"})}
    assert load_data_source_ids(env=env, path=path) == {"Contests": "from-env"}


def test_missing_ids_return_empty_dict(tmp_path) -> None:
    assert load_data_source_ids(env={}, path=tmp_path / "absent.json") == {}


def test_invalid_ids_json_returns_empty_dict() -> None:
    assert load_data_source_ids(env={"HQ_IDS_JSON": "{not json"}) == {}


def test_require_data_source() -> None:
    assert require_data_source({"Members": "ds-m"}, "Members") == "ds-m"
    with pytest.raises(KeyError, match="Problems"):
        require_data_source({}, "Problems")
