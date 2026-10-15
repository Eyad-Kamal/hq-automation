"""Row icons for the HQ databases.

The icon catalogue is ``hq/icons.json`` (packaged next to this module). Each entry has an
external image URL (``judges`` and ``ui``) and an emoji (``emoji_fallback``). ``ui(key)``
and ``judge(name)`` return a Notion icon object: an external image when a URL exists,
otherwise the emoji, and None when neither exists.

``icon_for(db, values)`` decides the icon of a row from its property values. The same
function serves both cases: the icon sent when a job creates a row (the builder output
from ``hq.notion.p_*``) and the icon of an existing row (``page["properties"]``). Both
hold the same shapes, so one rule set covers creates and backfill.

Rules for existing rows (``backfill``):
* Only a row whose icon is empty is filled. A user's own icon is never replaced.
* At most ``ICON_BACKFILL_PER_RUN`` rows are changed per run.
* The Daily Log state icon (``daily_log_state``) changes only while the current icon is one
  of this module's icons, or empty.
* Sync Log rows get their icon at create time only (the table is large and is not read
  for backfill).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from hq import notion as nt
from hq import schema as S
from hq.config import num
from hq.schema import ConfigKey as K

log = logging.getLogger("hq.icons")

ICONS_FILE: Path = Path(__file__).resolve().parent / "icons.json"
Json = dict[str, Any]

# Platform / Judge option names that differ from the keys of icons.json.
JUDGE_ALIASES: dict[str, str] = {"CF Gym": "Codeforces Gym"}

TASK_TYPE_ICON: dict[str, str] = {
    "Contest": "contest",
    "Topic Practice": "topic",
    "Upsolve": "upsolve",
    "Sheet": "sheet",
    "Reading": "reading",
    "Custom": "checklist",
}
TIER_ICON: dict[str, str] = {"Bronze": "medal_bronze", "Silver": "medal_silver", "Gold": "medal_gold", "Legendary": "crown"}
DB_ICON: dict[str, str] = {
    S.DB_CONTEST_ENTRIES: "contest",
    S.DB_WEEKLY_SUMMARY: "weekly_summary",
    S.DB_WEEKLY_REPORTS: "weekly_report",
    S.DB_RATING_HISTORY: "rating",
    S.DB_GOALS: "goal",
    S.DB_LEARNING_QUEUE: "topic",
    S.DB_SYNC_LOG: "sync_log",
    S.DB_MEMBERS: "member",
}
# Databases whose existing rows are backfilled (Sync Log is excluded on purpose, see above).
BACKFILL_DBS: tuple[str, ...] = (
    S.DB_PROBLEMS,
    S.DB_TASKS,
    S.DB_CONTESTS,
    S.DB_CONTEST_ENTRIES,
    S.DB_DAILY_LOG,
    S.DB_WEEKLY_SUMMARY,
    S.DB_WEEKLY_REPORTS,
    S.DB_RATING_HISTORY,
    S.DB_ACHIEVEMENTS,
    S.DB_GOALS,
    S.DB_LEARNING_QUEUE,
    S.DB_MEMBERS,
)


@lru_cache(maxsize=1)
def _catalog() -> dict[str, dict[str, str]]:
    with ICONS_FILE.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return {section: dict(data.get(section, {})) for section in ("judges", "ui", "emoji_fallback")}


def _from_entry(url: str | None, emoji: str | None) -> Json | None:
    if url:
        return {"type": "external", "external": {"url": url}}
    if emoji:
        return {"type": "emoji", "emoji": emoji}
    return None


def ui(key: str) -> Json | None:
    """Icon of a UI key (task types, Daily Log states, medals, ...)."""
    cat = _catalog()
    return _from_entry(cat["ui"].get(key), cat["emoji_fallback"].get(key))


def judge(name: str | None) -> Json | None:
    """Icon of a judge or platform name ("Codeforces", "CF Gym", "AtCoder", "LeetCode", ...)."""
    if not name:
        return None
    cat = _catalog()
    key = JUDGE_ALIASES.get(name, name)
    return _from_entry(cat["judges"].get(key), cat["emoji_fallback"].get(key))


@lru_cache(maxsize=1)
def _our_icons() -> tuple[Json, ...]:
    """Every icon object this module can produce (URL and emoji forms of each key)."""
    cat = _catalog()
    found: list[Json] = []
    for url in list(cat["judges"].values()) + list(cat["ui"].values()):
        found.append({"type": "external", "external": {"url": url}})
    for emoji in cat["emoji_fallback"].values():
        found.append({"type": "emoji", "emoji": emoji})
    return tuple(found)


def is_ours(icon: Json | None) -> bool:
    return icon is not None and icon in _our_icons()


def _sel(values: Mapping[str, Any], prop: str) -> str | None:
    return nt.r_select(values.get(prop))


def _text(values: Mapping[str, Any], prop: str) -> str:
    return nt.r_text(values.get(prop))


def _checked(values: Mapping[str, Any], prop: str) -> bool:
    return nt.r_checkbox(values.get(prop))


def icon_for(db: str, values: Mapping[str, Any]) -> Json | None:
    """The icon of a row of ``db`` with these property values, or None (leave it unset)."""
    if db == S.DB_PROBLEMS:
        return judge(_sel(values, S.P_Problems.JUDGE))
    if db == S.DB_CONTESTS:
        return judge(_sel(values, S.P_Contests.PLATFORM))
    if db == S.DB_TASKS:
        kind = _sel(values, S.P_Tasks.TYPE)
        if kind == "Custom" and _text(values, S.P_Tasks.SOURCE_KEY).startswith(S.LC_DAILY_SOURCE_PREFIX):
            return ui("leetcode_daily")
        key = TASK_TYPE_ICON.get(kind or "")
        return ui(key) if key else None
    if db == S.DB_DAILY_LOG:
        if _checked(values, S.P_DailyLog.FROZEN):
            return ui("freeze")
        return ui("streak") if _checked(values, S.P_DailyLog.ACTIVE) else ui("daily")
    if db == S.DB_ACHIEVEMENTS:
        key = TIER_ICON.get(_sel(values, S.P_Achievements.TIER) or "")
        return ui(key) if key else None
    key = DB_ICON.get(db)
    return ui(key) if key else None


def daily_log_state(repo: Any, page: Mapping[str, Any], *, active: bool, frozen: bool) -> bool:
    """Set the Daily Log row's state icon (streak / daily / freeze) when it changed.

    Only an empty icon or one of this module's icons is replaced. Returns True when a write
    was made (or would be made in dry-run)."""
    target = icon_for(S.DB_DAILY_LOG, {S.P_DailyLog.ACTIVE: nt.p_checkbox(active), S.P_DailyLog.FROZEN: nt.p_checkbox(frozen)})
    current = page.get("icon")
    if target is None or current == target:
        return False
    if current and not is_ours(current):
        return False
    repo.set_icon(S.DB_DAILY_LOG, page, target)
    return True


@dataclass
class BackfillReport:
    """What one backfill pass did. ``remaining`` counts rows still without an icon after the pass."""

    set_by_db: dict[str, int] = field(default_factory=dict)
    remaining: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.set_by_db.values())


ICON_ERROR_CHARS = 200
MAX_ICON_ERRORS = 5


def backfill(repo: Any, cfg: Mapping[str, Any]) -> BackfillReport:
    """Set icons on up to ``ICON_BACKFILL_PER_RUN`` rows whose icon is empty.

    Round-robin: one row from each database in turn, so no single table (Daily Log, say)
    takes the whole budget. A failed write uses up one attempt and is recorded in ``errors``
    (the first few, truncated), so the run goes on and the cause is visible in the Sync Log."""
    report = BackfillReport()
    cap = int(num(dict(cfg), K.ICON_BACKFILL_PER_RUN))
    queues: dict[str, list[tuple[Any, Json]]] = {}
    for db in BACKFILL_DBS:
        report.set_by_db[db] = 0
        todo: list[tuple[Any, Json]] = []
        if cap > 0:
            for page in repo.rows(db):
                if page.get("icon"):
                    continue
                icon = icon_for(db, page.get("properties") or {})
                if icon is not None:
                    todo.append((page, icon))
        queues[db] = todo
    done = 0
    while cap > 0 and done < cap and any(queues.values()):
        for db in BACKFILL_DBS:
            if done >= cap:
                break
            if not queues[db]:
                continue
            page, icon = queues[db].pop(0)
            done += 1
            try:
                repo.set_icon(db, page, icon)
            except Exception as exc:  # noqa: BLE001 - keep going; the error is reported below
                log.warning("icon write failed for %s: %s", db, exc)
                if len(report.errors) < MAX_ICON_ERRORS:
                    report.errors.append(f"{db}: {type(exc).__name__}: {str(exc)[:ICON_ERROR_CHARS]}")
                continue
            report.set_by_db[db] += 1
    report.remaining = {db: len(queues[db]) for db in BACKFILL_DBS}
    if report.total:
        log.info("icons: %d row icon(s) set", report.total)
    return report
