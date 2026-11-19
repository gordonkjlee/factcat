"""Setup refuses a billing project (or role + warehouse) that cannot run jobs.

"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from factcat.warehouses import AdapterError
from factcat.warehouses import bigquery as bq
from factcat.warehouses.bigquery import JOBS_CREATE, partition_avg_bytes
from factcat_app import main as main_mod
from factcat_app.layout import (
    _partition_avg,
    is_job_denial,
    job_access_from_form,
    job_access_key,
)
from factcat_app.main import app

DENIED = "Access Denied: Project p: User does not have bigquery.jobs.create permission in project p."


def _bq(**extra):
    return {"kind": "bigquery", "project": "p", "location": "EU", **extra}


# --- the IAM helper ---------------------------------------------------------


@pytest.fixture()
def google_stack(monkeypatch):
    bigquery = MagicMock()
    client = MagicMock()
    bigquery.Client.return_value = client
    monkeypatch.setattr(
        "factcat.warehouses.bigquery._load_google",
        lambda: (bigquery, MagicMock()),
    )
    return SimpleNamespace(bigquery=bigquery, client=client)


def _http_reply(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    return resp


def test_project_iam_returns_the_granted_permissions(google_stack):
    google_stack.client._http.post.return_value = _http_reply(
        body={"permissions": [JOBS_CREATE]}
    )
    assert bq.test_project_iam(project="jobs-proj") == [JOBS_CREATE]
    url = google_stack.client._http.post.call_args.args[0]
    assert url.endswith("/projects/jobs-proj:testIamPermissions")
    assert google_stack.client._http.post.call_args.kwargs["json"] == {
        "permissions": [JOBS_CREATE]
    }
    google_stack.client.query.assert_not_called()  # IAM, never a job


def test_project_iam_missing_permission_is_an_empty_grant(google_stack):
    google_stack.client._http.post.return_value = _http_reply(body={})
    assert bq.test_project_iam(project="data-proj") == []


def test_project_iam_http_failure_names_the_failure(google_stack):
    google_stack.client._http.post.return_value = _http_reply(
        status=403, body={"error": {"message": "API not enabled"}}
    )
    with pytest.raises(AdapterError, match="HTTP 403.*API not enabled"):
        bq.test_project_iam(project="p")


# --- the verdict ------------------------------------------------------------


def test_denied_when_jobs_create_is_not_granted(monkeypatch):
    monkeypatch.setattr("factcat_app.layout.test_project_iam", lambda **kw: [])
    out = job_access_from_form(_bq(data_project="data-proj"))
    assert out["status"] == "denied"
    assert out["project"] == "p" and out["data_project"] == "data-proj"


def test_ok_when_jobs_create_is_granted(monkeypatch):
    monkeypatch.setattr(
        "factcat_app.layout.test_project_iam", lambda **kw: [JOBS_CREATE]
    )
    out = job_access_from_form(_bq(data_project="p"))
    assert out["status"] == "ok"
    assert "data_project" not in out  # same project: nothing to name twice


def test_blank_billing_project_is_skipped():
    assert job_access_from_form({"kind": "bigquery"})["status"] == "skipped"


def test_iam_unavailable_falls_back_to_a_free_dry_run(monkeypatch):
    def no_iam(**kw):
        raise AdapterError("could not check permissions on p (HTTP 403): API not enabled")

    seen = {}

    def dry(**kw):
        seen.update(kw)
        return {"bytes_processed": 0, "referenced_tables": []}

    monkeypatch.setattr("factcat_app.layout.test_project_iam", no_iam)
    monkeypatch.setattr("factcat_app.layout.dry_run_scan", dry)
    out = job_access_from_form(_bq())
    assert out["status"] == "ok" and out["via"] == "dry_run"
    assert seen["sql"] == "SELECT 1" and seen["project"] == "p"


def test_iam_unavailable_and_dry_run_denied_is_denied(monkeypatch):
    def no_iam(**kw):
        raise AdapterError("API not enabled")

    def dry(**kw):
        raise AdapterError(DENIED)

    monkeypatch.setattr("factcat_app.layout.test_project_iam", no_iam)
    monkeypatch.setattr("factcat_app.layout.dry_run_scan", dry)
    assert job_access_from_form(_bq())["status"] == "denied"


def test_a_failed_check_is_an_error_not_a_silent_pass(monkeypatch):
    def no_iam(**kw):
        raise AdapterError("API not enabled")

    def dry(**kw):
        raise AdapterError("connection reset")

    monkeypatch.setattr("factcat_app.layout.test_project_iam", no_iam)
    monkeypatch.setattr("factcat_app.layout.dry_run_scan", dry)
    out = job_access_from_form(_bq())
    assert out["status"] == "error" and "API not enabled" in out["error"]


def test_snowflake_role_or_warehouse_denial_is_denied(monkeypatch):
    def refuse(**kw):
        raise AdapterError("No active warehouse selected / USAGE denied")

    monkeypatch.setattr("factcat_app.layout.can_run_statement", refuse)
    form = {"kind": "snowflake", "account": "xy12345", "user": "U", "warehouse": "WH", "role": "R"}
    out = job_access_from_form(form)
    assert out["status"] == "denied"
    assert out["warehouse"] == "WH" and out["role"] == "R"
    monkeypatch.setattr(
        "factcat_app.layout.can_run_statement",
        lambda **kw: {"role": "R", "warehouse": "WH"},
    )
    assert job_access_from_form(form)["status"] == "ok"


def test_snowflake_role_without_warehouse_usage_is_denied_end_to_end(monkeypatch, tmp_path):
    """The real probe, a stubbed driver: the session binds no warehouse.

    The connection and ``CURRENT_ROLE()`` succeed, as they do on Snowflake
    for a role with no USAGE on the warehouse; Setup must still say denied.
    """
    key = tmp_path / "rsa_key.p8"
    key.write_text("-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n", encoding="utf-8")
    cur = MagicMock()
    cur.description = [("FC_ROLE",), ("FC_WH",)]
    cur.fetchall.return_value = [("R", None)]
    connector = MagicMock()
    connector.connect.return_value.cursor.return_value = cur
    monkeypatch.setattr("factcat.warehouses.snowflake._load_snowflake", lambda: connector)
    form = {
        "kind": "snowflake",
        "account": "xy12345",
        "user": "U",
        "warehouse": "WH",
        "role": "R",
        "private_key_path": str(key),
    }
    out = job_access_from_form(form)
    assert out["status"] == "denied" and out["reason"] == "no_usage"
    assert "no USAGE on warehouse WH" in out["error"]
    cur.fetchall.return_value = [("R", "WH")]
    assert job_access_from_form(form)["status"] == "ok"


def test_snowflake_without_a_warehouse_is_skipped():
    form = {"kind": "snowflake", "account": "xy12345", "user": "U"}
    assert job_access_from_form(form)["status"] == "skipped"


def test_job_denial_is_told_apart_from_a_missing_table():
    assert is_job_denial(AdapterError(DENIED))
    assert not is_job_denial(AdapterError("Not found: Table p:d.t"))


# --- Save gate (server side) -------------------------------------------------


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    main_mod._JOB_VERDICTS.clear()
    return TestClient(app)


def test_denied_billing_project_cannot_be_saved(monkeypatch, tmp_path):
    """Mutation: drop the _job_refusal call in api_save and this goes red."""
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod, "job_access_from_form", lambda form: {"status": "denied", "project": "p"})
    body = {"kind": "bigquery", "project": "p", "dataset": "d"}
    probe = client.post("/api/job_access", json=body).json()
    assert probe["ok"] and probe["status"] == "denied"
    res = client.post("/api/save", json=body).json()
    assert res["ok"] is False
    assert "cannot run queries in p" in res["error"]
    assert not (tmp_path / "cfg.json").exists()


def test_a_grant_clears_the_refusal_without_a_restart(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    body = {"kind": "bigquery", "project": "p", "dataset": "d"}
    monkeypatch.setattr(main_mod, "job_access_from_form", lambda form: {"status": "denied", "project": "p"})
    client.post("/api/job_access", json=body)
    monkeypatch.setattr(main_mod, "job_access_from_form", lambda form: {"status": "ok", "project": "p"})
    assert client.post("/api/save", json=body).json()["ok"] is True
    assert (tmp_path / "cfg.json").exists()


def test_an_unprobed_mapping_still_saves(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.post("/api/save", json={"kind": "bigquery", "project": "p"}).json()["ok"]


def test_a_denial_for_one_project_does_not_block_another(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod, "job_access_from_form", lambda form: {"status": "denied", "project": form["project"]})
    client.post("/api/job_access", json={"kind": "bigquery", "project": "bad"})
    assert client.post("/api/save", json={"kind": "bigquery", "project": "good"}).json()["ok"]


def test_a_check_that_errors_does_not_block_saving(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod, "job_access_from_form", lambda form: {"status": "error", "error": "x"})
    client.post("/api/job_access", json={"kind": "bigquery", "project": "p"})
    assert client.post("/api/save", json={"kind": "bigquery", "project": "p"}).json()["ok"]


def test_snowflake_denial_blocks_save_with_role_and_warehouse_named(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    body = {"kind": "snowflake", "account": "xy", "user": "U", "role": "R", "warehouse": "WH"}
    monkeypatch.setattr(main_mod, "job_access_from_form", lambda form: {"status": "denied", "role": "R", "warehouse": "WH"})
    client.post("/api/job_access", json=body)
    res = client.post("/api/save", json=body).json()
    assert res["ok"] is False and "R cannot run queries on WH" in res["error"]


def test_key_separates_billing_projects_and_credentials():
    assert job_access_key(_bq()) != job_access_key(_bq(project="q"))
    assert job_access_key(_bq()) != job_access_key(_bq(credentials="k.json"))
    assert job_access_key(_bq(data_project="x")) == job_access_key(_bq())  # data project is not billed
    sf = {"kind": "snowflake", "account": "a", "user": "u", "role": "R", "warehouse": "WH"}
    assert job_access_key(sf) != job_access_key({**sf, "private_key_path": "other.p8"})


def test_setup_page_carries_the_callouts_and_the_gate():
    text = TestClient(app).get("/setup").text
    assert 'id="job-access-hint-bq"' in text
    assert 'id="job-access-hint-sf"' in text
    assert "gate: jobAccessGate" in text
    assert "/api/job_access" in text


# --- layout probe billing split ---------------------------------------------


def test_partition_avg_bills_billing_and_qualifies_the_data_project(google_stack):
    job = MagicMock()
    job.result.return_value = [{"fc_avg": 7}]
    google_stack.client.query.return_value = job
    out = partition_avg_bytes(
        project="data-proj",
        billing_project="jobs-proj",
        dataset="analytics",
        table="events",
        location="EU",
    )
    assert out == 7
    assert google_stack.bigquery.Client.call_args.kwargs["project"] == "jobs-proj"
    sql = google_stack.client.query.call_args.args[0]
    assert "`data-proj.analytics.INFORMATION_SCHEMA.PARTITIONS`" in sql


def test_partition_avg_defaults_to_one_project(google_stack):
    job = MagicMock()
    job.result.return_value = []
    google_stack.client.query.return_value = job
    partition_avg_bytes(project="p", dataset="d", table="t", location="EU")
    assert google_stack.bigquery.Client.call_args.kwargs["project"] == "p"


def _avg_form():
    return {
        "kind": "bigquery",
        "project": "jobs-proj",
        "data_project": "data-proj",
        "location": "EU",
        "dataset": "analytics",
        "table_name": "events",
    }


def test_layout_probe_passes_billing_and_data_projects(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return 5

    monkeypatch.setattr("factcat_app.layout.partition_avg_bytes", fake)
    got = _partition_avg(_avg_form(), {"partition": "occurred_at", "name": "events"})
    assert got == 5
    assert seen["project"] == "data-proj" and seen["billing_project"] == "jobs-proj"


def test_layout_probe_does_not_hide_a_job_denial(monkeypatch):
    """Mutation: swallow every AdapterError again (the 0.6.0 behaviour) and
    this goes red: a 403 on jobs.create must not look like a missing hint."""

    def deny(**kw):
        raise AdapterError(DENIED)

    monkeypatch.setattr("factcat_app.layout.partition_avg_bytes", deny)
    with pytest.raises(AdapterError, match="jobs.create"):
        _partition_avg(_avg_form(), {"partition": "occurred_at", "name": "events"})


def test_layout_probe_still_swallows_a_table_without_stats(monkeypatch):
    def missing(**kw):
        raise AdapterError("Not found: Table data-proj:analytics.events")

    monkeypatch.setattr("factcat_app.layout.partition_avg_bytes", missing)
    assert _partition_avg(_avg_form(), {"partition": "occurred_at", "name": "events"}) is None
