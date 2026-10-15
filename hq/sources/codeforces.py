"""Codeforces source client (public API, https://codeforces.com/apiHelp).

Rate limit: at least 2.1 s between calls, enforced by one module-level limiter
shared by every function here. Responses are ``{"status": "OK", "result": ...}``
or ``{"status": "FAILED", "comment": ...}``.

Solves: all submissions are paged through ``user.status`` (newest first) and
grouped per problem, so "first AC" and attempt counts are correct across the
full history. Results are then filtered to ``solved_at_utc >= since_utc``.
"""

from __future__ import annotations

import logging
import weakref
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import requests

from hq._net import RateLimiter, from_epoch, get_json, guarded
from hq.models import Contest, RatingChange, Solve
from hq.schema import Judge, Mode, Platform

log = logging.getLogger(__name__)

API = "https://codeforces.com/api"
GYM_ID_THRESHOLD = 100_000
PAGE_SIZE = 500  # count per user.status call (see contract; max not verified)
_limiter = RateLimiter(2.1)


def _cf(session: requests.Session, method: str, params: dict[str, Any] | None = None) -> Any | None:
    """Call one API method, throttled. Returns ``result`` on OK, else None."""
    _limiter.wait()
    body = get_json(session, "GET", f"{API}/{method}", params=params)
    if not isinstance(body, dict):
        return None
    if body.get("status") != "OK":
        log.warning("Codeforces %s failed: %s", method, body.get("comment"))
        return None
    return body.get("result")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Contests
# ---------------------------------------------------------------------------
@guarded(list)
def fetch_upcoming_contests(session: requests.Session) -> list[Contest]:
    """Non-gym contests in phase BEFORE (not yet started)."""
    rows = _cf(session, "contest.list", {"gym": "false"})
    if not isinstance(rows, list):
        return []
    contests: list[Contest] = []
    for row in rows:
        if row.get("phase") != "BEFORE" or row.get("startTimeSeconds") is None:
            continue
        cid = row["id"]
        contests.append(
            Contest(
                platform=Platform.CODEFORCES,
                external_id=f"CF-{cid}",
                name=row["name"],
                start_utc=from_epoch(row["startTimeSeconds"]),
                duration_min=int(row["durationSeconds"]) // 60,
                link=f"https://codeforces.com/contests/{cid}",
            )
        )
    return contests


# All contests (every phase) of the regular or the gym list, cached per session so one run
# asks Codeforces once per list. A failed call is not cached.
_contest_cache: "weakref.WeakKeyDictionary[requests.Session, dict[bool, list[Contest]]]" = weakref.WeakKeyDictionary()


@guarded(list)
def fetch_all_contests(session: requests.Session, gym: bool) -> list[Contest]:
    """Every contest of the regular (gym=False) or gym (gym=True) list, in any phase.
    external_id is "CF-<id>" or "GYM-<id>"; used to replace placeholder contest names."""
    cache = _contest_cache.setdefault(session, {})
    if gym in cache:
        return cache[gym]
    rows = _cf(session, "contest.list", {"gym": "true" if gym else "false"})
    if not isinstance(rows, list):
        return []
    prefix = "GYM" if gym else "CF"
    contests: list[Contest] = []
    for row in rows:
        if row.get("startTimeSeconds") is None or row.get("id") is None or not row.get("name"):
            continue
        cid = row["id"]
        contests.append(
            Contest(
                platform=Platform.CF_GYM if gym else Platform.CODEFORCES,
                external_id=f"{prefix}-{cid}",
                name=row["name"],
                start_utc=from_epoch(row["startTimeSeconds"]),
                duration_min=int(row.get("durationSeconds") or 0) // 60,
                link=f"https://codeforces.com/gym/{cid}" if gym else f"https://codeforces.com/contests/{cid}",
            )
        )
    cache[gym] = contests
    return contests


# ---------------------------------------------------------------------------
# Solves
# ---------------------------------------------------------------------------
def _fetch_all_submissions(session: requests.Session, handle: str) -> list[dict[str, Any]] | None:
    """Page through the full submission history. None if any page fails, so that
    a partial history never produces a wrong "first AC"."""
    collected: list[dict[str, Any]] = []
    start = 1
    while True:
        page = _cf(session, "user.status", {"handle": handle, "from": start, "count": PAGE_SIZE})
        if not isinstance(page, list):
            return None
        collected.extend(page)
        if len(page) < PAGE_SIZE:
            return collected
        start += PAGE_SIZE


def _mode_for(author: dict[str, Any] | None) -> str:
    participant = (author or {}).get("participantType", "")
    if participant == "CONTESTANT":
        return Mode.LIVE
    if participant == "VIRTUAL":
        return Mode.VIRTUAL
    return Mode.PRACTICE


def solves_from_submissions(
    submissions: list[dict[str, Any]],
    since_utc: datetime,
) -> list[Solve]:
    """Pure: first AC per problem, attempts, mode and gym mapping.

    Submissions are sorted ascending by creation time. A problem counts when its
    first OK verdict is at or after ``since_utc``.
    """
    ordered = sorted(submissions, key=lambda s: (s.get("creationTimeSeconds", 0), s.get("id", 0)))
    by_problem: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for sub in ordered:
        prob = sub.get("problem") or {}
        cid = prob.get("contestId", sub.get("contestId"))
        index = prob.get("index")
        if cid is None or index is None:
            continue
        by_problem[(int(cid), str(index))].append(sub)

    since_ts = _aware(since_utc).timestamp()
    solves: list[Solve] = []
    for (cid, index), group in by_problem.items():
        pos = next((i for i, s in enumerate(group) if s.get("verdict") == "OK"), None)
        if pos is None:
            continue
        ac = group[pos]
        if ac["creationTimeSeconds"] < since_ts:
            continue
        solves.append(_build_solve(ac, cid, index, attempts=pos + 1))
    return sorted(solves, key=lambda s: s.solved_at_utc)


def _build_solve(ac: dict[str, Any], cid: int, index: str, attempts: int) -> Solve:
    prob = ac.get("problem") or {}
    is_gym = cid >= GYM_ID_THRESHOLD
    if is_gym:
        judge = Judge.CF_GYM
        key = f"GYM-{cid}{index}"
        link = f"https://codeforces.com/gym/{cid}/problem/{index}"
        contest_key = f"GYM-{cid}"
    else:
        judge = Judge.CODEFORCES
        key = f"CF-{cid}{index}"
        link = f"https://codeforces.com/contest/{cid}/problem/{index}"
        contest_key = f"CF-{cid}"
    rating = prob.get("rating")
    return Solve(
        judge=judge,
        problem_key=key,
        name=prob.get("name", key),
        link=link,
        rating=int(rating) if rating is not None else None,
        difficulty=None,
        tags=tuple(str(t).lower() for t in prob.get("tags", [])),
        solved_at_utc=from_epoch(ac["creationTimeSeconds"]),
        mode=_mode_for(ac.get("author")),
        contest_external_id=contest_key,
        attempts=attempts,
    )


@guarded(lambda: None)
def fetch_solves_or_none(session: requests.Session, handle: str, since_utc: datetime) -> list[Solve] | None:
    """Like ``fetch_solves`` but returns None when the history could not be read,
    so the caller can tell "failed" apart from "no new solves"."""
    submissions = _fetch_all_submissions(session, handle)
    if submissions is None:
        return None
    return solves_from_submissions(submissions, since_utc)


@guarded(list)
def fetch_solves(session: requests.Session, handle: str, since_utc: datetime) -> list[Solve]:
    """First accepted solve per problem for ``handle``, first AC at or after ``since_utc``."""
    return fetch_solves_or_none(session, handle, since_utc) or []


# ---------------------------------------------------------------------------
# Ratings and users
# ---------------------------------------------------------------------------
@guarded(lambda: None)
def fetch_rating_history_or_none(session: requests.Session, handle: str) -> list[RatingChange] | None:
    """Like ``fetch_rating_history`` but None when the call failed."""
    rows = _cf(session, "user.rating", {"handle": handle})
    if not isinstance(rows, list):
        return None
    changes: list[RatingChange] = []
    for row in rows:
        rank = row.get("rank")
        changes.append(
            RatingChange(
                platform=Platform.CODEFORCES,
                contest_name=row.get("contestName", ""),
                date_utc=from_epoch(row["ratingUpdateTimeSeconds"]),
                old=int(row["oldRating"]),
                new=int(row["newRating"]),
                rank=int(rank) if rank is not None else None,
            )
        )
    return changes


@guarded(list)
def fetch_rating_history(session: requests.Session, handle: str) -> list[RatingChange]:
    return fetch_rating_history_or_none(session, handle) or []


@guarded(lambda: None)
def fetch_user_info_or_none(session: requests.Session, handles: list[str]) -> dict[str, dict] | None:
    """``{handle: {...}}`` for the handles the API knows, or None when the call failed."""
    if not handles:
        return {}
    rows = _cf(session, "user.info", {"handles": ";".join(handles)})
    if not isinstance(rows, list):
        return None
    wanted = {h.lower(): h for h in handles}
    info: dict[str, dict] = {}
    for row in rows:
        key = wanted.get(str(row.get("handle", "")).lower())
        if key is not None:
            info[key] = {"rating": row.get("rating"), "maxRating": row.get("maxRating")}
    return info


@guarded(dict)
def fetch_user_info(session: requests.Session, handles: list[str]) -> dict[str, dict]:
    """``{handle: {"rating": int|None, "maxRating": int|None}}`` for the requested
    handles (matched case-insensitively, keyed by the requested spelling)."""
    return fetch_user_info_or_none(session, handles) or {}


# problemset.problems is one large response: fetched once per session and shared by
# fetch_problemset (autotasks) and fetch_problem_counts (Total Problems).
_problemset_cache: "weakref.WeakKeyDictionary[requests.Session, dict]" = weakref.WeakKeyDictionary()


def _problemset_result(session: requests.Session) -> dict | None:
    if session in _problemset_cache:
        return _problemset_cache[session]
    result = _cf(session, "problemset.problems")
    if not isinstance(result, dict):
        return None
    _problemset_cache[session] = result
    return result


@guarded(dict)
def fetch_problem_counts(session: requests.Session) -> dict[int, int]:
    """Number of problems per regular contest id (from problemset.problems). Gym contests are
    not in the problemset and are not counted. {} when the call fails (not cached then)."""
    result = _problemset_result(session)
    if result is None:
        return {}
    counts: dict[int, int] = {}
    for prob in result.get("problems", []):
        cid = prob.get("contestId")
        if cid is None or prob.get("index") is None:
            continue
        counts[int(cid)] = counts.get(int(cid), 0) + 1
    return counts


@guarded(list)
def fetch_problemset(session: requests.Session) -> list[dict]:
    """All problems in the public problemset (no gym)."""
    result = _problemset_result(session)
    if result is None:
        return []
    items: list[dict] = []
    for prob in result.get("problems", []):
        if "contestId" not in prob or "index" not in prob:
            continue
        cid, index = prob["contestId"], prob["index"]
        items.append(
            {
                "key": f"CF-{cid}{index}",
                "name": prob.get("name", ""),
                "link": f"https://codeforces.com/problemset/problem/{cid}/{index}",
                "rating": prob.get("rating"),
                "tags": [str(t).lower() for t in prob.get("tags", [])],
            }
        )
    return items
