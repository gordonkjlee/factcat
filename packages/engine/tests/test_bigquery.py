"""BigQuery adapter: mock the official client, no network."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from factcat.warehouses import AdapterError, BytesCapError, connect
from factcat.warehouses.bigquery import (
    DEFAULT_MAXIMUM_BYTES_BILLED,
    BigQueryAdapter,
    _load_google,
)


class _JobConfig:
    """Real object so 'attribute was never set' is observable."""


@pytest.fixture()
def google_stack(monkeypatch):
    bigquery = MagicMock()
    service_account = MagicMock()
    job_config = _JobConfig()
    bigquery.QueryJobConfig.return_value = job_config
    client = MagicMock()
    bigquery.Client.return_value = client
    job = MagicMock()
    job.total_bytes_processed = 100
    job.total_bytes_billed = 80
    job.job_id = "job-1"
    job.result.return_value = [{"n": 1}]
    client.query.return_value = job
    monkeypatch.setattr(
        "factcat.warehouses.bigquery._load_google",
        lambda: (bigquery, service_account),
    )
    return SimpleNamespace(
        bigquery=bigquery,
        service_account=service_account,
        job_config=job_config,
        client=client,
        job=job,
    )


def test_default_cap_is_written_on_the_job(google_stack):
    adapter = BigQueryAdapter(project="p", location="EU")
    adapter.run("SELECT 1")
    assert google_stack.job_config.maximum_bytes_billed == DEFAULT_MAXIMUM_BYTES_BILLED
    assert google_stack.job_config.job_timeout_ms == 600_000
    assert DEFAULT_MAXIMUM_BYTES_BILLED == 10 * 1024**3


def test_none_cap_leaves_job_uncapped(google_stack):
    adapter = BigQueryAdapter(
        project="p", location="EU", maximum_bytes_billed=None
    )
    adapter.run("SELECT 1")
    assert not hasattr(google_stack.job_config, "maximum_bytes_billed")


def test_dry_run_does_not_fetch_rows(google_stack):
    adapter = BigQueryAdapter(project="p", location="EU")
    result = adapter.run("SELECT 1", dry_run=True)
    google_stack.job.result.assert_not_called()
    assert google_stack.job_config.dry_run is True
    assert result.rows == []
    assert result.bytes_processed == 100
    assert result.job_id == "job-1"


def test_dry_run_estimate_above_cap_raises(google_stack):
    google_stack.job.total_bytes_processed = DEFAULT_MAXIMUM_BYTES_BILLED + 1
    adapter = BigQueryAdapter(project="p", location="EU")
    with pytest.raises(BytesCapError) as exc:
        adapter.run("SELECT 1", dry_run=True)
    google_stack.job.result.assert_not_called()
    assert exc.value.bytes_processed == DEFAULT_MAXIMUM_BYTES_BILLED + 1
    assert exc.value.maximum_bytes_billed == DEFAULT_MAXIMUM_BYTES_BILLED


def test_project_and_location_passed_through(google_stack):
    adapter = BigQueryAdapter(project="my-proj", location="EU")
    adapter.run("SELECT 1 AS n")
    google_stack.bigquery.Client.assert_called_once_with(
        project="my-proj", credentials=None
    )
    kwargs = google_stack.client.query.call_args.kwargs
    assert google_stack.client.query.call_args.args[0] == "SELECT 1 AS n"
    assert kwargs["location"] == "EU"
    assert kwargs["job_config"] is google_stack.job_config


def test_missing_project_is_value_error():
    with pytest.raises(ValueError, match="project"):
        BigQueryAdapter(project="  ", location="EU")
    with pytest.raises(TypeError):
        BigQueryAdapter(location="EU")  # type: ignore[call-arg]


def test_missing_location_is_value_error():
    with pytest.raises(ValueError, match="location"):
        BigQueryAdapter(project="p", location="")
    with pytest.raises(TypeError):
        BigQueryAdapter(project="p")  # type: ignore[call-arg]


def test_adc_constructs_client_without_key(google_stack):
    BigQueryAdapter(project="p", location="EU").run("SELECT 1")
    _, kwargs = google_stack.bigquery.Client.call_args
    assert kwargs["credentials"] is None


def test_json_path_loads_service_account(google_stack):
    key = object()
    google_stack.service_account.Credentials.from_service_account_file.return_value = (
        key
    )
    BigQueryAdapter(
        project="p", location="EU", credentials="keys/sa.json"
    ).run("SELECT 1")
    google_stack.service_account.Credentials.from_service_account_file.assert_called_once_with(
        "keys/sa.json"
    )
    _, kwargs = google_stack.bigquery.Client.call_args
    assert kwargs["credentials"] is key


def test_bad_json_path_does_not_fall_back_to_adc(google_stack):
    google_stack.service_account.Credentials.from_service_account_file.side_effect = (
        FileNotFoundError("missing")
    )
    adapter = BigQueryAdapter(
        project="p", location="EU", credentials="/no/such.json"
    )
    with pytest.raises(AdapterError, match="/no/such.json"):
        adapter.run("SELECT 1")
    google_stack.bigquery.Client.assert_not_called()


def test_credentials_object_passed_through(google_stack):
    creds = object()
    BigQueryAdapter(project="p", location="EU", credentials=creds).run(
        "SELECT 1"
    )
    _, kwargs = google_stack.bigquery.Client.call_args
    assert kwargs["credentials"] is creds


def test_missing_extra_names_install(monkeypatch):
    def boom(name: str):
        raise ImportError("simulated missing extra")

    monkeypatch.setattr(
        "factcat.warehouses.bigquery.importlib.import_module", boom
    )
    with pytest.raises(ImportError, match=r"factcat\[bigquery\]"):
        _load_google()
    adapter = BigQueryAdapter(project="p", location="EU")
    with pytest.raises(ImportError, match=r"factcat\[bigquery\]"):
        adapter.run("SELECT 1")


def test_job_failure_is_adapter_error(google_stack):
    google_stack.client.query.side_effect = RuntimeError("access denied")
    with pytest.raises(AdapterError, match="access denied") as exc:
        BigQueryAdapter(project="p", location="EU").run("SELECT 1")
    assert not isinstance(exc.value, BytesCapError)
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_bq_bytes_cap_rejection_is_bytes_cap_error(google_stack):
    google_stack.client.query.side_effect = RuntimeError(
        "Query exceeded limit for bytes billed: 10737418240"
    )
    with pytest.raises(BytesCapError, match="bytes billed"):
        BigQueryAdapter(project="p", location="EU").run("SELECT 1")


def test_adc_missing_tells_you_to_login(google_stack):
    err = type("DefaultCredentialsError", (RuntimeError,), {})(
        "Could not automatically determine credentials"
    )
    google_stack.bigquery.Client.side_effect = err
    with pytest.raises(AdapterError, match="application-default login"):
        BigQueryAdapter(project="p", location="EU").run("SELECT 1")


def test_empty_sql_rejected_before_client(google_stack):
    with pytest.raises(ValueError, match="sql"):
        BigQueryAdapter(project="p", location="EU").run("  ")
    google_stack.bigquery.Client.assert_not_called()


def test_rows_and_bytes_from_job(google_stack):
    result = BigQueryAdapter(project="p", location="EU").run("SELECT 1")
    assert result.rows == [{"n": 1}]
    assert result.bytes_processed == 100
    assert result.bytes_billed == 80
    assert result.job_id == "job-1"


def test_connect_run_hits_the_mock_client(google_stack):
    adapter = connect("bigquery", project="p", location="EU")
    result = adapter.run("SELECT 1")
    assert result.rows == [{"n": 1}]
    google_stack.client.query.assert_called()


def test_sql_is_not_rewritten(google_stack):
    sql = "SELECT 1 FROM payments WHERE status = 'collected'"
    BigQueryAdapter(project="p", location="EU").run(sql)
    assert google_stack.client.query.call_args.args[0] == sql


@pytest.mark.skipif(
    not os.environ.get("FACTCAT_BQ_LIVE"),
    reason="FACTCAT_BQ_LIVE not set",
)
def test_live_select_one():
    adapter = BigQueryAdapter(
        project=os.environ["FACTCAT_BQ_PROJECT"],
        location=os.environ["FACTCAT_BQ_LOCATION"],
    )
    result = adapter.run("SELECT 1 AS n")
    assert result.rows[0]["n"] == 1
