"""Warehouse metadata for the mapping wizard. Not SQL generation."""

from __future__ import annotations

from typing import Any

from factcat.warehouses import AdapterError
from factcat_app.config import warehouse_kind
from factcat.warehouses.bigquery import (
    adc_quota_project,
    list_columns,
    list_datasets,
    list_tables,
)
from factcat.warehouses.snowflake import (
    DISTINCT_OF_TYPES as SF_DISTINCT_OF_TYPES,
    ENTITY_TYPES as SF_ENTITY_TYPES,
    EVENT_NAME_TYPES as SF_EVENT_NAME_TYPES,
    INSTANT_TIME_TYPES as SF_INSTANT_TIME_TYPES,
    JSON_TYPES as SF_JSON_TYPES,
    PROPERTY_OF_TYPES as SF_PROPERTY_OF_TYPES,
    TIME_TYPES as SF_TIME_TYPES,
    UNIX_TIME_TYPES as SF_UNIX_TIME_TYPES,
    WALLCLOCK_TIME_TYPES as SF_WALLCLOCK_TIME_TYPES,
    list_columns as sf_list_columns,
    list_databases,
    list_schemas as sf_list_schemas,
    list_tables as sf_list_tables,
    passphrase_from_env,
)

# BigQuery SchemaField.field_type names. Entity is an id for COUNT DISTINCT —
# strings and exact numerics, not floats, bools, or timestamps.
ENTITY_TYPES = frozenset(
    {"STRING", "INT64", "INTEGER", "NUMERIC", "BIGNUMERIC"}
)
# Instants, not calendar DATE — product analytics is event-time.
TIME_TYPES = frozenset({"TIMESTAMP", "DATETIME"})
# Integer Unix epoch (seconds / ms / µs). Instant, not civil DATETIME.
UNIX_TIME_TYPES = frozenset({"INT64", "INTEGER", "INT", "BIGINT"})
# Compared as a SQL string literal in the form (event_column = 'paid').
EVENT_NAME_TYPES = frozenset({"STRING"})
# Sum / Average / Median of a column. FLOAT is fine here; not for entity ids.
PROPERTY_OF_TYPES = frozenset(
    {"INT64", "INTEGER", "NUMERIC", "BIGNUMERIC", "FLOAT64", "FLOAT"}
)
# Distinct … per entity: numeric or STRING.
DISTINCT_OF_TYPES = PROPERTY_OF_TYPES | {"STRING"}
# JSON columns: Events asks for a key and emits JSON_VALUE. Not a property store.
JSON_TYPES = frozenset({"JSON"})


def form_kind(form: dict[str, Any]) -> str:
    return warehouse_kind(form)


def type_sets(kind: str) -> dict[str, frozenset[str]]:
    if kind == "snowflake":
        return {
            "entity": SF_ENTITY_TYPES,
            "event_time": SF_TIME_TYPES | SF_UNIX_TIME_TYPES,
            "event_time_instant": SF_INSTANT_TIME_TYPES,
            "event_time_wallclock": SF_WALLCLOCK_TIME_TYPES,
            "event_time_unix": SF_UNIX_TIME_TYPES,
            "event_column": SF_EVENT_NAME_TYPES,
            "of": SF_PROPERTY_OF_TYPES | SF_JSON_TYPES,
            "of_distinct": SF_DISTINCT_OF_TYPES | SF_JSON_TYPES,
            "json": SF_JSON_TYPES,
        }
    return {
        "entity": ENTITY_TYPES,
        "event_time": TIME_TYPES | UNIX_TIME_TYPES,
        "event_time_instant": frozenset({"TIMESTAMP"}),
        "event_time_wallclock": frozenset({"DATETIME"}),
        "event_time_unix": UNIX_TIME_TYPES,
        "event_column": EVENT_NAME_TYPES,
        "of": PROPERTY_OF_TYPES | JSON_TYPES,
        "of_distinct": DISTINCT_OF_TYPES | JSON_TYPES,
        "json": JSON_TYPES,
    }


def _normalise_type(field_type: str) -> str:
    raw = (field_type or "").strip().upper()
    if "(" in raw:
        raw = raw.split("(", 1)[0].strip()
    return raw


def column_fits(field_type: str, role: str, *, kind: str = "bigquery") -> bool:
    allowed = type_sets(kind)[
        {
            "entity": "entity",
            "event_time": "event_time",
            "event_column": "event_column",
            "of": "of",
            "of_distinct": "of_distinct",
        }[role]
    ]
    return _normalise_type(field_type) in allowed


def _creds(form: dict[str, Any]) -> str | None:
    raw = (form.get("credentials") or "").strip()
    return raw or None


def _sf_auth(form: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "account": (form.get("account") or "").strip(),
        "user": (form.get("user") or "").strip(),
        "warehouse": (form.get("warehouse") or "").strip(),
        "private_key_path": (form.get("private_key_path") or "").strip(),
        "authenticator": (form.get("snowflake_auth") or form.get("authenticator") or "key_pair").strip()
        or "key_pair",
    }
    role = (form.get("role") or "").strip()
    if role:
        out["role"] = role
    passphrase = passphrase_from_env()
    if passphrase:
        out["private_key_passphrase"] = passphrase
    return out


def datasets_from_form(form: dict[str, Any]) -> list[dict[str, str]]:
    if form_kind(form) == "snowflake":
        return list_databases(**_sf_auth(form))
    project = (form.get("data_project") or form.get("project") or "").strip()
    if not project:
        raise ValueError("project is required")
    return list_datasets(project=project, credentials=_creds(form))


def schemas_from_form(form: dict[str, Any]) -> list[str]:
    if form_kind(form) != "snowflake":
        raise ValueError("schemas are a Snowflake catalog step")
    database = (form.get("database") or "").strip()
    if not database:
        raise ValueError("database is required")
    return sf_list_schemas(database=database, **_sf_auth(form))


def tables_from_form(form: dict[str, Any]) -> dict[str, Any]:
    if form_kind(form) == "snowflake":
        database = (form.get("database") or "").strip()
        schema = (form.get("schema") or "").strip()
        if not database:
            raise ValueError("database is required")
        if not schema:
            raise ValueError("schema is required")
        return sf_list_tables(database=database, schema=schema, **_sf_auth(form))
    project = (form.get("data_project") or form.get("project") or "").strip()
    dataset = (form.get("dataset") or "").strip()
    if not project:
        raise ValueError("project is required")
    return list_tables(project=project, dataset=dataset, credentials=_creds(form))


def columns_from_form(form: dict[str, Any]) -> dict[str, Any]:
    if form_kind(form) == "snowflake":
        database = (form.get("database") or "").strip()
        schema = (form.get("schema") or "").strip()
        table = (form.get("table_name") or "").strip()
        payload = sf_list_columns(
            database=database,
            schema=schema,
            table=table,
            **_sf_auth(form),
        )
    else:
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
    "DISTINCT_OF_TYPES",
    "JSON_TYPES",
    "ENTITY_TYPES",
    "EVENT_NAME_TYPES",
    "PROPERTY_OF_TYPES",
    "TIME_TYPES",
    "bootstrap_project",
    "column_fits",
    "columns_from_form",
    "datasets_from_form",
    "form_kind",
    "schemas_from_form",
    "tables_from_form",
    "type_sets",
]
