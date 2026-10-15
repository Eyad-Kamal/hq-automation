"""AtCoder source client.

Sources:
- Upcoming contests: HTML "Upcoming Contests" table on https://atcoder.jp/contests/
  (parsed with ``html.parser``; best-effort).
- Solves: kenkoooo.com AtCoder Problems API (submissions, problem names,
  estimated difficulties), paged by ``from_second``, at least 1.1 s apart.
- Rating history: https://atcoder.jp/users/{user}/history/json.

Difficulty: raw values below 400 are clipped with the standard formula
``400 / exp((400 - d) / 400)``, then ``offset`` is added and the result is
clamped to >= 0.
"""

from __future__ import annotations

import logging
import math
import re
import weakref
from collections.abc import Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import requests

from hq._net import RateLimiter, get_json, get_text, guarded
from hq.models import Contest, RatingChange, Solve
from hq.schema import Judge, Mode, Platform

log = logging.getLogger(__name__)

ATCODER = "https://atcoder.jp"
KENKOOKO = "https://kenkoooo.com/atcoder"
PAGE_LIMIT = 500  # submissions per kenkoooo call
MAX_PAGES = 10_000  # safety bound on paging
_limiter = RateLimiter(1.1)


# ---------------------------------------------------------------------------
# Difficulty
# ---------------------------------------------------------------------------
def clip_difficulty(raw: float | None, offset: int = 0) -> int | None:
    """Standard AtCoder low-difficulty clipping, then ``+ offset``, clamped >= 0.

    >>> clip_difficulty(None)
    >>> clip_difficulty(800, 0)
    800
    """
    if raw is None:
        return None
    value = float(raw)
    if value < 400:
        value = 400 / math.exp((400 - value) / 400)
    return max(0, int(round(value)) + int(offset))


# ---------------------------------------------------------------------------
# Upcoming contests (HTML)
# ---------------------------------------------------------------------------
class _UpcomingTableParser(HTMLParser):
    """Collects rows of the first table that follows the text "Upcoming Contests".

    Each row is ``{"headers": [...], "cells": [...], "hrefs": [...]}`` where
    ``cells`` is the text of each cell and ``hrefs`` the first link in each cell.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._armed = False
        self._in_table = False
        self._done = False
        self._row: dict[str, list[Any]] | None = None
        self._cell_text: list[str] | None = None
        self._cell_href: str | None = None
        self._is_header_cell = False
        self.rows: list[dict[str, list[Any]]] = []
        self._headers: list[str] = []

    def handle_data(self, data: str) -> None:
        if self._done:
            return
        if not self._in_table and "Upcoming Contests" in data:
            self._armed = True
        if self._cell_text is not None:
            self._cell_text.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        if tag == "table" and self._armed and not self._in_table:
            self._in_table = True
            self._armed = False
        elif not self._in_table:
            return
        elif tag == "tr":
            self._row = {"headers": [], "cells": [], "hrefs": []}
        elif tag in ("td", "th") and self._row is not None:
            self._cell_text = []
            self._cell_href = None
            self._is_header_cell = tag == "th"
        elif tag == "a" and self._cell_text is not None and self._cell_href is None:
            self._cell_href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        if self._done or not self._in_table:
            return
        if tag in ("td", "th") and self._cell_text is not None and self._row is not None:
            text = " ".join("".join(self._cell_text).split())
            if self._is_header_cell:
                self._row["headers"].append(text)
            self._row["cells"].append(text)
            self._row["hrefs"].append(self._cell_href)
            self._cell_text = None
        elif tag == "tr" and self._row is not None:
            if self._row["cells"]:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._in_table = False
            self._done = True


def parse_upcoming_table(html: str) -> list[dict[str, Any]]:
    """Return one dict per data row: ``{"start", "name", "duration", "href"}``.

    Column positions come from the header names when present, otherwise from the
    default order (start, name, duration). Header rows are skipped.
    """
    parser = _UpcomingTableParser()
    parser.feed(html)
    parser.close()

    header_row = next((r for r in parser.rows if r["headers"]), None)
    headers = [h.lower() for h in header_row["headers"]] if header_row else []
    i_start = _column(headers, "start", 0)
    i_name = _column(headers, "contest", 1)
    i_dur = _column(headers, "duration", 2)

    out: list[dict[str, Any]] = []
    for row in parser.rows:
        if row["headers"]:
            continue
        cells, hrefs = row["cells"], row["hrefs"]
        if max(i_start, i_name, i_dur) >= len(cells):
            continue
        out.append(
            {
                "start": cells[i_start],
                "name": cells[i_name],
                "duration": cells[i_dur],
                "href": hrefs[i_name],
            }
        )
    return out


def _column(headers: list[str], keyword: str, default: int) -> int:
    for idx, header in enumerate(headers):
        if keyword in header:
            return idx
    return default


def _parse_start(text: str) -> datetime:
    """Parse "2026-10-18 21:00:00+0900" into an aware UTC datetime."""
    return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S%z").astimezone(timezone.utc)


def _parse_duration_min(text: str) -> int:
    hours, minutes = text.strip().split(":")
    return int(hours) * 60 + int(minutes)


_LEADING_DECORATION = re.compile(r"^[^A-Za-z0-9]+")


def clean_contest_name(name: str) -> str:
    """Drop decorative prefix glyphs (e.g. "Ⓗ◉ ") so the name starts at the first
    ASCII letter or digit: "Ⓗ◉ AtCoder Heuristic Contest 073" becomes
    "AtCoder Heuristic Contest 073"."""
    return _LEADING_DECORATION.sub("", name).strip()


@guarded(list)
def fetch_upcoming_contests(session: requests.Session) -> list[Contest]:
    """Upcoming AtCoder contests, including heuristic (AHC) contests.

    Rows that fail to parse are skipped with a warning. Names are cleaned of
    decorative prefix glyphs.
    """
    html = get_text(session, "GET", f"{ATCODER}/contests/")
    if html is None:
        return []
    rows = parse_upcoming_table(html)
    if not rows:
        log.warning("AtCoder: no rows found in the Upcoming Contests table")
        return []
    contests: list[Contest] = []
    for row in rows:
        slug = (row["href"] or "").rstrip("/").split("/")[-1]
        if not slug:
            log.warning("AtCoder: upcoming row without contest link: %s", row["name"])
            continue
        name = clean_contest_name(row["name"])
        if not name:
            log.warning("AtCoder: upcoming row %s has no usable name", slug)
            continue
        try:
            start = _parse_start(row["start"])
            duration = _parse_duration_min(row["duration"])
        except ValueError as exc:
            log.warning("AtCoder: skipping upcoming row %s: %s", slug, exc)
            continue
        contests.append(
            Contest(
                platform=Platform.ATCODER,
                external_id=f"AT-{slug}",
                name=name,
                start_utc=start,
                duration_min=duration,
                link=f"{ATCODER}/contests/{slug}",
            )
        )
    return contests


# ---------------------------------------------------------------------------
# Solves (kenkoooo)
# ---------------------------------------------------------------------------
def _kenkoooo_get(session: requests.Session, path: str, params: dict[str, Any] | None = None) -> Any | None:
    _limiter.wait()
    return get_json(session, "GET", f"{KENKOOKO}{path}", params=params)


def _fetch_all_submissions(session: requests.Session, user: str) -> list[dict[str, Any]] | None:
    """All AC-capable submissions from epoch 0. None on any failure (partial
    history would give a wrong first AC)."""
    collected: list[dict[str, Any]] = []
    from_second = 0
    for _ in range(MAX_PAGES):
        page = _kenkoooo_get(
            session,
            "/atcoder-api/v3/user/submissions",
            {"user": user, "from_second": from_second},
        )
        if not isinstance(page, list):
            return None
        collected.extend(page)
        if len(page) < PAGE_LIMIT:
            return collected
        last = max(int(s["epoch_second"]) for s in page)
        if last + 1 <= from_second:
            log.warning("AtCoder paging stalled at %s; aborting solves", from_second)
            return None
        from_second = last + 1
    log.warning("AtCoder paging exceeded %d pages; aborting solves", MAX_PAGES)
    return None


_SESSION_CACHE: "weakref.WeakKeyDictionary[Any, dict[str, Any]]" = weakref.WeakKeyDictionary()


def _cached(session: Any, name: str, loader: Callable[[Any], Any]) -> Any:
    """Call ``loader(session)`` once per session object and remember the result.

    The jobs create one session per run, so each reference file is fetched once per
    run. Sessions that cannot be weak-referenced are not cached.
    """
    try:
        store = _SESSION_CACHE.setdefault(session, {})
    except TypeError:
        return loader(session)
    if name not in store:
        store[name] = loader(session)
    return store[name]


def _fetch_problem_index(session: requests.Session) -> dict[str, dict[str, Any]] | None:
    rows = _kenkoooo_get(session, "/resources/problems.json")
    if not isinstance(rows, list):
        return None
    return {str(r["id"]): r for r in rows if "id" in r}


def _fetch_difficulties(session: requests.Session) -> dict[str, dict[str, Any]] | None:
    data = _kenkoooo_get(session, "/resources/problem-models.json")
    if not isinstance(data, dict):
        return None
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _fetch_contest_windows(session: requests.Session) -> dict[str, tuple[int, int]]:
    """``{contest_id: (start_epoch_second, end_epoch_second)}`` from kenkoooo.

    Returns an empty dict on any failure; every mode then falls back to Practice.
    """
    rows = _kenkoooo_get(session, "/resources/contests.json")
    if not isinstance(rows, list):
        return {}
    windows: dict[str, tuple[int, int]] = {}
    for row in rows:
        try:
            start = int(row["start_epoch_second"])
            duration = int(row["duration_second"])
            windows[str(row["id"])] = (start, start + duration)
        except (KeyError, TypeError, ValueError):
            continue
    return windows


def _is_heuristic(contest_id: str, pid: str) -> bool:
    """Heuristic (AHC) contests are optimisation contests; "AC" there is not a solve."""
    return contest_id.startswith("ahc") or pid.startswith("ahc")


def _mode_for(contest_id: str, ts: int, windows: dict[str, tuple[int, int]]) -> str:
    """Live Contest when the accepted submission falls inside its contest window
    (both ends inclusive), otherwise Practice."""
    window = windows.get(contest_id)
    if window is not None and window[0] <= ts <= window[1]:
        return Mode.LIVE
    return Mode.PRACTICE


def solves_from_submissions(
    submissions: list[dict[str, Any]],
    since_utc: datetime,
    user_offset: int,
    problems: dict[str, dict[str, Any]],
    models: dict[str, dict[str, Any]],
    contests: dict[str, tuple[int, int]] | None = None,
) -> list[Solve]:
    """Pure: first AC per problem at or after ``since_utc``, with attempts, rating
    and mode. Heuristic (ahc) problems are excluded entirely."""
    windows = contests or {}
    since_ts = since_utc.replace(tzinfo=timezone.utc).timestamp() if since_utc.tzinfo is None else since_utc.timestamp()
    ordered = sorted(submissions, key=lambda s: (int(s["epoch_second"]), int(s.get("id", 0))))
    by_problem: dict[str, list[dict[str, Any]]] = {}
    for sub in ordered:
        by_problem.setdefault(str(sub["problem_id"]), []).append(sub)

    solves: list[Solve] = []
    for pid, group in by_problem.items():
        pos = next((i for i, s in enumerate(group) if s.get("result") == "AC"), None)
        if pos is None:
            continue
        ac = group[pos]
        ts = int(ac["epoch_second"])
        if ts < since_ts:
            continue
        info = problems.get(pid, {})
        contest_id = str(info.get("contest_id") or ac.get("contest_id") or "")
        if _is_heuristic(contest_id, pid):
            continue
        raw = (models.get(pid) or {}).get("difficulty")
        solves.append(
            Solve(
                judge=Judge.ATCODER,
                problem_key=f"AT-{pid}",
                name=str(info.get("name") or info.get("title") or pid),
                link=f"{ATCODER}/contests/{contest_id}/tasks/{pid}",
                rating=clip_difficulty(raw, user_offset) if raw is not None else None,
                difficulty=None,
                tags=(),
                solved_at_utc=datetime.fromtimestamp(ts, tz=timezone.utc),
                mode=_mode_for(contest_id, ts, windows),
                contest_external_id=f"AT-{contest_id}" if contest_id else None,
                attempts=pos + 1,
            )
        )
    return sorted(solves, key=lambda s: s.solved_at_utc)


@guarded(lambda: None)
def fetch_solves_or_none(
    session: requests.Session,
    user: str,
    since_utc: datetime,
    offset: int,
) -> list[Solve] | None:
    """Like ``fetch_solves`` but None when the submission history could not be read."""
    submissions = _fetch_all_submissions(session, user)
    if submissions is None:
        return None
    problems = _cached(session, "problems", _fetch_problem_index) or {}
    models = _cached(session, "difficulties", _fetch_difficulties) or {}
    windows = _cached(session, "windows", _fetch_contest_windows) or {}
    return solves_from_submissions(submissions, since_utc, offset, problems, models, windows)


@guarded(list)
def fetch_solves(
    session: requests.Session,
    user: str,
    since_utc: datetime,
    offset: int,
) -> list[Solve]:
    """AtCoder first-AC solves since ``since_utc``.

    Heuristic (AHC) problems are excluded. Modes come from the contest windows in
    kenkoooo's contests.json. The three reference files are fetched once per
    session (per run), not once per member. If they cannot be fetched, the solves
    are still returned with the problem id as the name, ``rating=None`` and mode
    Practice. A failed submission history gives an empty list here.
    """
    return fetch_solves_or_none(session, user, since_utc, offset) or []


# ---------------------------------------------------------------------------
# Rating history
# ---------------------------------------------------------------------------
@guarded(list)
def fetch_rating_history(session: requests.Session, user: str) -> list[RatingChange]:
    """Rated AtCoder contests only (``IsRated`` true). Empty on failure."""
    return fetch_rating_history_or_none(session, user) or []


@guarded(lambda: None)
def fetch_rating_history_or_none(session: requests.Session, user: str) -> list[RatingChange] | None:
    """Like ``fetch_rating_history`` but None when the call failed."""
    rows = get_json(session, "GET", f"{ATCODER}/users/{quote(user, safe='')}/history/json")
    if not isinstance(rows, list):
        return None
    changes: list[RatingChange] = []
    for row in rows:
        if not row.get("IsRated"):
            continue
        end = datetime.fromisoformat(str(row["EndTime"]))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        place = row.get("Place")
        changes.append(
            RatingChange(
                platform=Platform.ATCODER,
                contest_name=str(row.get("ContestName", "")),
                date_utc=end.astimezone(timezone.utc),
                old=int(row["OldRating"]),
                new=int(row["NewRating"]),
                rank=int(place) if place is not None else None,
            )
        )
    return changes
