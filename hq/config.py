"""Config database access and persisted job state.

The Config database has rows keyed by "Key" (title) with "Value" (text) and
"Number". ``load`` returns one dict: {key: number or text}. Keys missing from
the database fall back to ``DEFAULTS`` (the values in build/schema-spec.md).

Job state (last import, last daily run, last weekly run) is stored as extra
Config rows with the keys ``STATE_*`` below. Their Value is an ISO timestamp
or date.

SEASON_START is read differently: its Config "Date" property (the Cairo day of it)
wins; otherwise its Value text must be YYYY-MM-DD. Empty or invalid means no season.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from hq import notion as nt
from hq import schema as S
from hq.repo import Repo, cairo_day_of, get_date, get_number, get_text, get_title

log = logging.getLogger("hq.config")

STATE_LAST_IMPORT = "STATE_LAST_IMPORT"
STATE_LAST_DAILY = "STATE_LAST_DAILY"
STATE_LAST_WEEKLY = "STATE_LAST_WEEKLY"
STATE_LAST_DASHBOARD_REPAIR = "STATE_LAST_DASHBOARD_REPAIR"  # Cairo day of the last dashboard view repair pass
STATE_KEYS: tuple[str, ...] = (STATE_LAST_IMPORT, STATE_LAST_DAILY, STATE_LAST_WEEKLY, STATE_LAST_DASHBOARD_REPAIR)
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
    "ICON_BACKFILL_PER_RUN": 300,
    "TITLE_FIX_PER_RUN": 200,
    "DAILY_LOG_DAYS": 60,
    "WEEKLY_BACKFILL_WEEKS": 12,
    "DASHBOARDS_AUTO": 1,
    "ADMIN_USER_ID": "",
    "SEASON_START": None,  # a date, or None = no season (see _season_start)
    "SEASON_FLAG_PER_RUN": 400,
    "TOPIC_ROUTE_PER_RUN": 300,
}

_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _season_start(page: dict[str, Any]) -> date | None:
    """The global season start of the SEASON_START row: its Date property (Cairo day), else
    its Value text as YYYY-MM-DD. None when both are empty or the text is not a date."""
    when = cairo_day_of(get_date(page, S.P_Config.DATE))
    if when is not None:
        return when
    text = get_text(page, S.P_Config.VALUE).strip()
    if not text:
        return None
    if _ISO_DAY.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    log.warning("Config %s has an invalid date %r (expected YYYY-MM-DD); there is no season start", S.ConfigKey.SEASON_START, text)
    return None


def load(repo: Repo) -> dict[str, Any]:
    """Return the effective configuration: database rows over ``DEFAULTS``."""
    cfg: dict[str, Any] = dict(DEFAULTS)
    for page in repo.rows(S.DB_CONFIG):
        key = get_title(page, S.P_Config.TITLE).strip()
        if not key:
            continue
        if key == S.ConfigKey.SEASON_START:
            cfg[key] = _season_start(page)
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
