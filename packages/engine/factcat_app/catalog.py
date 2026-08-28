"""BigQuery metadata for the mapping wizard. Not SQL generation."""

from __future__ import annotations

from typing import Any

from factcat.warehouses import AdapterError
from factcat.warehouses.bigquery import (
    adc_quota_project,
    list_columns,
    list_datasets,
    list_tables,
)

# BigQuery SchemaField.field_type names. Entity is an id for COUNT DISTINCT —
# strings and exact numerics, not floats, bools, or timestamps.
ENTITY_TYPES = frozenset(
    {"STRING", "INT64", "INTEGER", "NUMERIC", "BIGNUMERIC"}
)
# Instants, not calendar DATE — product analytics is event-time.
TIME_TYPES = frozenset({"TIMESTAMP", "DATETIME"})
# Compared as a SQL string literal in the form (event_column = 'paid').
EVENT_NAME_TYPES = frozenset({"STRING"})


def column_fits(field_type: str, role: str) -> bool:
    allowed = {
        "entity": ENTITY_TYPES,
        "event_time": TIME_TYPES,
        "event_column": EVENT_NAME_TYPES,
    }[role]
    return (field_type or "").strip().upper() in allowed


def _creds(form: dict[str, Any]) -> str | None:
    raw = (form.get("credentials") or "").strip()
    return raw or None


def datasets_from_form(form: dict[str, Any]) -> list[dict[str, str]]:
    project = (form.get("data_project") or form.get("project") or "").strip()
    if not project:
        raise ValueError("project is required")
    return list_datasets(project=project, credentials=_creds(form))


def tables_from_form(form: dict[str, Any]) -> dict[str, Any]:
    project = (form.get("data_project") or form.get("project") or "").strip()
    dataset = (form.get("dataset") or "").strip()
    if not project:
        raise ValueError("project is required")
    return list_tables(project=project, dataset=dataset, credentials=_creds(form))


def columns_from_form(form: dict[str, Any]) -> dict[str, Any]:
    project = (form.get("data_project") or form.get("project") or "").strip()
    dataset = (form.get("dataset") or "").strip()
    table = (form.get("table_name") or "").strip()
    if not project:
        raise ValueError("project is required")
    payload = list_columns(
        project=project,
        dataset=dataset,
        table=table,
        credentials=_creds(form),
    )
    columns = list(payload.get("columns") or [])
    columns.sort(key=lambda c: str(c.get("name") or "").lower())
    payload["columns"] = columns
    return payload


def bootstrap_project() -> str:
    return adc_quota_project()


__all__ = [
    "AdapterError",
    "ENTITY_TYPES",
    "EVENT_NAME_TYPES",
    "TIME_TYPES",
    "bootstrap_project",
    "column_fits",
    "columns_from_form",
    "datasets_from_form",
    "tables_from_form",
]
