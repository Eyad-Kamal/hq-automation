"""In-memory doubles for the B3 jobs: a Notion client, source clients and a seeded HQ.

Nothing here touches the network. ``FakeNotion`` implements the subset of
``hq.notion.Notion`` that the jobs use (query, create_page, update_page,
append_blocks, create_comment). It stores property values in the same builder
shape that ``hq.notion.p_*`` produces.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from hq import notion as nt
from hq import schema as S
from hq.config import load as load_config
from hq.notion import NotionError
from hq.jobs import Context
from hq.models import Contest, RatingChange, Solve
from hq.repo import Repo

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)  # Thu 15 Oct 2026 (Cairo: same day)
NOW_SATURDAY = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)  # Sat 17 Oct 2026 (after 00:10 Cairo)


class FakeNotion:
    """In-memory Notion. Records every write so tests can assert on counts."""

    def __init__(self) -> None:
        self.ids: dict[str, str] = {title: f"ds-{i}" for i, title in enumerate(S.DATABASE_TITLES)}
        self.tables: dict[str, list[dict[str, Any]]] = {ds: [] for ds in self.ids.values()}
        self.creates: list[tuple[str, str]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.comments: list[tuple[str, list[dict[str, Any]]]] = []
        self.appends: list[tuple[str, list[dict[str, Any]]]] = []
        self.blocks: dict[str, list[dict[str, Any]]] = {}
        self.queries = 0
        self.create_faults: list[str] = []  # "fail" | "commit_then_fail", consumed per create
        self.query_log: list[str] = []
        self.child_pages: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.child_icons: list[Any] = []  # icon sent with each child page create
        self.icon_updates: list[tuple[str, dict[str, Any]]] = []  # (page id, icon) for icon-only patches
        self.views: list[dict[str, Any]] = []
        self.view_faults: set[str] = set()  # view types the fake rejects with HTTP 400
        self._n = 0

    # -- Notion client surface ------------------------------------------------
    def query(self, ds_id: str, filter: Any = None, sorts: Any = None) -> list[dict[str, Any]]:  # noqa: A002
        if filter is not None:
            raise NotImplementedError("FakeNotion does not evaluate filters; the jobs filter in Python")
        self.queries += 1
        self.query_log.append(ds_id)
        return copy.deepcopy(self.tables[ds_id])

    def create_page(self, ds_id: str, properties: dict[str, Any], children: Any = None, icon: Any = None) -> dict[str, Any]:
        self._n += 1
        page_id = f"page-{self._n:05d}"
        fault = self.create_faults.pop(0) if self.create_faults else None
        if fault == "fail":
            raise NotionError("POST /pages -> HTTP 500 (injected: nothing stored)")
        page = {"id": page_id, "parent": {"type": "data_source_id", "data_source_id": ds_id}, "properties": copy.deepcopy(properties), "icon": copy.deepcopy(icon)}
        self.tables[ds_id].append(page)
        self.creates.append((ds_id, page_id))
        if children:
            self.blocks.setdefault(page_id, []).extend(copy.deepcopy(list(children)))
        if fault == "commit_then_fail":
            raise NotionError("POST /pages timed out after the row was stored (injected)")
        return copy.deepcopy(page)

    def block_children(self, block_id: str, page_size: int = 100) -> list[dict[str, Any]]:
        return [{"id": f"{block_id}-b{i}"} for i in range(len(self.blocks.get(block_id, [])))][:page_size]

    def update_page(self, page_id: str, properties: dict[str, Any] | None = None, *, icon: Any = None) -> dict[str, Any]:
        page = self.page(page_id)
        if page is None:
            raise KeyError(page_id)
        if properties:
            page["properties"].update(copy.deepcopy(properties))
            self.updates.append((page_id, copy.deepcopy(properties)))
        if icon is not None:
            page["icon"] = copy.deepcopy(icon)
            self.icon_updates.append((page_id, copy.deepcopy(icon)))
        return copy.deepcopy(page)

    def get_page(self, page_id: str) -> dict[str, Any]:
        page = self.page(page_id)
        if page is None:
            raise KeyError(page_id)
        return copy.deepcopy(page)

    def append_blocks(self, page_id: str, children: Any) -> dict[str, Any]:
        self.appends.append((page_id, copy.deepcopy(list(children))))
        self.blocks.setdefault(page_id, []).extend(copy.deepcopy(list(children)))
        return {}

    def create_comment(self, page_id: str, rich_text: list[dict[str, Any]]) -> dict[str, Any]:
        self.comments.append((page_id, copy.deepcopy(rich_text)))
        return {"id": f"comment-{len(self.comments)}"}

    def retrieve_data_source(self, ds_id: str) -> dict[str, Any]:
        # Property ids are the name with a prefix; enough for the dashboard code under test.
        names: set[str] = set()
        for row in self.tables.get(ds_id, []):
            names.update(row.get("properties", {}).keys())
        return {"id": ds_id, "properties": {n: {"id": f"id-{n}", "type": "x"} for n in sorted(names)}}

    def create_child_page(self, parent_page_id: str, title: str, children: Any = None, icon: Any = None) -> dict[str, Any]:
        self._n += 1
        page_id = f"page-{self._n:05d}"
        self.child_pages.append((parent_page_id, title, copy.deepcopy(list(children or []))))
        self.child_icons.append(copy.deepcopy(icon))
        if children:
            self.blocks.setdefault(page_id, []).extend(copy.deepcopy(list(children)))
        return {"id": page_id, "url": f"https://www.notion.so/{page_id}"}

    def create_view(self, body: dict[str, Any]) -> dict[str, Any]:
        if "*" in self.view_faults:
            from hq.notion import NotionError as _NE

            raise _NE("POST /views -> HTTP 500 (injected)")
        if self.view_faults and body.get("type") in self.view_faults:
            from hq.notion import NotionError as _NE
            raise _NE("POST /views -> HTTP 400 (injected)")
        self.views.append(copy.deepcopy(body))
        return {"id": f"view-{len(self.views)}"}

    # -- test helpers -----------------------------------------------------------
    def page(self, page_id: str) -> dict[str, Any] | None:
        for rows in self.tables.values():
            for row in rows:
                if row["id"] == page_id:
                    return row
        return None

    def rows(self, title: str) -> list[dict[str, Any]]:
        return self.tables[self.ids[title]]

    def seed(self, title: str, properties: dict[str, Any]) -> str:
        return self.create_page(self.ids[title], properties)["id"]

    def write_total(self) -> int:
        return len(self.creates) + len(self.updates) + len(self.comments) + len(self.appends) + len(self.icon_updates)

    def reset_writes(self) -> None:
        self.creates.clear()
        self.updates.clear()
        self.comments.clear()
        self.appends.clear()
        self.icon_updates.clear()


@dataclass
class FakeSources:
    """Canned platform data. Each method mirrors the matching ``LiveSources`` method."""

    upcoming: list[Contest] = field(default_factory=list)
    cf: dict[str, list[Solve]] = field(default_factory=dict)
    cf_hist: dict[str, list[RatingChange]] = field(default_factory=dict)
    cf_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    at: dict[str, list[Solve]] = field(default_factory=dict)
    at_hist: dict[str, list[RatingChange]] = field(default_factory=dict)
    lc: dict[str, list[Solve]] = field(default_factory=dict)
    lc_daily: Any = None  # a leetcode.DailyQuestion; None means the daily fetch failed
    problemset: list[dict[str, Any]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    failing: set[str] = field(default_factory=set)  # handles whose calls "fail"
    contests: dict[str, Contest] = field(default_factory=dict)  # external id -> real contest (contest.list)
    problem_counts: dict[int, int] = field(default_factory=dict)  # regular contest id -> problem count
    count_calls: list[str] = field(default_factory=list)

    def _fails(self, handle: str, strict: bool) -> bool:
        return handle in self.failing

    def upcoming_contests(self) -> list[Contest]:
        self.calls.append("upcoming")
        return list(self.upcoming)

    def cf_solves(self, handle: str, since: datetime, *, strict: bool = False) -> list[Solve] | None:
        self.calls.append(f"cf_solves:{handle}")
        if self._fails(handle, strict):
            return None if strict else []
        return [s for s in self.cf.get(handle, []) if s.solved_at_utc >= since]

    def cf_rating_history(self, handle: str, *, strict: bool = False) -> list[RatingChange] | None:
        if self._fails(handle, strict):
            return None if strict else []
        return list(self.cf_hist.get(handle, []))

    def cf_user_info(self, handles: list[str], *, strict: bool = False) -> dict[str, dict[str, Any]] | None:
        if any(h in self.failing for h in handles):
            # A failing batch: strict callers see None; lenient callers see nothing.
            return None if strict else {}
        return {h: self.cf_info[h] for h in handles if h in self.cf_info}

    def cf_problemset(self) -> list[dict[str, Any]]:
        self.calls.append("problemset")
        return list(self.problemset)

    def cf_contest(self, external_id: str) -> Contest | None:
        self.calls.append(f"cf_contest:{external_id}")
        return self.contests.get(external_id)

    def cf_problem_count(self, external_id: str) -> int | None:
        if not external_id.startswith("CF-") or not external_id[3:].isdigit():
            return None  # gym: no lookup
        self.count_calls.append(external_id)
        return self.problem_counts.get(int(external_id[3:]))

    def at_solves(self, user: str, since: datetime, offset: int, *, strict: bool = False) -> list[Solve] | None:
        if self._fails(user, strict):
            return None if strict else []
        return [s for s in self.at.get(user, []) if s.solved_at_utc >= since]

    def at_rating_history(self, user: str, *, strict: bool = False) -> list[RatingChange] | None:
        if self._fails(user, strict):
            return None if strict else []
        return list(self.at_hist.get(user, []))

    def lc_solves(self, username: str, since: datetime, *, strict: bool = False) -> list[Solve] | None:
        if self._fails(username, strict):
            return None if strict else []
        return [s for s in self.lc.get(username, []) if s.solved_at_utc >= since]

    def lc_daily_question(self) -> Any:
        """``lc_daily`` as set by the test; None (a failed fetch) by default."""
        self.calls.append("lc_daily")
        return self.lc_daily


def cf_solve(
    index: str,
    when: datetime,
    *,
    rating: int | None = 1000,
    tags: tuple[str, ...] = ("greedy",),
    mode: str = "Practice",
    contest: str | None = None,
    name: str | None = None,
    judge: str = "Codeforces",
) -> Solve:
    prefix = "GYM" if judge == "CF Gym" else "CF"
    return Solve(
        judge=judge,
        problem_key=f"{prefix}-{index}",
        name=name or f"Problem {index}",
        link=f"https://codeforces.com/problemset/problem/{index[:-1]}/{index[-1]}",
        rating=rating,
        difficulty=None,
        tags=tags,
        solved_at_utc=when,
        mode=mode,
        contest_external_id=contest,
        attempts=1,
    )


def hours_from_now(hours: float, now: datetime = NOW) -> datetime:
    return now + timedelta(hours=hours)


@dataclass
class World:
    notion: FakeNotion
    sources: FakeSources
    ids: dict[str, str]
    levels: dict[str, str]
    topics: dict[str, str]
    team: str
    eyad: str  # active L1 member in Team Alpha, has a Person, email and CF handle
    sara: str  # active L1 member, CF handle, no team, no person
    old: str  # inactive member

    def context(self, *, dry_run: bool = False, force: bool = False, member: str | None = None, now: datetime = NOW) -> Context:
        repo = Repo(self.notion, self.ids, dry_run=dry_run)
        cfg = load_config(repo)
        return Context(
            repo=repo,
            sources=self.sources,
            now=now,
            cfg=cfg,
            mail=None,
            force=force,
            member_filter=member,
        )


def build_world(*, subscriptions: list[str] | None = None) -> World:
    """A small club: two active trainees (one in a team, one not), one inactive member,
    two L1 topics and one L2 topic, and default Config (no rows)."""
    notion = FakeNotion()
    sources = FakeSources()
    ids = dict(notion.ids)

    level1 = notion.seed(
        S.DB_LEVELS,
        {
            S.P_Levels.TITLE: nt.p_title("L1 · Foundations"),
            S.P_Levels.LEVEL_NO: nt.p_number(1),
            S.P_Levels.BAND_LOW: nt.p_number(800),
            S.P_Levels.BAND_HIGH: nt.p_number(1199),
        },
    )
    level2 = notion.seed(
        S.DB_LEVELS,
        {
            S.P_Levels.TITLE: nt.p_title("L2 · Core"),
            S.P_Levels.LEVEL_NO: nt.p_number(2),
            S.P_Levels.BAND_LOW: nt.p_number(1200),
            S.P_Levels.BAND_HIGH: nt.p_number(1599),
        },
    )
    greedy = notion.seed(
        S.DB_TOPICS,
        {
            S.P_Topics.TITLE: nt.p_title("Greedy & Constructive"),
            S.P_Topics.ORDER: nt.p_number(1),
            S.P_Topics.BAND_LOW: nt.p_number(800),
            S.P_Topics.BAND_HIGH: nt.p_number(1199),
            S.P_Topics.CORE: nt.p_checkbox(True),
            S.P_Topics.CF_TAGS: nt.p_multi(["greedy", "constructive algorithms"]),
            S.P_Topics.LEVEL: nt.p_relation([level1]),
        },
    )
    strings = notion.seed(
        S.DB_TOPICS,
        {
            S.P_Topics.TITLE: nt.p_title("Strings & Parsing"),
            S.P_Topics.ORDER: nt.p_number(2),
            S.P_Topics.BAND_LOW: nt.p_number(800),
            S.P_Topics.BAND_HIGH: nt.p_number(1199),
            S.P_Topics.CORE: nt.p_checkbox(False),
            S.P_Topics.CF_TAGS: nt.p_multi(["strings"]),
            S.P_Topics.LEVEL: nt.p_relation([level1]),
        },
    )
    dp = notion.seed(
        S.DB_TOPICS,
        {
            S.P_Topics.TITLE: nt.p_title("DP Basics"),
            S.P_Topics.ORDER: nt.p_number(3),
            S.P_Topics.BAND_LOW: nt.p_number(1200),
            S.P_Topics.BAND_HIGH: nt.p_number(1599),
            S.P_Topics.CORE: nt.p_checkbox(True),
            S.P_Topics.CF_TAGS: nt.p_multi(["dp"]),
            S.P_Topics.LEVEL: nt.p_relation([level2]),
        },
    )
    team = notion.seed(S.DB_TEAMS, {S.P_Teams.TITLE: nt.p_title("Team Alpha"), S.P_Teams.TEAM_TYPE: nt.p_select("ICPC Trio")})
    eyad = notion.seed(
        S.DB_MEMBERS,
        {
            S.P_Members.TITLE: nt.p_title("Eyad"),
            S.P_Members.PERSON: nt.p_people(["user-eyad"]),
            S.P_Members.ACTIVE: nt.p_checkbox(True),
            S.P_Members.CF_HANDLE: nt.p_text("eyad_cf"),
            S.P_Members.EMAIL: nt.p_email("eyad@example.test"),
            S.P_Members.LEVEL: nt.p_relation([level1]),
            S.P_Members.TEAM: nt.p_relation([team]),
            S.P_Members.CONTEST_SUBSCRIPTIONS: nt.p_multi(
                subscriptions if subscriptions is not None else ["CF Div. 2", "AtCoder ABC"]
            ),
        },
    )
    sara = notion.seed(
        S.DB_MEMBERS,
        {
            S.P_Members.TITLE: nt.p_title("Sara"),
            S.P_Members.ACTIVE: nt.p_checkbox(True),
            S.P_Members.CF_HANDLE: nt.p_text("sara_cf"),
            S.P_Members.LEVEL: nt.p_relation([level1]),
            S.P_Members.CONTEST_SUBSCRIPTIONS: nt.p_multi(["CF Div. 3"]),
        },
    )
    old = notion.seed(
        S.DB_MEMBERS,
        {
            S.P_Members.TITLE: nt.p_title("Former"),
            S.P_Members.ACTIVE: nt.p_checkbox(False),
            S.P_Members.CF_HANDLE: nt.p_text("former_cf"),
            S.P_Members.LEVEL: nt.p_relation([level1]),
        },
    )
    notion.reset_writes()
    # Every real CF handle has a user.info row (unrated users have null ratings).
    sources.cf_info["sara_cf"] = {"rating": None, "maxRating": None}
    return World(
        notion=notion,
        sources=sources,
        ids=ids,
        levels={"L1": level1, "L2": level2},
        topics={"greedy": greedy, "strings": strings, "dp": dp},
        team=team,
        eyad=eyad,
        sara=sara,
        old=old,
    )


def enable_practice(w: World) -> None:
    """Turn on the automatic Topic Practice tasks (Config AUTO_PRACTICE_TASKS = 1; the default is 0).
    Call before the first job run of the test."""
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("AUTO_PRACTICE_TASKS"), S.P_Config.NUMBER: nt.p_number(1)})


def contest(name: str, start: datetime, *, platform: str = "Codeforces", duration: int = 120, ext: str | None = None) -> Contest:
    external = ext or "CF-" + "".join(ch for ch in name if ch.isalnum())[:12]
    return Contest(
        platform=platform,
        external_id=external,
        name=name,
        start_utc=start,
        duration_min=duration,
        link=f"https://codeforces.com/contests/{external}",
        rated=True,
    )


def seed_eyad_solves(w: World, *, seed_contest: bool = False, rating_1999b: int = 1300) -> None:
    """Eyad's CF history used by the import, recompute and weekly tests.

    * 1985C: live in CF-1985, rated 1200, greedy, solved two days before NOW.
    * 1990A: practice solve of a problem from CF-1985, rated 800, strings, one day
      before NOW (an upsolve, because Eyad was live in CF-1985 earlier).
    * 1999B: plain practice, rated 1300, dp, one day before NOW.
    * Rating history: 1100 -> 1150 for CF-1985 (three days before NOW).
    """
    w.sources.cf["eyad_cf"] = [
        cf_solve("1985C", NOW - timedelta(days=2), rating=1200, tags=("greedy",), mode="Live Contest", contest="CF-1985", name="Permutation Swap"),
        cf_solve("1990A", NOW - timedelta(days=1), rating=800, tags=("strings",), mode="Practice", contest="CF-1985", name="Trust Nobody"),
        cf_solve("1999B", NOW - timedelta(days=1), rating=rating_1999b, tags=("dp",), mode="Practice", name="Dp Task"),
    ]
    w.sources.cf_hist["eyad_cf"] = [
        RatingChange("Codeforces", "Codeforces Round 1985 (Div. 2)", NOW - timedelta(days=3), 1100, 1150, 300),
    ]
    w.sources.cf_info["eyad_cf"] = {"rating": 1150, "maxRating": 1150}
    if seed_contest:
        w.notion.seed(
            S.DB_CONTESTS,
            {
                S.P_Contests.TITLE: nt.p_title("Codeforces Round 1985 (Div. 2)"),
                S.P_Contests.PLATFORM: nt.p_select("Codeforces"),
                S.P_Contests.KIND: nt.p_select("CF Div. 2"),
                S.P_Contests.START: nt.p_date(NOW - timedelta(days=2)),
                S.P_Contests.DURATION_MIN: nt.p_number(120),
                S.P_Contests.PHASE: nt.p_select("Finished"),
                S.P_Contests.EXTERNAL_ID: nt.p_text("CF-1985"),
                S.P_Contests.TASKS_CREATED: nt.p_checkbox(True),
                S.P_Contests.ALERT_SENT: nt.p_checkbox(True),
            },
        )
    w.notion.reset_writes()
