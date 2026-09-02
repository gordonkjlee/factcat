"""Execute-adapter protocol: no Google, no caller-warehouse identity on Adapter."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from factcat import RetentionSpec, __file__ as factcat_init, retention_sql
from factcat.dialects import SUPPORTED
from factcat.warehouses import (
    ADAPTERS,
    CAP_DRY_RUN,
    CAP_SCAN_CAP,
    Adapter,
    AdapterError,
    DryRunNotSupported,
    is_missing_relation,
    QueryResult,
    capabilities,
    connect,
)
from factcat.warehouses import __file__ as warehouses_init


class FakeAdapter:
    """Second adapter shape: no project, no location, no bytes cap."""

    dialect = "duckdb"
    capabilities = frozenset()

    def __init__(self) -> None:
        self.executed: list[str] = []

    def run(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        if dry_run:
            raise DryRunNotSupported("fake adapter cannot dry-run")
        self.executed.append(sql)
        return QueryResult(rows=[{"ok": True}])


def _spec() -> RetentionSpec:
    return RetentionSpec(
        table="payments",
        entity="subscription_id",
        entity_time="sub_start",
        event_time="paid_at",
        period_days=35,
        n_periods=2,
        retained="status = 'collected' AND within_period_offset <= 5",
    )


def test_fake_has_no_bigquery_fields():
    """If project/location leak onto Adapter, the generate-then-run fake breaks."""
    fake = FakeAdapter()
    assert not hasattr(fake, "project")
    assert not hasattr(fake, "location")
    assert not hasattr(fake, "maximum_bytes_billed")
    hints = getattr(Adapter, "__annotations__", {})
    assert "project" not in hints
    assert "location" not in hints
    assert "maximum_bytes_billed" not in hints


def test_app_shaped_generate_then_run():
    fake = FakeAdapter()
    adapter: Adapter = fake
    sql = retention_sql(_spec(), dialect=adapter.dialect)
    result = adapter.run(sql)
    assert "period_index" in sql
    assert result.rows == [{"ok": True}]
    assert fake.executed == [sql]


def test_fake_dry_run_does_not_execute():
    fake = FakeAdapter()
    with pytest.raises(DryRunNotSupported):
        fake.run("SELECT 1", dry_run=True)
    assert fake.executed == []


def test_connect_unknown_sql_dialect():
    with pytest.raises(LookupError, match="shipped:") as exc:
        connect("databricks")
    assert "SQL generation supports" not in str(exc.value)
    assert "databricks" in SUPPORTED
    assert "bigquery" in str(exc.value)
    assert "snowflake" in str(exc.value)


def test_connect_unknown_even_to_sql():
    with pytest.raises(LookupError, match="SQL generation supports") as exc:
        connect("not-a-warehouse")
    message = str(exc.value)
    assert "shipped:" in message
    assert "bigquery" in message
    assert "snowflake" in message
    assert "duckdb" in message


def test_connect_bigquery_constructs_without_google():
    adapter = connect("bigquery", project="my-proj", location="EU")
    assert adapter.dialect == "bigquery"


def test_connect_snowflake_constructs_without_connector():
    adapter = connect(
        "snowflake",
        account="xy12345",
        user="ANALYST",
        warehouse="COMPUTE_WH",
        database="ANALYTICS",
        schema="MARTS",
        private_key_path="rsa_key.p8",
    )
    assert adapter.dialect == "snowflake"
    assert adapter.capabilities == frozenset()


def test_snowflake_constructor_rejects_bigquery_project():
    with pytest.raises(TypeError):
        connect(
            "snowflake",
            account="xy12345",
            user="ANALYST",
            warehouse="COMPUTE_WH",
            database="ANALYTICS",
            schema="MARTS",
            private_key_path="rsa_key.p8",
            project="my-proj",
        )


def test_adapters_lists_shipped_kinds():
    assert dict(ADAPTERS) == {
        "bigquery": "factcat.warehouses.bigquery:BigQueryAdapter",
        "snowflake": "factcat.warehouses.snowflake:SnowflakeAdapter",
    }


def test_capabilities_differ_by_kind():
    assert CAP_DRY_RUN in capabilities("bigquery")
    assert CAP_SCAN_CAP in capabilities("bigquery")
    assert CAP_DRY_RUN not in capabilities("snowflake")
    assert CAP_SCAN_CAP not in capabilities("snowflake")


def test_warehouses_package_does_not_import_google():
    source = Path(warehouses_init).read_text(encoding="utf-8")
    assert "google.cloud" not in source
    assert "google.oauth2" not in source
    assert "from .bigquery" not in source
    assert "from .snowflake" not in source
    assert "snowflake.connector" not in source
    assert "importlib.import_module(module_name)" in source
    assert "subprocess" not in source


def test_core_init_does_not_import_execute_layer():
    source = Path(factcat_init).read_text(encoding="utf-8")
    assert "google" not in source
    assert "warehouses" not in source


def test_google_is_optional_extra_not_a_core_dependency():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    core, _, rest = text.partition("[project.optional-dependencies]")
    extras, _, _ = rest.partition("\n[")
    assert "google" not in core
    assert "snowflake-connector" not in core
    assert "google-cloud-bigquery" in extras
    assert "snowflake-connector-python" in extras
    # Product (the app) is default; warehouse SDKs are not.
    assert "fastapi" in core
    # Extra names minus dev/all match connect(kind=). Privileging the first
    # warehouse as a core dep, or shipping an adapter with no extra, goes red.
    extra_names = {
        line.split("=")[0].strip()
        for line in extras.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert extra_names == {"dev", "all", *ADAPTERS}
    assert "factcat[bigquery,snowflake]" in extras or (
        "factcat[bigquery]" in extras and "factcat[snowflake]" in extras
    )


def test_schema_grants_ignore_other_roles():
    from factcat.warehouses.snowflake import _schema_privileges_from_grants

    rows = [
        {"privilege": "CREATE TABLE", "grantee_name": "ACCOUNTADMIN"},
        {"privilege": "USAGE", "grantee_name": "ANALYST"},
        {"privilege": "CREATE TABLE", "grantee_name": "PUBLIC"},
    ]
    privs = _schema_privileges_from_grants(rows, role="ANALYST")
    assert "USAGE" in privs
    assert "CREATE TABLE" in privs  # PUBLIC
    only = _schema_privileges_from_grants(rows[:2], role="ANALYST")
    assert "CREATE TABLE" not in only
    assert "USAGE" in only


def test_adapter_error_hierarchy():
    assert issubclass(DryRunNotSupported, AdapterError)
    assert inspect.isclass(Adapter)


def test_is_missing_relation():
    assert is_missing_relation(AdapterError("not found"))
    assert is_missing_relation(
        AdapterError("Not found: Table dest.analytics.fc_event_names")
    )
    assert is_missing_relation(
        AdapterError("Object 'A.B.FC_EVENT_NAMES' does not exist or not authorized.")
    )
    assert not is_missing_relation(AdapterError("Access Denied"))
    assert not is_missing_relation(
        AdapterError("service-account JSON not found: /tmp/key.json")
    )
    assert is_missing_relation(AdapterError("nope", not_found=True))
