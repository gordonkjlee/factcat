"""The billing-project probe asks the real IAM.

No job runs and nothing is billed: Resource Manager's ``testIamPermissions``
is metadata, and the fallback is a dry run.
"""

from __future__ import annotations

import pytest

from factcat.warehouses.bigquery import JOBS_CREATE
from factcat_app.layout import job_access_from_form

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _bigquery_only(live):
    if live.kind != "bigquery":
        pytest.skip(f"the mapping is {live.kind}, not bigquery")


def test_sandbox_billing_project_can_run_jobs(live):
    form = live.form()
    out = job_access_from_form(
        {
            "kind": "bigquery",
            "project": str(live.mapping.get("project") or live.mapping.get("data_project")),
            "data_project": str(live.mapping.get("data_project") or ""),
            "location": live.mapping["location"],
            "credentials": live.mapping.get("credentials") or "",
        }
    )
    assert out["status"] == "ok", {k: v for k, v in out.items() if k != "project"}
    assert form["kind"] == "bigquery"
    if "granted" in out:
        assert JOBS_CREATE in out["granted"]


def test_a_project_that_does_not_exist_is_not_a_pass(live):
    out = job_access_from_form(
        {
            "kind": "bigquery",
            "project": "factcat-no-such-project-0000",
            "location": live.mapping["location"],
            "credentials": live.mapping.get("credentials") or "",
        }
    )
    assert out["status"] in ("denied", "error")
