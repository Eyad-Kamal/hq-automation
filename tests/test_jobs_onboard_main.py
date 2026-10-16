"""Onboard, dry-run, the auto sequence, Sync Log behaviour, config/notify helpers and the CLI."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from hq import config, notify
from hq import main as cli
from hq import notion as nt
from hq import schema as S
from hq.jobs import ERROR, OK, JobResult, execute, onboard
from hq.repo import get_date, get_multi, get_number, get_relation, get_select, get_status, get_text
from tests.fakes import NOW, NOW_SATURDAY, build_world, seed_eyad_solves
from tests.fakes import World


def _lq_for(w: World, member_id: str):
    return [r for r in w.notion.rows(S.DB_LEARNING_QUEUE) if get_relation(r, S.P_LearningQueue.MEMBER) == [member_id]]


def _lq_row(w: World, member_id: str, topic_id: str):
    matches = [r for r in _lq_for(w, member_id) if get_relation(r, S.P_LearningQueue.TOPIC) == [topic_id]]
    assert len(matches) == 1
    return matches[0]


# ---------------------------------------------------------------------------
# onboard
# ---------------------------------------------------------------------------
def test_onboard_creates_one_queue_row_per_topic_and_member() -> None:
    w = build_world()
    result = onboard.run(w.context())
    assert result.result == OK
    assert len(_lq_for(w, w.eyad)) == 3
    assert len(_lq_for(w, w.sara)) == 3
    assert _lq_for(w, w.old) == [], "inactive members get no queue"

    greedy = _lq_row(w, w.eyad, w.topics["greedy"])
    assert get_status(greedy, S.P_LearningQueue.QUEUE_STATUS) == "Queued"
    assert get_select(greedy, S.P_LearningQueue.PRIORITY) == "Important", "core topic of the member's level"
    assert get_select(_lq_row(w, w.eyad, w.topics["strings"]), S.P_LearningQueue.PRIORITY) == "Optional"
    assert get_select(_lq_row(w, w.eyad, w.topics["dp"]), S.P_LearningQueue.PRIORITY) == "Optional", "L2 topic for an L1 member"
    assert greedy["properties"][S.P_LearningQueue.TITLE]["title"][0]["text"]["content"] == "Greedy & Constructive · Eyad"


def test_onboard_is_idempotent_and_fills_defaults_once() -> None:
    w = build_world()
    onboard.run(w.context())
    count = len(w.notion.rows(S.DB_LEARNING_QUEUE))
    eyad = w.notion.page(w.eyad)
    assert get_date(eyad, S.P_Members.JOINED) == NOW.date()
    assert get_number(eyad, S.P_Members.DAILY_TARGET) == 3

    w.notion.reset_writes()
    onboard.run(w.context(now=NOW + timedelta(days=3)))
    assert len(w.notion.rows(S.DB_LEARNING_QUEUE)) == count
    assert w.notion.creates == []
    assert get_date(w.notion.page(w.eyad), S.P_Members.JOINED) == NOW.date(), "Joined is not overwritten"


def test_onboard_keeps_existing_subscriptions_and_gives_level_defaults() -> None:
    w = build_world()
    onboard.run(w.context())
    assert get_multi(w.notion.page(w.eyad), S.P_Members.CONTEST_SUBSCRIPTIONS) == ["CF Div. 2", "AtCoder ABC"]

    newcomer = w.notion.seed(S.DB_MEMBERS, {S.P_Members.TITLE: nt.p_title("Newbie"), S.P_Members.ACTIVE: nt.p_checkbox(True)})
    l2 = w.levels["L2"]
    levelled = w.notion.seed(
        S.DB_MEMBERS,
        {
            S.P_Members.TITLE: nt.p_title("Levelled"),
            S.P_Members.ACTIVE: nt.p_checkbox(True),
            S.P_Members.LEVEL: nt.p_relation([l2]),
        },
    )
    onboard.run(w.context())
    assert get_multi(w.notion.page(newcomer), S.P_Members.CONTEST_SUBSCRIPTIONS) == [
        "CF Div. 4",
        "CF Div. 3",
        "AtCoder ABC",
    ], "no level counts as L1"
    assert get_multi(w.notion.page(levelled), S.P_Members.CONTEST_SUBSCRIPTIONS) == [
        "CF Div. 3",
        "CF Div. 2",
        "CF Educational",
        "AtCoder ABC",
    ]


# ---------------------------------------------------------------------------
# dry-run and the auto sequence
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing_for_every_job() -> None:
    from tests.test_jobs_daily_weekly import PROBLEMSET

    w = build_world()
    seed_eyad_solves(w)
    w.sources.problemset = list(PROBLEMSET)
    w.notion.reset_writes()
    w.sources.upcoming = []
    ctx = w.context(dry_run=True, force=True, now=NOW_SATURDAY)
    results = cli.run_auto(ctx)
    assert {r.job for r in results} >= {"onboard", "contests", "alerts", "imports", "recompute", "daily", "weekly"}
    assert w.notion.write_total() == 0, "dry-run must not write"
    assert w.notion.queries > 0, "dry-run still reads Notion"
    assert len(ctx.repo.write_log) > 10, "the intended writes are recorded"


def test_auto_runs_jobs_in_order_and_respects_due_checks() -> None:
    w = build_world()
    config.set_state(w.context().repo, config.STATE_LAST_WEEKLY, "2026-10-10")  # weekly not due this week
    first = [r.job for r in cli.run_auto(w.context(now=NOW))]
    assert first == ["onboard", "contests", "alerts", "imports", "recompute", "daily", "dashboards", "cleanup"]

    second = [r.job for r in cli.run_auto(w.context(now=NOW + timedelta(minutes=5)))]
    assert second == ["onboard", "contests", "alerts", "dashboards", "cleanup"], "imports (25 min guard) and daily (once a day) are not due"


def test_auto_runs_weekly_after_saturday() -> None:
    w = build_world()
    config.set_state(w.context().repo, config.STATE_LAST_WEEKLY, "2026-10-10")
    names = [r.job for r in cli.run_auto(w.context(now=NOW_SATURDAY))]
    assert names[-2:] == ["weekly", "cleanup"], "cleanup is the last job of the auto sequence"


# ---------------------------------------------------------------------------
# Sync Log and failure isolation
# ---------------------------------------------------------------------------
def test_sync_log_row_per_job_with_the_sync_log_job_name() -> None:
    w = build_world()
    result = execute(w.context(now=NOW), "imports", lambda ctx: JobResult(job="imports", result=OK, items=4, details="fine"))
    assert result.items == 4
    rows = w.notion.rows(S.DB_SYNC_LOG)
    assert len(rows) == 1
    assert get_select(rows[0], S.P_SyncLog.JOB) == "import", "the Sync Log option is 'import'"
    assert get_select(rows[0], S.P_SyncLog.RESULT) == "OK"
    assert get_number(rows[0], S.P_SyncLog.ITEMS) == 4
    assert rows[0]["properties"][S.P_SyncLog.TITLE]["title"][0]["text"]["content"].startswith("import 2026-10-15")
    assert w.notion.ids[S.DB_SYNC_LOG] not in w.notion.query_log, "writing a log row never reads the Sync Log table"


def test_a_crashing_job_is_logged_as_error_and_does_not_raise() -> None:
    w = build_world()

    def boom(ctx):
        raise ValueError("kaboom")

    result = execute(w.context(now=NOW), "contests", boom)
    assert result.result == ERROR
    assert "kaboom" in result.details
    row = w.notion.rows(S.DB_SYNC_LOG)[0]
    assert get_select(row, S.P_SyncLog.RESULT) == "Error"


def test_sync_log_details_are_capped_to_notion_text_limit() -> None:
    w = build_world()
    execute(w.context(now=NOW), "daily", lambda ctx: JobResult(job="daily", result=OK, items=1, details="x" * 5000))
    text = get_text(w.notion.rows(S.DB_SYNC_LOG)[0], S.P_SyncLog.DETAILS)
    assert len(text) == 1900


# ---------------------------------------------------------------------------
# Config and state
# ---------------------------------------------------------------------------
def test_config_rows_override_defaults_and_state_round_trips() -> None:
    w = build_world()
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("ALERT_MINUTES"), S.P_Config.NUMBER: nt.p_number(45)})
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("ATCODER_OFFSET"), S.P_Config.VALUE: nt.p_text("-200")})
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("TIMEZONE"), S.P_Config.VALUE: nt.p_text("Africa/Cairo")})
    ctx = w.context()
    assert ctx.cfg["ALERT_MINUTES"] == 45
    assert ctx.cfg["ATCODER_OFFSET"] == -200.0
    assert ctx.cfg["TIER_COMPETENT"] == 12, "missing keys fall back to the spec defaults"
    assert ctx.cfg["TIMEZONE"] == "Africa/Cairo"

    config.set_state(w.context().repo, config.STATE_LAST_DAILY, "2026-10-15")
    config.set_state(w.context().repo, config.STATE_LAST_DAILY, "2026-10-16")
    state_rows = [r for r in w.notion.rows(S.DB_CONFIG) if r["properties"][S.P_Config.TITLE]["title"][0]["text"]["content"] == config.STATE_LAST_DAILY]
    assert len(state_rows) == 1, "set_state updates the same row"
    assert config.state_date(w.context().repo, config.STATE_LAST_DAILY) == date(2026, 10, 16)


def test_config_non_numeric_value_for_numeric_key_falls_back_to_default() -> None:
    w = build_world()
    w.notion.seed(S.DB_CONFIG, {S.P_Config.TITLE: nt.p_title("ALERT_MINUTES"), S.P_Config.VALUE: nt.p_text("soon")})
    assert w.context().cfg["ALERT_MINUTES"] == 30


# ---------------------------------------------------------------------------
# Notify
# ---------------------------------------------------------------------------
def test_smtp_settings_require_host_user_and_password() -> None:
    assert notify.smtp_settings({}) is None
    assert notify.smtp_settings({"SMTP_HOST": "smtp.example.test", "SMTP_USER": "u"}) is None
    settings = notify.smtp_settings({"SMTP_HOST": "smtp.example.test", "SMTP_USER": "u", "SMTP_PASS": "p"})
    assert settings is not None
    assert (settings.port, settings.sender) == (587, "u")
    assert notify.make_mailer(None) is None


def test_comment_rich_text_mentions_each_user_once() -> None:
    items = notify.comment_rich_text("hello", ["user-a", "user-a", "user-b", ""])
    mentions = [i["mention"]["user"]["id"] for i in items if i["type"] == "mention"]
    assert mentions == ["user-a", "user-b"]
    assert items[0]["text"]["content"] == "hello"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_arguments() -> None:
    args = cli.parse_args([])
    assert (args.job, args.force, args.dry_run, args.member) == ("auto", False, False, None)
    args = cli.parse_args(["import", "--force", "--dry-run", "--member", "Eyad"])
    assert (args.job, args.force, args.dry_run, args.member) == ("import", True, True, "Eyad")
    assert cli.ALIASES["import"] == "imports"
    with pytest.raises(SystemExit):
        cli.parse_args(["nope"])


def test_cli_exits_2_without_a_token(capsys) -> None:
    assert cli.main(["auto"], env={}) == 2
    assert "NOTION_TOKEN" in capsys.readouterr().err


def test_cli_exits_2_without_data_source_ids(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("hq.notion.IDS_FILE", tmp_path / "missing.json")
    assert cli.main(["auto"], env={"NOTION_TOKEN": "secret-token-value"}) == 2
    assert "data source ids" in capsys.readouterr().err


def test_live_sources_call_each_platform_client(monkeypatch) -> None:
    calls: list[str] = []
    for name in ("fetch_upcoming_contests",):
        for module, label in ((cli.codeforces, "cf"), (cli.atcoder, "at"), (cli.leetcode, "lc")):
            monkeypatch.setattr(module, name, lambda session, _label=label: calls.append(_label) or [])
    sources = cli.LiveSources(session=object())
    assert sources.upcoming_contests() == []
    assert calls == ["cf", "at", "lc"]
