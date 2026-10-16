"""Notion REST client (API version 2025-09-03), property builders and readers.

Client: ``Notion`` wraps ``/v1`` endpoints through an injected ``requests.Session``.
Failures on writes and queries raise ``NotionError`` (jobs need to know when a
write did not land). Retries: 429, 529 and 5xx, honouring ``Retry-After``, up
to 5 attempts in total.

Builders (``p_*``) return Notion property *values*. Readers (``r_*``) take the
property *value* object from a page (``page["properties"][name]``) and return
Python values, with ``None`` or empty defaults when the property is missing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

from hq._net import DEFAULT_TIMEOUT, RateLimiter

log = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
VIEWS_VERSION = "2026-03-11"  # the create-view reference's version; the chart config is only documented for it
MAX_TRIES = 5
RICH_TEXT_LIMIT = 2000  # Notion limit per rich-text item
APPEND_CHILDREN_LIMIT = 100  # Notion limit of children per append request
IDS_ENV_VAR = "HQ_IDS_JSON"
IDS_FILE: Path = Path(__file__).resolve().parent.parent / "notion-ids.json"

Json = dict[str, Any]


class NotionError(RuntimeError):
    """A Notion API call failed after retries, or returned a non-retryable error.

    ``status`` is the HTTP status code when the API answered, else None (network errors and
    retries that ran out)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class Notion:
    """Thin Notion REST client. Pass ``session`` in tests to avoid the network."""

    def __init__(
        self,
        token: str,
        *,
        min_interval: float = 0.35,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("Notion token is empty (set NOTION_TOKEN)")
        self._token = token
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._limiter = RateLimiter(min_interval, sleep=sleep)

    def __repr__(self) -> str:  # never leak the token
        return "Notion(token=<redacted>)"

    def _headers(self, version: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": version or NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _retry_wait(self, resp: requests.Response, attempt: int) -> float:
        raw = resp.headers.get("Retry-After") if resp.headers else None
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        return float(min(60, 2**attempt))

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Json | None = None,
        retry_server_errors: bool = True,
        version: str | None = None,
    ) -> Json:
        """Perform one API call with throttling and retries; return decoded JSON.

        Retried: 429 (honouring Retry-After), 529, and, when ``retry_server_errors``
        is true, other 5xx responses and network errors. A 429 whose error code is
        ``public_api_request_blocked`` is never retried.

        ``retry_server_errors=False`` is used for non-idempotent creates: a 5xx or
        a timeout may come after Notion already stored the row, so the caller must
        look before it retries (see ``hq.repo.Repo.create``).
        """
        url = NOTION_API + path
        last_error = "no attempt made"
        for attempt in range(MAX_TRIES):
            self._limiter.wait()
            try:
                resp = self._session.request(
                    method,
                    url,
                    headers=self._headers(version),
                    params=params,
                    json=body,
                    timeout=DEFAULT_TIMEOUT,
                )
            except requests.RequestException as exc:
                if not retry_server_errors:
                    raise NotionError(f"{method} {path} network error (not retried): {exc}") from exc
                last_error = f"network error: {exc}"
                resp = None

            if resp is not None:
                code = resp.status_code
                if 200 <= code < 300:
                    return _decode(resp)
                if code == 429 and _error_code(resp) == BLOCKED_CODE:
                    raise NotionError(f"{method} {path} -> HTTP 429 {BLOCKED_CODE}: not retried", status=429)
                if code == 429 or code == 529:
                    last_error = f"HTTP {code}"
                    wait = self._retry_wait(resp, attempt)
                elif code >= 500:
                    if not retry_server_errors:
                        raise NotionError(f"{method} {path} -> HTTP {code} (not retried): {resp.text[:500]}", status=code)
                    last_error = f"HTTP {code}"
                    wait = self._retry_wait(resp, attempt)
                else:
                    raise NotionError(f"{method} {path} -> HTTP {code}: {resp.text[:500]}", status=code)
            else:
                wait = float(min(60, 2**attempt))

            log.warning("Notion %s %s: %s; retry %d/%d in %.1fs", method, path, last_error, attempt + 1, MAX_TRIES, wait)
            if attempt < MAX_TRIES - 1:
                self._sleep(wait)
        raise NotionError(f"{method} {path} failed after {MAX_TRIES} tries: {last_error}")

    # -- public API ---------------------------------------------------------
    def query(
        self,
        ds_id: str,
        filter: Json | None = None,  # noqa: A002 - mirrors the Notion API name
        sorts: list[Json] | None = None,
    ) -> list[Json]:
        """Query every row of a data source, following pagination."""
        results: list[Json] = []
        cursor: str | None = None
        while True:
            body: Json = {"page_size": 100}
            if filter is not None:
                body["filter"] = filter
            if sorts is not None:
                body["sorts"] = sorts
            if cursor is not None:
                body["start_cursor"] = cursor
            page = self._call("POST", f"/data_sources/{ds_id}/query", body=body)
            results.extend(page.get("results", []))
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                return results

    def create_page(
        self,
        ds_id: str,
        properties: Json,
        children: list[Json] | None = None,
        icon: Json | None = None,
    ) -> Json:
        """Create a row in a data source. ``icon`` is a Notion icon object (see hq.icons)."""
        body: Json = {
            "parent": {"type": "data_source_id", "data_source_id": ds_id},
            "properties": properties,
        }
        if children:
            body["children"] = children
        if icon is not None:
            body["icon"] = icon
        # Not retried on 5xx or network errors: the row may already exist.
        return self._call("POST", "/pages", body=body, retry_server_errors=False)

    def create_child_page(
        self,
        parent_page_id: str,
        title: str,
        children: Sequence[Json] | None = None,
        icon: Json | None = None,
    ) -> Json:
        """Create a page under another page (not retried: the page may already exist)."""
        body: Json = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {"title": {"title": rich_text(title)}},
        }
        if children:
            body["children"] = list(children)[:APPEND_CHILDREN_LIMIT]
        if icon is not None:
            body["icon"] = icon
        return self._call("POST", "/pages", body=body, retry_server_errors=False)

    def create_view(self, body: Json) -> Json:
        """Create a database view, e.g. a linked view on a page (``create_database``). Views
        in the chart configuration need Notion-Version 2026-03-11 (see hq.jobs.dashboards)."""
        return self._call("POST", "/views", body=body, retry_server_errors=False, version=VIEWS_VERSION)

    def delete_block(self, block_id: str) -> Json:
        """Delete (archive) a block, e.g. an empty dashboard callout whose view was not created."""
        return self._call("DELETE", f"/blocks/{block_id}")

    def block_children(self, block_id: str, page_size: int = 100) -> list[Json]:
        """The first page of child blocks of a page (``page_size=1`` to test emptiness)."""
        page = self._call("GET", f"/blocks/{block_id}/children", params={"page_size": page_size})
        return list(page.get("results", []))

    def all_block_children(self, block_id: str) -> list[Json]:
        """Every child block of a page, following pagination (the cleanup job lists a parent's child pages)."""
        results: list[Json] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": APPEND_CHILDREN_LIMIT}
            if cursor is not None:
                params["start_cursor"] = cursor
            page = self._call("GET", f"/blocks/{block_id}/children", params=params)
            results.extend(page.get("results", []))
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                return results

    def trash_page(self, page_id: str) -> Json:
        """Move a page or row to the Notion trash (recoverable for 30 days). ``in_trash`` is the
        field for NOTION_VERSION 2025-09-03; a 2022-06-28 client would send ``archived`` instead.
        Trashing is idempotent, so the usual 5xx retries are safe."""
        return self._call("PATCH", f"/pages/{page_id}", body={"in_trash": True})

    def update_data_source(self, ds_id: str, properties: Json) -> Json:
        """Patch a data source's property definitions. For a select, the options sent are meant to
        replace the whole option list, so an option left out is removed (not verified live: the
        cleanup job logs a warning if Notion refuses the change, and carries on)."""
        return self._call("PATCH", f"/data_sources/{ds_id}", body={"properties": properties})

    def update_page(self, page_id: str, properties: Json | None = None, *, icon: Json | None = None) -> Json:
        """Patch the properties and/or the icon of a page or row. ``icon`` is sent at the top
        level of the request body, as the Notion API expects."""
        body: Json = {}
        if properties is not None:
            body["properties"] = properties
        if icon is not None:
            body["icon"] = icon
        return self._call("PATCH", f"/pages/{page_id}", body=body)

    def get_page(self, page_id: str) -> Json:
        """Retrieve a page or row."""
        return self._call("GET", f"/pages/{page_id}")

    def append_blocks(self, page_id: str, children: Sequence[Json]) -> Json:
        """Append blocks to a page, in chunks of 100. Returns the last response
        (or an empty dict when there is nothing to append)."""
        last: Json = {}
        items = list(children)
        for start in range(0, len(items), APPEND_CHILDREN_LIMIT):
            chunk = items[start : start + APPEND_CHILDREN_LIMIT]
            last = self._call("PATCH", f"/blocks/{page_id}/children", body={"children": chunk})
        return last

    def create_comment(self, page_id: str, rich_text: list[Json]) -> Json:
        """Post a comment on a page."""
        return self._call(
            "POST",
            "/comments",
            body={"parent": {"page_id": page_id}, "rich_text": rich_text},
        )

    def retrieve_data_source(self, ds_id: str) -> Json:
        """Retrieve a data source (schema and metadata)."""
        return self._call("GET", f"/data_sources/{ds_id}")


BLOCKED_CODE = "public_api_request_blocked"


def _error_code(resp: requests.Response) -> str | None:
    """The ``code`` field of a Notion error body, or None."""
    try:
        data = resp.json()
    except (ValueError, AttributeError):
        return None
    return data.get("code") if isinstance(data, dict) else None


def _decode(resp: requests.Response) -> Json:
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except ValueError as exc:
        raise NotionError(f"Notion returned non-JSON body: {exc}") from exc
    return data if isinstance(data, dict) else {"value": data}


# ---------------------------------------------------------------------------
# Data source ids
# ---------------------------------------------------------------------------
def load_data_source_ids(
    env: dict[str, str] | None = None,
    path: Path | None = None,
) -> dict[str, str]:
    """Return ``{database title: data_source_id}``.

    Reads env ``HQ_IDS_JSON`` (a JSON object string) first, then the file
    ``notion-ids.json`` in the project root. Returns ``{}`` (with a warning)
    when neither is available or the content is invalid.
    """
    source = os.environ if env is None else env
    raw = source.get(IDS_ENV_VAR)
    if raw:
        return _parse_ids(raw, IDS_ENV_VAR)
    file_path = path if path is not None else IDS_FILE
    if file_path.is_file():
        return _parse_ids(file_path.read_text(encoding="utf-8"), str(file_path))
    log.warning("No data source ids: set %s or create %s", IDS_ENV_VAR, file_path)
    return {}


def _parse_ids(raw: str, origin: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Invalid JSON in %s: %s", origin, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("%s must hold a JSON object keyed by database title", origin)
        return {}
    return {str(k): str(v) for k, v in data.items()}


def require_data_source(ids: dict[str, str], title: str) -> str:
    """Return the data source id for a database title or raise KeyError."""
    try:
        return ids[title]
    except KeyError as exc:
        raise KeyError(f"No data source id for database '{title}'") from exc


# ---------------------------------------------------------------------------
# Rich text helpers
# ---------------------------------------------------------------------------
def chunk_text(text: str, limit: int = RICH_TEXT_LIMIT) -> list[str]:
    """Split text into pieces of at most ``limit`` UTF-16 code units (Notion
    counts characters this way). Empty text yields an empty list."""
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if units + width > limit:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(ch)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks


def rich_text(text: str) -> list[Json]:
    """Plain text as Notion rich-text items, chunked to the per-item limit."""
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunk_text(text)]


def mention_user(user_id: str) -> Json:
    """A rich-text item that mentions a Notion user (for comments)."""
    return {"type": "mention", "mention": {"type": "user", "user": {"id": user_id}}}


# ---------------------------------------------------------------------------
# Property builders
# ---------------------------------------------------------------------------
def p_title(text: str) -> Json:
    return {"title": rich_text(text)}


def p_text(text: str | None) -> Json:
    return {"rich_text": rich_text(text or "")}


def p_number(value: float | int | None) -> Json:
    return {"number": value}


def p_select(name: str | None) -> Json:
    return {"select": {"name": name} if name else None}


def p_multi(names: Iterable[str]) -> Json:
    return {"multi_select": [{"name": n} for n in names]}


def p_status(name: str | None) -> Json:
    return {"status": {"name": name} if name else None}


def p_date(value: datetime | date | str | None, with_time: bool = True) -> Json:
    """Date property. Aware datetimes are sent in UTC; naive ones are taken as UTC.
    With ``with_time=False`` only the calendar date is sent."""
    if value is None or value == "":
        return {"date": None}
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc).replace(microsecond=0)
        start = dt.isoformat() if with_time else dt.date().isoformat()
    elif isinstance(value, date):
        start = value.isoformat()
    else:
        start = str(value)
    return {"date": {"start": start, "end": None}}


def p_checkbox(value: bool) -> Json:
    return {"checkbox": bool(value)}


def p_url(url: str | None) -> Json:
    return {"url": url or None}


def p_email(email: str | None) -> Json:
    return {"email": email or None}


def p_relation(ids: Iterable[str]) -> Json:
    return {"relation": [{"id": i} for i in ids]}


def p_people(user_ids: Iterable[str]) -> Json:
    return {"people": [{"object": "user", "id": i} for i in user_ids]}


# ---------------------------------------------------------------------------
# Property readers (take the property value object from a page)
# ---------------------------------------------------------------------------
def prop(page: Json | None, name: str) -> Json | None:
    """The raw property value of ``name`` on a page, or None."""
    if not page:
        return None
    return (page.get("properties") or {}).get(name)


def _plain(items: Sequence[Json] | None) -> str:
    parts: list[str] = []
    for item in items or []:
        text = item.get("plain_text")
        if text is None:
            text = (item.get("text") or {}).get("content", "")
        parts.append(text or "")
    return "".join(parts)


def _parse_iso(value: str) -> datetime | date:
    if "T" in value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.fromisoformat(value)


def r_title(p: Json | None) -> str:
    return _plain((p or {}).get("title"))


def r_text(p: Json | None) -> str:
    return _plain((p or {}).get("rich_text"))


def r_number(p: Json | None) -> float | None:
    return (p or {}).get("number")


def r_select(p: Json | None) -> str | None:
    return ((p or {}).get("select") or {}).get("name")


def r_multi(p: Json | None) -> list[str]:
    return [o.get("name", "") for o in (p or {}).get("multi_select") or []]


def r_status(p: Json | None) -> str | None:
    return ((p or {}).get("status") or {}).get("name")


def r_date(p: Json | None) -> datetime | date | None:
    """Start of a date property: ``datetime`` (aware) if it has a time, else ``date``."""
    value = ((p or {}).get("date") or {}).get("start")
    return _parse_iso(value) if value else None


def r_checkbox(p: Json | None) -> bool:
    return bool((p or {}).get("checkbox", False))


def r_url(p: Json | None) -> str | None:
    return (p or {}).get("url")


def r_relation(p: Json | None) -> list[str]:
    return [r.get("id", "") for r in (p or {}).get("relation") or []]


def r_people(p: Json | None) -> list[str]:
    return [u.get("id", "") for u in (p or {}).get("people") or []]


def r_formula(p: Json | None) -> Any:
    """Value of a formula property, whatever its result type."""
    formula = (p or {}).get("formula") or {}
    kind = formula.get("type")
    if kind in ("string", "number", "boolean"):
        return formula.get(kind)
    if kind == "date":
        value = (formula.get("date") or {}).get("start")
        return _parse_iso(value) if value else None
    return None
