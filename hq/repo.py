"""Data access over the Notion databases used by the jobs.

Design:

* Each database is queried once per run (``Repo.rows``) and kept in memory.
  Jobs look rows up in memory (``Repo.find`` by a text key such as
  "Source Key"), so a run makes one query per database, not one per row.
* Writes go through ``create``, ``update`` and ``sync``. ``sync`` sends only
  the properties whose values differ from the cached row, so a second run with
  unchanged data writes nothing. This is what makes repeated runs safe.
* In dry-run mode no write reaches Notion and no comment is posted. The
  in-memory cache is still updated, so the rest of the run behaves as if the
  writes had happened, and every intended write is recorded in ``write_log``.

Property values are passed around in the Notion API JSON shape produced by the
``hq.notion`` builders (``p_*``). Cached rows are Notion page dicts.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from hq import notion as nt
from hq import schema as S
from hq.notion import NotionError
from hq.logic.topic_map import TopicInfo
from hq.timeutil import cairo_date

log = logging.getLogger("hq.repo")

Json = dict[str, Any]

_NUMBER_EPS = 1e-9


# ---------------------------------------------------------------------------
# Raw value helpers
# ---------------------------------------------------------------------------
def _plain(items: Any) -> str:
    parts: list[str] = []
    for item in items or []:
        text = item.get("plain_text")
        if text is None:
            text = (item.get("text") or {}).get("content", "")
        parts.append(text or "")
    return "".join(parts)


def _text_key(page: Json, prop: str) -> str:
    raw = nt.prop(page, prop) or {}
    if "rich_text" in raw:
        return _plain(raw["rich_text"]).strip()
    if "title" in raw:
        return _plain(raw["title"]).strip()
    return ""


def _title_of(properties: Json) -> str:
    for value in properties.values():
        if isinstance(value, dict) and "title" in value:
            return _plain(value["title"])
    return ""


def _parse_start(value: str) -> datetime | date:
    if "T" in value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return date.fromisoformat(value)


def _create_key(db: str, properties: Json) -> tuple[str | None, str | None]:
    """The (property name, value) that identifies a row for re-lookup after a failed create."""
    for name in (S.P_Problems.SOURCE_KEY, S.P_Contests.EXTERNAL_ID):
        raw = properties.get(name) or {}
        text = _plain(raw.get("rich_text")).strip() if "rich_text" in raw else ""
        if text:
            return name, text
    if db == S.DB_CONFIG:
        title = _title_of(properties).strip()
        if title:
            return S.P_Config.TITLE, title
    return None, None


def is_blank(raw: Json | None) -> bool:
    """True when a raw property value holds nothing (fill-only writes use this).

    Checkboxes are never blank: ``False`` is a value the user may have set.
    """
    if not raw:
        return True
    if "title" in raw:
        return not _plain(raw["title"]).strip()
    if "rich_text" in raw:
        return not _plain(raw["rich_text"]).strip()
    if "number" in raw:
        return raw["number"] is None
    if "select" in raw:
        return not raw["select"]
    if "status" in raw:
        return not raw["status"]
    if "multi_select" in raw:
        return not raw["multi_select"]
    if "date" in raw:
        return not raw["date"]
    if "checkbox" in raw:
        return False
    if "url" in raw:
        return not raw["url"]
    if "email" in raw:
        return not raw["email"]
    if "relation" in raw:
        return not raw["relation"]
    if "people" in raw:
        return not raw["people"]
    return True


def differs(raw: Json | None, new: Json) -> bool:
    """True when the value ``new`` (builder output) differs from the raw cached value."""
    if raw is None:
        return True
    key = next(iter(new))
    old = raw.get(key)
    value = new[key]
    if key in ("title", "rich_text"):
        return _plain(old) != _plain(value)
    if key == "number":
        if old is None or value is None:
            return old != value
        return abs(float(old) - float(value)) > _NUMBER_EPS
    if key in ("select", "status"):
        return _name(old) != _name(value)
    if key == "multi_select":
        return sorted(_name(x) for x in old or []) != sorted(_name(x) for x in value or [])
    if key == "checkbox":
        return bool(old) != bool(value)
    if key in ("url", "email"):
        return (old or None) != (value or None)
    if key in ("relation", "people"):
        return {x.get("id") for x in old or []} != {x.get("id") for x in value or []}
    if key == "date":
        return _date_value(old) != _date_value(value)
    return old != value


def _name(item: Json | None) -> str | None:
    return (item or {}).get("name")


def _date_value(d: Json | None) -> datetime | date | None:
    start = (d or {}).get("start")
    return _parse_start(start) if start else None


# ---------------------------------------------------------------------------
# Typed getters (page dict + property name)
# ---------------------------------------------------------------------------
def get_title(page: Json | None, name: str) -> str:
    return nt.r_title(nt.prop(page, name))


def get_text(page: Json | None, name: str) -> str:
    return nt.r_text(nt.prop(page, name))


def get_number(page: Json | None, name: str) -> float | None:
    return nt.r_number(nt.prop(page, name))


def get_select(page: Json | None, name: str) -> str | None:
    return nt.r_select(nt.prop(page, name))


def get_multi(page: Json | None, name: str) -> list[str]:
    return nt.r_multi(nt.prop(page, name))


def get_status(page: Json | None, name: str) -> str | None:
    return nt.r_status(nt.prop(page, name))


def get_date(page: Json | None, name: str) -> datetime | date | None:
    return nt.r_date(nt.prop(page, name))


def get_checkbox(page: Json | None, name: str) -> bool:
    return nt.r_checkbox(nt.prop(page, name))


def get_url(page: Json | None, name: str) -> str | None:
    return nt.r_url(nt.prop(page, name))


def get_email(page: Json | None, name: str) -> str | None:
    return ((nt.prop(page, name) or {}).get("email")) or None


def get_relation(page: Json | None, name: str) -> list[str]:
    return nt.r_relation(nt.prop(page, name))


def get_people(page: Json | None, name: str) -> list[str]:
    return nt.r_people(nt.prop(page, name))


def get_formula(page: Json | None, name: str) -> Any:
    return nt.r_formula(nt.prop(page, name))


def cairo_day_of(value: datetime | date | None) -> date | None:
    """Cairo calendar day of a date or datetime property value (None stays None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return cairo_date(value)
    return value


# ---------------------------------------------------------------------------
# Typed views used by the jobs
# ---------------------------------------------------------------------------
@dataclass
class Member:
    """A Members row, read into plain fields. ``page`` is the live cached row."""

    id: str
    name: str
    page: Json
    active: bool
    person_ids: list[str]
    team_ids: list[str]
    level_ids: list[str]
    level_no: int
    cf_handle: str | None
    atcoder_handle: str | None
    leetcode_handle: str | None
    email: str | None
    subscriptions: list[str]
    cf_rating: int | None
    daily_target: int | None
    last_sync: date | datetime | None

    @property
    def has_handles(self) -> bool:
        return bool(self.cf_handle or self.atcoder_handle or self.leetcode_handle)


@dataclass
class Topic:
    """A Topics row. ``band_*`` may be None for a topic not yet banded."""

    id: str
    name: str
    cf_tags: tuple[str, ...]
    band_low: int | None
    band_high: int | None
    level_no: int
    order: int
    core: bool
    level_ids: list[str]

    @property
    def has_band(self) -> bool:
        return self.band_low is not None and self.band_high is not None

    def info(self) -> TopicInfo:
        if not self.has_band:
            raise ValueError(f"topic {self.name!r} has no rating band")
        return TopicInfo(
            id=self.id,
            name=self.name,
            cf_tags=self.cf_tags,
            band_low=int(self.band_low),  # type: ignore[arg-type]
            band_high=int(self.band_high),  # type: ignore[arg-type]
            level_no=self.level_no,
            order=self.order,
        )


def _int_or_none(value: float | None) -> int | None:
    return int(value) if value is not None else None


def _handle(value: str) -> str | None:
    value = value.strip()
    return value or None


# ---------------------------------------------------------------------------
# Repo
# ---------------------------------------------------------------------------
class Repo:
    """Cached, dry-run aware access to the HQ databases."""

    def __init__(self, notion: Any, ids: dict[str, str], *, dry_run: bool = False) -> None:
        self.notion = notion
        self.ids = dict(ids)
        self.dry_run = dry_run
        self.write_log: list[str] = []
        self.writes = 0
        self._rows: dict[str, list[Json]] = {}
        self._by_id: dict[str, dict[str, Json]] = {}
        self._index: dict[tuple[str, str], tuple[dict[str, Json], dict[str, str]]] = {}
        self._pending: dict[str, list[Json]] = {}
        self._seq = 0

    # -- reading ------------------------------------------------------------
    def ds(self, db: str) -> str:
        return nt.require_data_source(self.ids, db)

    def rows(self, db: str) -> list[Json]:
        """All rows of a database (queried once per run)."""
        if db not in self._rows:
            pages = list(self.notion.query(self.ds(db)))
            known = {p["id"] for p in pages}
            pages += [p for p in self._pending.pop(db, []) if p["id"] not in known]
            self._rows[db] = pages
            self._by_id[db] = {p["id"]: p for p in pages}
        return self._rows[db]

    def by_id(self, db: str, page_id: str | None) -> Json | None:
        if not page_id:
            return None
        self.rows(db)
        return self._by_id[db].get(page_id)

    def find(self, db: str, prop: str, key: str | None) -> Json | None:
        """First row whose text/title property ``prop`` equals ``key`` (stripped)."""
        if not key:
            return None
        index, _ = self._index_for(db, prop)
        return index.get(key)

    def _index_for(self, db: str, prop: str) -> tuple[dict[str, Json], dict[str, str]]:
        slot = (db, prop)
        if slot not in self._index:
            index: dict[str, Json] = {}
            reverse: dict[str, str] = {}
            for page in self.rows(db):
                key = _text_key(page, prop)
                if key:
                    reverse[page["id"]] = key
                    index.setdefault(key, page)
            self._index[slot] = (index, reverse)
        return self._index[slot]

    def _reindex(self, db: str, page: Json) -> None:
        pid = page["id"]
        for (d, prop), (index, reverse) in self._index.items():
            if d != db:
                continue
            new = _text_key(page, prop)
            old = reverse.get(pid)
            if old == new:
                continue
            if old is not None:
                reverse.pop(pid, None)
                if index.get(old) is page:
                    del index[old]
            if new:
                reverse[pid] = new
                index.setdefault(new, page)

    # -- writing ------------------------------------------------------------
    def create(self, db: str, properties: Json, children: list[Json] | None = None) -> Json:
        """Create a row. For a row with a Source Key / External ID, a failed create
        (5xx, timeout) is not blindly repeated: the table is re-read, an existing row
        with that key is adopted (after syncing it), and only if none exists is the
        create retried once. Rows without a key are never retried."""
        title = _title_of(properties)
        self.writes += 1
        if self.dry_run:
            self._seq += 1
            page: Json = {"id": f"dry-run-{self._seq}", "properties": copy.deepcopy(properties)}
            self._record(f"create {db}: {title}")
        else:
            page = self._create_remote(db, properties, children)
        if db not in self._rows:
            # Table not loaded in this run (e.g. Sync Log): keep the page for
            # the first later load instead of querying a large table now.
            self._pending.setdefault(db, []).append(page)
            return page
        self._rows[db].append(page)
        self._by_id[db][page["id"]] = page
        self._reindex(db, page)
        return page

    def _create_remote(self, db: str, properties: Json, children: list[Json] | None) -> Json:
        key_prop, key = _create_key(db, properties)
        try:
            resp = self.notion.create_page(self.ds(db), properties, children)
        except NotionError as exc:
            if key is None:
                raise
            log.warning("create %s (%s=%s) failed (%s); checking whether it was stored", db, key_prop, key, exc)
            self.reload(db)
            existing = self.find(db, key_prop, key)
            if existing is not None:
                log.warning("adopted existing %s row for %s=%s", db, key_prop, key)
                self.sync(db, existing, properties)
                return existing
            resp = self.notion.create_page(self.ds(db), properties, children)
        return {
            "id": resp["id"],
            "properties": copy.deepcopy(resp.get("properties") or properties),
        }

    def reload(self, db: str) -> None:
        """Re-read one table from Notion and rebuild its lookup indexes."""
        pages = list(self.notion.query(self.ds(db)))
        self._rows[db] = pages
        self._by_id[db] = {p["id"]: p for p in pages}
        for slot in [s for s in self._index if s[0] == db]:
            del self._index[slot]

    def has_children(self, page_id: str) -> bool:
        """Whether a page has at least one child block (a read; also in dry-run)."""
        return bool(self.notion.block_children(page_id, page_size=1))

    def update(self, db: str, page: Json, properties: Json) -> Json:
        if not properties:
            return page
        self.writes += 1
        if self.dry_run:
            self._record(f"update {db}: {_title_of(page.get('properties') or {}) or page['id']} {sorted(properties)}")
        else:
            self.notion.update_page(page["id"], properties)
        page.setdefault("properties", {}).update(copy.deepcopy(properties))
        self._reindex(db, page)
        return page

    def sync(self, db: str, page: Json, properties: Json, *, fill_only: bool = False) -> Json:
        """Write only the properties that differ (or, with ``fill_only``, only the blank ones).

        Returns the dict of properties that were written.
        """
        current = page.get("properties") or {}
        if fill_only:
            # Only fill an empty field, and only with a value that is not itself empty.
            changes = {k: v for k, v in properties.items() if is_blank(current.get(k)) and not is_blank(v)}
        else:
            changes = {k: v for k, v in properties.items() if differs(current.get(k), v)}
        if changes:
            self.update(db, page, changes)
        return changes

    def upsert(self, db: str, key_prop: str, key: str, properties: Json) -> tuple[Json, bool]:
        """Create the row keyed by ``key`` or sync it. Returns (page, created)."""
        page = self.find(db, key_prop, key)
        if page is None:
            return self.create(db, properties), True
        self.sync(db, page, properties)
        return page, False

    def create_child_page(self, parent_page_id: str, title: str, children: list[Json] | None = None) -> Json:
        """Create a page under ``parent_page_id`` (dry-run aware). Returns the page (with url)."""
        self.writes += 1
        if self.dry_run:
            self._seq += 1
            self._record(f"create page under {parent_page_id}: {title}")
            return {"id": f"dry-run-page-{self._seq}", "url": ""}
        resp = self.notion.create_child_page(parent_page_id, title, children)
        return {"id": resp["id"], "url": resp.get("url", "")}

    def create_view(self, body: Json) -> Json:
        """Create a database view (dry-run aware)."""
        self.writes += 1
        if self.dry_run:
            self._record(f"create view {body.get('name')}: {body.get('type')}")
            return {}
        return self.notion.create_view(body)

    def data_source_properties(self, db: str) -> dict[str, dict]:
        """Property name -> {"id", "type"} of a database's data source (read; also in dry-run)."""
        resp = self.notion.retrieve_data_source(self.ds(db))
        return dict(resp.get("properties") or {})

    def comment(self, page_id: str, rich_text: list[Json]) -> Json:
        self.writes += 1
        if self.dry_run:
            self._record(f"comment on {page_id}")
            return {}
        return self.notion.create_comment(page_id, rich_text)

    def append(self, page_id: str, children: list[Json]) -> None:
        if not children:
            return
        self.writes += 1
        if self.dry_run:
            self._record(f"append {len(children)} blocks to {page_id}")
            return
        self.notion.append_blocks(page_id, children)

    def _record(self, message: str) -> None:
        self.write_log.append(message)
        log.info("[dry-run] %s", message)

    # -- typed views --------------------------------------------------------
    def level_no(self, level_id: str | None) -> int | None:
        page = self.by_id(S.DB_LEVELS, level_id)
        if page is None:
            return None
        return _int_or_none(get_number(page, S.P_Levels.LEVEL_NO))

    def level_band_low(self, level_id: str | None) -> int | None:
        page = self.by_id(S.DB_LEVELS, level_id)
        if page is None:
            return None
        return _int_or_none(get_number(page, S.P_Levels.BAND_LOW))

    def team_name(self, team_id: str | None) -> str | None:
        page = self.by_id(S.DB_TEAMS, team_id)
        if page is None:
            return None
        return get_title(page, S.P_Teams.TITLE) or None

    def members(self) -> list[Member]:
        out: list[Member] = []
        for page in self.rows(S.DB_MEMBERS):
            level_ids = get_relation(page, S.P_Members.LEVEL)
            level_no = self.level_no(level_ids[0]) if level_ids else None
            cf_rating = _int_or_none(get_number(page, S.P_Members.CF_RATING))
            out.append(
                Member(
                    id=page["id"],
                    name=get_title(page, S.P_Members.TITLE).strip() or "(unnamed)",
                    page=page,
                    active=get_checkbox(page, S.P_Members.ACTIVE),
                    person_ids=get_people(page, S.P_Members.PERSON),
                    team_ids=get_relation(page, S.P_Members.TEAM),
                    level_ids=level_ids,
                    level_no=level_no or 1,
                    cf_handle=_handle(get_text(page, S.P_Members.CF_HANDLE)),
                    atcoder_handle=_handle(get_text(page, S.P_Members.ATCODER_HANDLE)),
                    leetcode_handle=_handle(get_text(page, S.P_Members.LEETCODE_HANDLE)),
                    email=get_email(page, S.P_Members.EMAIL),
                    subscriptions=get_multi(page, S.P_Members.CONTEST_SUBSCRIPTIONS),
                    cf_rating=cf_rating,
                    daily_target=_int_or_none(get_number(page, S.P_Members.DAILY_TARGET)),
                    last_sync=get_date(page, S.P_Members.LAST_SYNC),
                )
            )
        return out

    def topics(self) -> list[Topic]:
        out: list[Topic] = []
        for page in self.rows(S.DB_TOPICS):
            level_ids = get_relation(page, S.P_Topics.LEVEL)
            level_no = self.level_no(level_ids[0]) if level_ids else None
            out.append(
                Topic(
                    id=page["id"],
                    name=get_title(page, S.P_Topics.TITLE).strip() or "(unnamed)",
                    cf_tags=tuple(get_multi(page, S.P_Topics.CF_TAGS)),
                    band_low=_int_or_none(get_number(page, S.P_Topics.BAND_LOW)),
                    band_high=_int_or_none(get_number(page, S.P_Topics.BAND_HIGH)),
                    level_no=level_no or 0,
                    order=int(get_number(page, S.P_Topics.ORDER) or 0),
                    core=get_checkbox(page, S.P_Topics.CORE),
                    level_ids=level_ids,
                )
            )
        return out


def member_task_props(
    member: Member,
    *,
    title: str,
    task_type: str,
    status: str,
    due: datetime | None,
    origin: str,
    source_key: str,
) -> Json:
    """Common properties of a Tasks row assigned to one member."""
    return {
        S.P_Tasks.TITLE: nt.p_title(title),
        S.P_Tasks.ASSIGNEE: nt.p_relation([member.id]),
        S.P_Tasks.ASSIGNEE_PERSON: nt.p_people(member.person_ids),
        S.P_Tasks.WHO: nt.p_select(member.name),
        S.P_Tasks.TYPE: nt.p_select(task_type),
        S.P_Tasks.STATUS: nt.p_status(status),
        S.P_Tasks.DUE: nt.p_date(due),
        S.P_Tasks.ORIGIN: nt.p_select(origin),
        S.P_Tasks.SOURCE_KEY: nt.p_text(source_key),
    }
