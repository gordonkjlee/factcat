"""Unit tests for the T1 auto-merge selector. No network."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

from automerge_candidates import candidates, main

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
OWNER = "octocat"


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_run(conclusion: str = "SUCCESS") -> dict:
    return {
        "__typename": "CheckRun",
        "name": "test",
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion,
    }


def status_context(state: str = "SUCCESS") -> dict:
    return {"__typename": "StatusContext", "context": "ci", "state": state}


def pr(number: int = 1, **overrides) -> dict:
    base = {
        "number": number,
        "createdAt": at(30),
        "author": {"login": OWNER},
        "isDraft": False,
        "labels": [{"name": "auto-merge-t1"}, {"name": "dependencies"}],
        "headRefName": "fix-thing",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [check_run(), status_context()],
        "reviewDecision": "",
    }
    base.update(overrides)
    return base


def pick(*prs: dict, min_age_hours: float = 24, authors=(OWNER,)) -> list[int]:
    return candidates(list(prs), NOW, min_age_hours, authors)


def test_qualifies_when_every_gate_passes():
    assert pick(pr(7)) == [7]


def test_too_young_does_not_qualify():
    """Mutation: ignoring min_age_hours, or hard-coding it, must turn this red."""
    assert pick(pr(createdAt=at(23))) == []
    assert pick(pr(createdAt=at(23)), min_age_hours=20) == [1]


def test_exactly_min_age_qualifies():
    assert pick(pr(createdAt=at(24))) == [1]


def test_label_absent_does_not_qualify():
    assert pick(pr(labels=[{"name": "dependencies"}])) == []
    assert pick(pr(labels=[])) == []


def test_failing_check_does_not_qualify():
    assert pick(pr(statusCheckRollup=[check_run(), check_run("FAILURE")])) == []
    assert pick(pr(statusCheckRollup=[status_context("FAILURE")])) == []


def test_pending_check_does_not_qualify():
    assert pick(pr(statusCheckRollup=[check_run(), check_run("")])) == []
    assert pick(pr(statusCheckRollup=[status_context("PENDING")])) == []


def test_zero_checks_does_not_qualify():
    assert pick(pr(statusCheckRollup=[])) == []
    assert pick(pr(statusCheckRollup=None)) == []


def test_skipped_check_counts_as_green():
    assert pick(pr(statusCheckRollup=[check_run("SKIPPED"), check_run()])) == [1]


def test_draft_does_not_qualify():
    assert pick(pr(isDraft=True)) == []


def test_other_author_does_not_qualify():
    assert pick(pr(author={"login": "someone-else"})) == []
    assert pick(pr(), authors=()) == []


def test_non_main_base_does_not_qualify():
    assert pick(pr(baseRefName="release/0.5")) == []


def test_conflicting_does_not_qualify():
    assert pick(pr(mergeable="CONFLICTING")) == []
    assert pick(pr(mergeable="UNKNOWN")) == [1]


def test_changes_requested_does_not_qualify():
    assert pick(pr(reviewDecision="CHANGES_REQUESTED")) == []
    assert pick(pr(reviewDecision="APPROVED")) == [1]


def test_main_reads_gh_json_and_prints_sorted_numbers(capsys):
    payload = json.dumps([pr(12), pr(3), pr(5, isDraft=True)])
    rc = main(
        ["--now", "2026-09-06T12:00:00Z", "--author", "nobody", "--author", OWNER],
        stdin=io.StringIO(payload),
    )
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["3", "12"]


def test_main_default_min_age_is_a_day(capsys):
    payload = json.dumps([pr(1, createdAt=at(23)), pr(2, createdAt=at(25))])
    main(["--now", "2026-09-06T12:00:00Z", "--author", OWNER], stdin=io.StringIO(payload))
    assert capsys.readouterr().out.splitlines() == ["2"]
