"""Turn the form into an EventsSpec. Identifiers only — no free SQL."""

from __future__ import annotations

import re
from typing import Any

from factcat import EVENT_MEASURES, EventsSpec
from factcat._emit import transpile

_TABLE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_]+)+$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GRAINS = ("day", "week", "month")
EVENT_VALUE_LIMIT = 1000


def _ident_table(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _TABLE.match(value):
        raise ValueError(
            f"{label} must be dataset.table or project.dataset.table"
        )
    # DuckDB-quoted so sqlglot can parse hyphenated GCP project ids, then
    # emit BigQuery backticks. Unquoted my-proj.ds.t is subtraction.
    return ".".join(f'"{part}"' for part in value.split("."))


def _ident_column(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _COLUMN.match(value):
        raise ValueError(f"{label} must be a column name")
    return value


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _lookback_days(form: dict[str, Any]) -> int:
    raw_lookback = form.get("lookback_days", 30)
    if raw_lookback in (None, ""):
        raw_lookback = 30
    try:
        lookback = int(raw_lookback)
    except (TypeError, ValueError) as exc:
        raise ValueError("lookback_days must be an integer") from exc
    if lookback < 1 or lookback > 3650:
        raise ValueError("lookback_days must be between 1 and 3650")
    return lookback


def spec_from_form(form: dict[str, Any]) -> EventsSpec:
    table = _ident_table(str(form.get("table") or ""), "table")
    entity = (form.get("entity") or "").strip()
    if not entity:
        raise ValueError("entity is required (no default column)")
    entity = _ident_column(entity, "entity")
    event_time = _ident_column(str(form.get("event_time") or ""), "event_time")
    measure = str(form.get("measure") or "uniques")
    if measure not in EVENT_MEASURES:
        raise ValueError("measure must be total, uniques, or average")
    grain = str(form.get("grain") or "day")
    if grain not in _GRAINS:
        raise ValueError("grain must be day, week, or month")
    lookback = _lookback_days(form)

    exact_raw = form.get("exact")
    exact = exact_raw in (True, "true", "on", "1", 1)

    # Integer days, not INTERVAL: sqlglot's INTERVAL '30' DAY is not BigQuery.
    # DuckDB and BigQuery both treat DATE - INT as days.
    clauses = [f"{event_time} >= current_date - {lookback}"]
    event_column = (form.get("event_column") or "").strip()
    event_value = (form.get("event_value") or "").strip()
    if event_value and not event_column:
        raise ValueError("event name requires an event column")
    if event_column and event_value:
        event_column = _ident_column(event_column, "event_column")
        clauses.append(f"{event_column} = {_sql_string(event_value)}")

    return EventsSpec(
        table=table,
        entity=entity,
        event_time=event_time,
        measure=measure,  # type: ignore[arg-type]
        on="events",
        bucket=f"CAST(date_trunc('{grain}', {event_time}) AS DATE)",
        where=" AND ".join(clauses),
        exact=exact,
    )


def event_values_sql(form: dict[str, Any]) -> str:
    """DISTINCT event names in the lookback window. Identifiers only."""
    table = _ident_table(str(form.get("table") or ""), "table")
    event_column = _ident_column(str(form.get("event_column") or ""), "event_column")
    event_time = _ident_column(str(form.get("event_time") or ""), "event_time")
    lookback = _lookback_days(form)
    sql = (
        f"SELECT DISTINCT {event_column} AS fc_value "
        f"FROM {table} "
        f"WHERE {event_column} IS NOT NULL "
        f"AND {event_time} >= current_date - {lookback} "
        f"ORDER BY 1 "
        f"LIMIT {EVENT_VALUE_LIMIT}"
    )
    return transpile(sql, "bigquery")


def connection_from_form(form: dict[str, Any]) -> dict[str, str]:
    project = (form.get("project") or "").strip()
    location = (form.get("location") or "").strip()
    if not project:
        raise ValueError("project is required")
    if not location:
        raise ValueError("location is required")
    out = {"project": project, "location": location}
    credentials = (form.get("credentials") or "").strip()
    if credentials:
        out["credentials"] = credentials
    return out
