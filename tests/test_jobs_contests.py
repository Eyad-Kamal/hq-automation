"""Jobs contests and alerts: upsert idempotency, subscription targeting, alert window."""

from __future__ import annotations

from datetime import timedelta

from hq import schema as S
from hq.jobs import alerts, contests
from hq.repo import get_checkbox, get_relation, get_status
from tests.fakes import NOW, build_world, contest, hours_from_now


def _contest_rows(w, name_part: str = ""):
    return [r for r in w.notion.rows(S.DB_CONTESTS)]


def test_contest_upsert_is_idempotent_across_runs() -> None:
    w = build_world()
    w.sources.upcoming = [contest("Codeforces Round 1000 (Div. 2)", hours_from_now(20), ext="CF-2050")]

    first = contests.run(w.context())
    assert first.result == "OK"
    assert len(_contest_rows(w)) == 1

    w.notion.reset_writes()
    second = contests.run(w.context())
    assert second.result == "OK"
    assert len(_contest_rows(w)) == 1, "a rerun must not duplicate the contest"
    assert w.notion.creates == [], "a rerun with unchanged data must create nothing"
    assert w.notion.updates == [], "a rerun with unchanged data must update nothing"


def test_contest_tasks_only_for_subscribed_members_and_once() -> None:
    w = build_world()
    w.sources.upcoming = [contest("Codeforces Round 1000 (Div. 2)", hours_from_now(20), ext="CF-2050")]
    contests.run(w.context())

    tasks = w.notion.rows(S.DB_TASKS)
    assert len(tasks) == 1, "only Eyad subscribes to CF Div. 2; Sara subscribes to Div. 3"
    task = tasks[0]
    assert get_relation(task, S.P_Tasks.ASSIGNEE) == [w.eyad]
    assert task["properties"][S.P_Tasks.TITLE]["title"][0]["text"]["content"] == "🏆 Codeforces Round 1000 (Div. 2)"
    assert get_status(task, S.P_Tasks.STATUS) == "Planned"
    assert task["properties"][S.P_Tasks.SOURCE_KEY]["rich_text"][0]["text"]["content"] == f"contest:CF-2050:{w.eyad}"

    contest_row = w.notion.rows(S.DB_CONTESTS)[0]
    assert get_checkbox(contest_row, S.P_Contests.TASKS_CREATED) is True

    w.notion.reset_writes()
    contests.run(w.context())
    assert len(w.notion.rows(S.DB_TASKS)) == 1
    assert w.notion.creates == []


def test_no_contest_tasks_outside_the_lead_window() -> None:
    w = build_world()
    w.sources.upcoming = [contest("Codeforces Round 1001 (Div. 2)", hours_from_now(30), ext="CF-2051")]
    contests.run(w.context())
    assert w.notion.rows(S.DB_TASKS) == []
    assert get_checkbox(w.notion.rows(S.DB_CONTESTS)[0], S.P_Contests.TASKS_CREATED) is False


def test_phase_is_updated_for_existing_rows_not_listed_any_more() -> None:
    w = build_world()
    w.sources.upcoming = [contest("Codeforces Round 1002 (Div. 2)", hours_from_now(2), ext="CF-2052")]
    contests.run(w.context())
    w.sources.upcoming = []
    later = NOW + timedelta(hours=3)  # started 1 h ago, 120 min long: still live
    contests.run(w.context(now=later))
    row = w.notion.rows(S.DB_CONTESTS)[0]
    assert row["properties"][S.P_Contests.PHASE]["select"]["name"] == "Live"
    much_later = NOW + timedelta(hours=10)
    contests.run(w.context(now=much_later))
    row = w.notion.rows(S.DB_CONTESTS)[0]
    assert row["properties"][S.P_Contests.PHASE]["select"]["name"] == "Finished"


def test_alert_fires_once_with_mentions_and_email() -> None:
    w = build_world()
    w.sources.upcoming = [contest("Codeforces Round 1003 (Div. 2)", NOW + timedelta(minutes=20), ext="CF-2053")]
    contests.run(w.context())
    sent: list[tuple[str, str, str]] = []

    ctx = w.context()
    ctx.mail = lambda to, subject, body: sent.append((to, subject, body)) or True
    result = alerts.run(ctx)
    assert result.result == "OK"
    assert result.items == 1
    assert len(w.notion.comments) == 1
    page_id, rich = w.notion.comments[0]
    assert page_id == w.notion.rows(S.DB_CONTESTS)[0]["id"]
    text = "".join(item.get("text", {}).get("content", "") for item in rich if item["type"] == "text")
    assert "starts in 20 min" in text
    mentions = [item["mention"]["user"]["id"] for item in rich if item["type"] == "mention"]
    assert mentions == ["user-eyad"], "one mention per participant's Person"
    assert [to for to, _, _ in sent] == ["eyad@example.test"]
    assert get_checkbox(w.notion.rows(S.DB_CONTESTS)[0], S.P_Contests.ALERT_SENT) is True

    # Re-run inside the window: Alert Sent is ticked, so nothing is posted or mailed again.
    ctx2 = w.context(now=NOW + timedelta(minutes=5))
    ctx2.mail = lambda to, subject, body: sent.append((to, subject, body)) or True
    alerts.run(ctx2)
    assert len(w.notion.comments) == 1
    assert len(sent) == 1


def test_alert_not_sent_before_or_after_window() -> None:
    w = build_world()
    w.sources.upcoming = [contest("Codeforces Round 1004 (Div. 2)", NOW + timedelta(minutes=90), ext="CF-2054")]
    contests.run(w.context())
    alerts.run(w.context())
    assert w.notion.comments == []
    assert get_checkbox(w.notion.rows(S.DB_CONTESTS)[0], S.P_Contests.ALERT_SENT) is False

    later = NOW + timedelta(minutes=90)
    alerts.run(w.context(now=later))  # 0 minutes to start: not "before" the start
    assert w.notion.comments == []


def test_alert_without_participants_only_ticks() -> None:
    w = build_world(subscriptions=[])  # Eyad subscribes to nothing, no tasks exist
    w.sources.upcoming = [contest("Codeforces Round 1005 (Div. 2)", NOW + timedelta(minutes=15), ext="CF-2055")]
    contests.run(w.context())
    result = alerts.run(w.context())
    assert w.notion.comments == []
    assert get_checkbox(w.notion.rows(S.DB_CONTESTS)[0], S.P_Contests.ALERT_SENT) is True
    assert "no active tasks" in result.details
