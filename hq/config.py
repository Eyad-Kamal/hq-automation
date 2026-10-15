"""Config database access and persisted job state.

The Config database has rows keyed by "Key" (title) with "Value" (text) and
"Number". ``load`` returns one dict: {key: number or text}. Keys missing from
the database fall back to ``DEFAULTS`` (the values in build/schema-spec.md).

Job state (last import, last daily run, last weekly run) is stored as extra
Config rows with the keys ``STATE_*`` below. Their Value is an ISO timestamp
or date.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from hq import notion as nt
from hq import schema as S
from hq.repo import Repo, get_number, get_text, get_title

log = logging.getLogger("hq.config")

STATE_LAST_IMPORT = "STATE_LAST_IMPORT"
STATE_LAST_DAILY = "STATE_LAST_DAILY"
STATE_LAST_WEEKLY = "STATE_LAST_WEEKLY"
STATE_KEYS: tuple[str, ...] = (STATE_LAST_IMPORT, STATE_LAST_DAILY, STATE_LAST_WEEKLY)
STATE_DESCRIPTION = "Automation state written by the jobs. Do not edit by hand."

DEFAULTS: dict[str, Any] = {
    "TIMEZONE": "Africa/Cairo",
    "WEEK_START": "Saturday",
    "GATE_L1": 1000,
    "GATE_L2": 1300,
    "GATE_L3": 1600,
    "GATE_L4": 1900,
    "TIER_NOVICE": 1,
    "TIER_APPRENTICE": 5,
    "TIER_COMPETENT": 12,
    "TIER_PROFICIENT": 25,
    "TIER_PROFICIENT_ABOVE": 3,
    "TIER_MASTER": 40,
    "TIER_MASTER_ABOVE": 8,
    "BAND_BELOW_FACTOR": 0.25,
    "BAND_IN_FACTOR": 1,
    "BAND_ABOVE_FACTOR": 1.5,
    "MODE_UPSOLVE": 0.7,
    "MODE_EDITORIAL": 0.5,
    "ATCODER_OFFSET": 0,
    "LC_EASY": 1000,
    "LC_MEDIUM": 1400,
    "LC_HARD": 1900,
    "AUTO_TASK_PROBLEMS": 3,
    "ALERT_MINUTES": 30,
    "CONTEST_TASK_LEAD_HOURS": 24,
    "FREEZE_EVERY": 7,
    "FREEZE_MAX": 2,
    "DEFAULT_DAILY_TARGET": 3,
    "IMPORT_BACKFILL_DAYS": 120,
    "IMPORT_MAX_CREATES": 400,
    "UPSOLVE_WINDOW_DAYS": 7,
    "AUTO_PRACTICE_TASKS": 0,
    "LEETCODE_DAILY_TASKS": 1,
    "ICON_BACKFILL_PER_RUN": 100,
    "DAILY_LOG_DAYS": 60,
    "WEEKLY_BACKFILL_WEEKS": 12,
    "DASHBOARDS_AUTO": 1,
    "ADMIN_USER_ID": "",
}


def load(repo: Repo) -> dict[str, Any]:
    """Return the effective configuration: database rows over ``DEFAULTS``."""
    cfg: dict[str, Any] = dict(DEFAULTS)
    for page in repo.rows(S.DB_CONFIG):
        key = get_title(page, S.P_Config.TITLE).strip()
        if not key:
            continue
        number = get_number(page, S.P_Config.NUMBER)
        if number is not None:
            cfg[key] = number
            continue
        text = get_text(page, S.P_Config.VALUE).strip()
        if not text:
            continue
        default = DEFAULTS.get(key)
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                cfg[key] = float(text)
            except ValueError:
                log.warning("Config %s has a non-numeric value %r; using the default", key, text)
            continue
        cfg[key] = text
    return cfg


def num(cfg: dict[str, Any], key: str) -> float:
    """Numeric config value (the default when missing or not a number)."""
    value = cfg.get(key, DEFAULTS.get(key))
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def get_state(repo: Repo, key: str) -> str | None:
    row = repo.find(S.DB_CONFIG, S.P_Config.TITLE, key)
    if row is None:
        return None
    return get_text(row, S.P_Config.VALUE).strip() or None


def state_datetime(repo: Repo, key: str) -> datetime | None:
    """Stored ISO timestamp as an aware UTC datetime, or None."""
    raw = get_state(repo, key)
    if not raw:
        return None
    value = datetime.fromisoformat(raw)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def state_date(repo: Repo, key: str) -> date | None:
    raw = get_state(repo, key)
    return date.fromisoformat(raw[:10]) if raw else None


def set_state(repo: Repo, key: str, value: str) -> None:
    """Create or update the Config row ``key`` with a text value (dry-run aware)."""
    row =repo.find(S.DB_CONFIG, S.P_Config.TITLE, key)
    if row is None:
        repo.create(
            S.DB_CONFIG,
            {
                S.P_Config.TITLE: nt.p_title(key),
                S.P_Config.VALUE: nt.p_text(value),
                S.P_Config.DESCRIPTION: nt.p_text(STATE_DESCRIPTION),
            },
        )
        return
    repo.sync(S.DB_CONFIG, row, {S.P_Config.VALUE: nt.p_text(value)})
