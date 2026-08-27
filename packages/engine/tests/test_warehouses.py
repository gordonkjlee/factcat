"""Execute-adapter protocol: no Google, no warehouse identity on Warehouse."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from factcat import RetentionSpec, __file__ as factcat_init, retention_sql
from factcat.dialects import SUPPORTED
from factcat.warehouses import (
    ADAPTERS,
    DryRunNotSupported,
    QueryResult,
    Warehouse,
    WarehouseError,
    connect,
)
from factcat.warehouses import __file__ as warehouses_init


class FakeWarehouse:
    """Second adapter shape: no project, no location, no bytes cap."""

    dialect = "duckdb"

    def __init__(self) -> None:
        self.executed: list[str] = []

    def run(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        if dry_run:
            raise DryRunNotSupported("fake warehouse cannot dry-run")
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
    """If project/location leak onto Warehouse, the app-shaped fake breaks."""
    wh = FakeWarehouse()
    assert not hasattr(wh, "project")
    assert not hasattr(wh, "location")
    assert not hasattr(wh, "maximum_bytes_billed")
    hints = getattr(Warehouse, "__annotations__", {})
    assert "project" not in hints
    assert "location" not in hints
    assert "maximum_bytes_billed" not in hints


def test_app_shaped_generate_then_run():
    fake = FakeWarehouse()
    warehouse: Warehouse = fake
    sql = retention_sql(_spec(), dialect=warehouse.dialect)
    result = warehouse.run(sql)
    assert "period_index" in sql
    assert result.rows == [{"ok": True}]
    assert fake.executed == [sql]


def test_fake_dry_run_does_not_execute():
    wh = FakeWarehouse()
    with pytest.raises(DryRunNotSupported):
        wh.run("SELECT 1", dry_run=True)
    assert wh.executed == []


def test_connect_unknown_sql_dialect():
    with pytest.raises(LookupError, match="shipped: bigquery") as exc:
        connect("snowflake")
    assert "SQL generation supports" not in str(exc.value)
    assert "snowflake" in SUPPORTED


def test_connect_unknown_even_to_sql():
    with pytest.raises(LookupError, match="SQL generation supports") as exc:
        connect("not-a-warehouse")
    message = str(exc.value)
    assert "shipped: bigquery" in message
    assert "duckdb" in message


def test_connect_bigquery_constructs_without_google():
    warehouse = connect("bigquery", project="my-proj", location="EU")
    assert warehouse.dialect == "bigquery"


def test_adapters_lists_bigquery_only():
    assert dict(ADAPTERS) == {
        "bigquery": "factcat.warehouses.bigquery:BigQueryWarehouse"
    }


def test_warehouses_package_does_not_import_google():
    source = Path(warehouses_init).read_text(encoding="utf-8")
    assert "google.cloud" not in source
    assert "google.oauth2" not in source
    assert "from .bigquery" not in source
    assert "importlib.import_module(module_name)" in source


def test_core_init_does_not_import_execute_layer():
    source = Path(factcat_init).read_text(encoding="utf-8")
    assert "google" not in source
    assert "warehouses" not in source


def test_google_is_optional_extra_not_a_core_dependency():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    core, _, extras = text.partition("[project.optional-dependencies]")
    assert "google" not in core
    assert "google-cloud-bigquery" in extras


def test_warehouse_error_hierarchy():
    assert issubclass(DryRunNotSupported, WarehouseError)
    assert inspect.isclass(Warehouse)
