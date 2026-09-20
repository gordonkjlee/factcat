"""Pick the labelled T1 pull requests that have cooled long enough to merge.

Reads the JSON that ``gh pr list --json ...`` prints on stdin, applies every
gate, and prints one qualifying PR number per line. Selection only: nothing
here talks to GitHub, so every gate is testable without a network.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, TextIO

LABEL = "auto-merge-t1"
BASE = "main"
# A check that ran and passed, or one GitHub skipped on purpose. Anything
# else - pending, neutral, failed - is not evidence of green.
GREEN = frozenset({"SUCCESS", "SKIPPED"})


def parse_instant(value: str) -> datetime:
    # GitHub writes a trailing Z, which older fromisoformat rejects.
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def check_is_green(check: dict[str, Any]) -> bool:
    # A check run reports ``conclusion`` (empty while it runs); a commit
    # status reports ``state``.
    verdict = check.get("conclusion") or check.get("state") or ""
    return verdict.upper() in GREEN


def body_names_head(pr: dict[str, Any]) -> bool:
    """The body must name the current head so a later push cannot ride the label."""
    sha = (pr.get("headRefOid") or "").strip().lower()
    if len(sha) < 40:
        return False
    return sha in (pr.get("body") or "").lower()


def gates_hold(
    pr: dict[str, Any], now: datetime, min_age_hours: float, authors: set[str]
) -> bool:
    if not any(label.get("name") == LABEL for label in pr.get("labels") or []):
        return False
    if pr.get("isDraft"):
        return False
    if pr.get("baseRefName") != BASE:
        return False
    if (pr.get("author") or {}).get("login") not in authors:
        return False
    if now - parse_instant(pr["createdAt"]) < timedelta(hours=min_age_hours):
        return False
    if pr.get("mergeable") == "CONFLICTING":
        return False
    # A reviewer who asked for changes after the label went on outranks it.
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        return False
    if not body_names_head(pr):
        return False
    checks = pr.get("statusCheckRollup") or []
    # No checks is no evidence: a PR nothing ran on is not green.
    return bool(checks) and all(check_is_green(check) for check in checks)


def qualifies(
    pr: dict[str, Any], now: datetime, min_age_hours: float, authors: set[str]
) -> bool:
    return gates_hold(pr, now, min_age_hours, authors) and pr.get("mergeStateStatus") == "CLEAN"


def is_behind(
    pr: dict[str, Any], now: datetime, min_age_hours: float, authors: set[str]
) -> bool:
    return gates_hold(pr, now, min_age_hours, authors) and pr.get("mergeStateStatus") == "BEHIND"


def candidates(
    prs: Iterable[dict[str, Any]],
    now: datetime,
    min_age_hours: float,
    authors: Iterable[str],
) -> list[int]:
    allowed = set(authors)
    return sorted(
        int(pr["number"]) for pr in prs if qualifies(pr, now, min_age_hours, allowed)
    )


def behind(
    prs: Iterable[dict[str, Any]],
    now: datetime,
    min_age_hours: float,
    authors: Iterable[str],
) -> list[int]:
    allowed = set(authors)
    return sorted(
        int(pr["number"]) for pr in prs if is_behind(pr, now, min_age_hours, allowed)
    )


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--now", required=True, help="ISO 8601 instant, e.g. 2026-09-06T12:00:00Z"
    )
    parser.add_argument("--min-age-hours", type=float, default=24.0)
    parser.add_argument(
        "--author",
        action="append",
        default=[],
        help="author login allowed to merge this way; repeatable",
    )
    parser.add_argument(
        "--behind",
        action="store_true",
        help="print BEHIND numbers (to update-branch) instead of CLEAN merge candidates",
    )
    args = parser.parse_args(argv)
    prs = json.load(stdin or sys.stdin)
    pick = behind if args.behind else candidates
    for number in pick(prs, parse_instant(args.now), args.min_age_hours, args.author):
        print(number)
    return 0


if __name__ == "__main__":
    sys.exit(main())
