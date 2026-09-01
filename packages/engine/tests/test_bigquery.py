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
    _cli_project_value,
    _load_google,
    gcloud_config_project,
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


def test_bq_bytes_cap_rejection_keeps_both_figures(google_stack):
    """BigQuery states what it needs and what it was capped at. Keep both."""
    google_stack.client.query.side_effect = RuntimeError(
        "500 Query exceeded limit for bytes billed: 10737418240. "
        "39277559808 or higher required.; reason: bytesBilledLimitExceeded, "
        "message: Query exceeded limit for bytes billed: 10737418240. "
        "39277559808 or higher required."
    )
    with pytest.raises(BytesCapError) as exc:
        BigQueryAdapter(project="p", location="EU").run("SELECT 1")
    assert exc.value.bytes_processed == 39277559808
    assert exc.value.maximum_bytes_billed == 10737418240


def test_bq_bytes_cap_rejection_reports_the_cap_it_was_measured_against(
    google_stack,
):
    # The rejection's own limit wins over the one we asked for: BigQuery is
    # authoritative about what it actually enforced.
    google_stack.client.query.side_effect = RuntimeError(
        "Query exceeded limit for bytes billed: 5368709120. "
        "39277559808 or higher required."
    )
    with pytest.raises(BytesCapError) as exc:
        BigQueryAdapter(
            project="p", location="EU", maximum_bytes_billed=10 * 1024**3
        ).run("SELECT 1")
    assert exc.value.maximum_bytes_billed == 5368709120


def test_bq_bytes_cap_rejection_without_figures_falls_back(google_stack):
    """A reworded message still picks the class; the figures degrade to None."""
    google_stack.client.query.side_effect = RuntimeError(
        "reason: bytesBilledLimitExceeded"
    )
    with pytest.raises(BytesCapError) as exc:
        BigQueryAdapter(
            project="p", location="EU", maximum_bytes_billed=10 * 1024**3
        ).run("SELECT 1")
    assert exc.value.bytes_processed is None
    assert exc.value.maximum_bytes_billed == 10 * 1024**3


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


def test_gcloud_config_project_reads_cli(monkeypatch):
    monkeypatch.delenv("CLOUDSDK_CORE_PROJECT", raising=False)

    def fake_run(*_a, **_k):
        return SimpleNamespace(returncode=0, stdout="cli-project\n", stderr="")

    monkeypatch.setattr("factcat.warehouses.bigquery.subprocess.run", fake_run)
    assert gcloud_config_project() == "cli-project"


def test_gcloud_config_project_env_skips_cli(monkeypatch):
    monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", "env-project")

    def boom(*_a, **_k):
        raise AssertionError("must not spawn gcloud when CLOUDSDK_CORE_PROJECT is set")

    monkeypatch.setattr("factcat.warehouses.bigquery.subprocess.run", boom)
    assert gcloud_config_project() == "env-project"


def test_gcloud_config_project_unset_or_missing(monkeypatch):
    monkeypatch.delenv("CLOUDSDK_CORE_PROJECT", raising=False)
    monkeypatch.setattr(
        "factcat.warehouses.bigquery.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="(unset)\n", stderr=""),
    )
    assert gcloud_config_project() == ""

    def missing(*_a, **_k):
        raise FileNotFoundError("gcloud")

    monkeypatch.setattr("factcat.warehouses.bigquery.subprocess.run", missing)
    assert gcloud_config_project() == ""


def test_cli_project_value_skips_warnings():
    assert _cli_project_value("WARNING: your current project is unset\ncli-project\n") == "cli-project"
    assert _cli_project_value("Your active configuration is: [default]\n(unset)\n") == ""
    assert _cli_project_value("") == ""


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
