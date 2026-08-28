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
    return list_columns(
        project=project,
        dataset=dataset,
        table=table,
        credentials=_creds(form),
    )


def bootstrap_project() -> str:
    return adc_quota_project()


__all__ = [
    "AdapterError",
    "bootstrap_project",
    "columns_from_form",
    "datasets_from_form",
    "tables_from_form",
]
