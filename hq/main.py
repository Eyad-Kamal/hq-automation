"""Command line entry point: ``python -m hq.main <job> [--force] [--dry-run] [--member NAME]``.

Jobs: auto (default in CI), onboard, contests, alerts, imports, recompute, daily, weekly.

``auto`` runs, in this order:
  onboard -> contests -> alerts -> [imports + recompute, when the last import is
  more than 25 minutes old] -> [daily, when not yet run today (Cairo)] ->
  [weekly, after Saturday 00:10 Cairo and not yet run this week].
``--force`` ignores those "due" checks. A Sync Log row is written when a job changed items or did not end OK, and a
failing job never stops the others. The exit code is 1 when any job ended with
the result "Error".

``--dry-run`` reads Notion but writes nothing: no rows, no comments, no state,
no email. The intended writes are printed instead.

``--member NAME`` limits per-member work to one active member (matched by name).

Environment: NOTION_TOKEN (required). Optional: HQ_IDS_JSON (data source ids as
JSON, otherwise notion-ids.json), SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence

import requests

from hq import config, notify
from hq.jobs import ERROR, Context, JobResult, execute
from hq.jobs import alerts, contests, daily, imports, onboard, recompute, weekly
from hq.notion import Notion, load_data_source_ids
from hq.repo import Repo
from hq.sources import atcoder, codeforces, leetcode
from hq.timeutil import now_utc

JOBS: dict[str, Callable[[Context], JobResult]] = {
    "onboard": onboard.run,
    "contests": contests.run,
    "alerts": alerts.run,
    "imports": imports.run,
    "recompute": recompute.run,
    "daily": daily.run,
    "weekly": weekly.run,
}
ALIASES: dict[str, str] = {"import": "imports"}
CHOICES = ("auto", *JOBS, *ALIASES)


class LiveSources:
    """Real platform clients.

    By default every method returns an empty value on failure (never raises). With
    ``strict=True`` a failed call returns None instead, so the caller can tell
    "failed" apart from "nothing new". The imports job always uses strict=True.
    """

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session if session is not None else requests.Session()

    def upcoming_contests(self):  # -> list[Contest]
        found = []
        for fetch in (codeforces.fetch_upcoming_contests, atcoder.fetch_upcoming_contests, leetcode.fetch_upcoming_contests):
            found.extend(fetch(self.session) or [])
        return found

    def cf_solves(self, handle, since, *, strict: bool = False):
        if strict:
            return codeforces.fetch_solves_or_none(self.session, handle, since)
        return codeforces.fetch_solves(self.session, handle, since) or []

    def cf_rating_history(self, handle, *, strict: bool = False):
        if strict:
            return codeforces.fetch_rating_history_or_none(self.session, handle)
        return codeforces.fetch_rating_history(self.session, handle) or []

    def cf_user_info(self, handles, *, strict: bool = False):
        if strict:
            return codeforces.fetch_user_info_or_none(self.session, list(handles))
        return codeforces.fetch_user_info(self.session, list(handles)) or {}

    def cf_problemset(self):
        return codeforces.fetch_problemset(self.session) or []

    def at_solves(self, user, since, offset, *, strict: bool = False):
        if strict:
            return atcoder.fetch_solves_or_none(self.session, user, since, offset)
        return atcoder.fetch_solves(self.session, user, since, offset) or []

    def at_rating_history(self, user, *, strict: bool = False):
        if strict:
            return atcoder.fetch_rating_history_or_none(self.session, user)
        return atcoder.fetch_rating_history(self.session, user) or []

    def lc_solves(self, username, since, *, strict: bool = False):
        if strict:
            return leetcode.fetch_solves_or_none(self.session, username, since)
        return leetcode.fetch_solves(self.session, username, since) or []


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m hq.main", description="MUST CPC Training HQ automation")
    parser.add_argument("job", nargs="?", default="auto", choices=CHOICES, help="job to run (default: auto)")
    parser.add_argument("--force", action="store_true", help="ignore the due checks (imports, daily, weekly)")
    parser.add_argument("--dry-run", action="store_true", help="read Notion, write nothing; print the intended writes")
    parser.add_argument("--member", metavar="NAME", help="limit per-member work to one active member")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def build_context(args: argparse.Namespace, env: Mapping[str, str]) -> Context:
    token = (env.get("NOTION_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("NOTION_TOKEN is not set")
    ids = load_data_source_ids(dict(env))
    if not ids:
        raise RuntimeError("no Notion data source ids: set HQ_IDS_JSON or provide notion-ids.json")
    repo = Repo(Notion(token), ids, dry_run=args.dry_run)
    cfg = config.load(repo)
    mail = None if args.dry_run else notify.make_mailer(notify.smtp_settings(dict(env)))
    return Context(
        repo=repo,
        sources=LiveSources(),
        now=now_utc(),
        cfg=cfg,
        mail=mail,
        force=bool(args.force),
        member_filter=args.member,
    )


def run_auto(ctx: Context) -> list[JobResult]:
    """The scheduled sequence. Each step runs only when due (or when forced)."""
    results = [execute(ctx, "onboard", onboard.run)]
    results.append(execute(ctx, "contests", contests.run))
    results.append(execute(ctx, "alerts", alerts.run))
    if ctx.force or imports.imports_due(ctx):
        results.append(execute(ctx, "imports", imports.run))
        results.append(execute(ctx, "recompute", recompute.run))
    if ctx.force or daily.daily_due(ctx):
        results.append(execute(ctx, "daily", daily.run))
    if ctx.force or weekly.weekly_due(ctx):
        results.append(execute(ctx, "weekly", weekly.run))
    return results


def run_named(ctx: Context, name: str) -> list[JobResult]:
    name = ALIASES.get(name, name)
    if name == "auto":
        return run_auto(ctx)
    return [execute(ctx, name, JOBS[name])]


def _print_results(results: list[JobResult]) -> None:
    for result in results:
        print(f"[{result.result}] {result.job}: {result.items} item(s)")
        for line in result.details.splitlines():
            print(f"    {line}")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    environment = os.environ if env is None else env
    try:
        ctx = build_context(args, environment)
    except (RuntimeError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print("DRY RUN: nothing will be written to Notion.")
    results = run_named(ctx, args.job)
    _print_results(results)
    if args.dry_run:
        print(f"{len(ctx.repo.write_log)} intended write(s):")
        for line in ctx.repo.write_log:
            print(f"    {line}")
    return 1 if any(r.result == ERROR for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
