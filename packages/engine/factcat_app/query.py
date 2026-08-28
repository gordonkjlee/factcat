"""Turn the form into an EventsSpec. Identifiers only — no free SQL."""

from __future__ import annotations

import re
from typing import Any

from datetime import date, timedelta

from factcat import EVENT_MEASURES, EventsSpec, events_sql
from factcat.warehouses.bigquery import DEFAULT_MAXIMUM_BYTES_BILLED
from factcat._emit import transpile
from factcat.dialects import splice_placeholders

_TABLE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_]+)+$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GRAINS = ("day", "week", "month")
_GRAIN_RANK = {"day": 0, "week": 1, "month": 2, "quarter": 3, "year": 4}
_UNITS = frozenset({"day", "week", "month", "quarter", "year"})
_MODES = frozenset({"last", "this", "previous", "custom"})
_LAST_N = frozenset({"7", "30", "90", "365"})
# Last-N defaults when the saved window unit does not match the chart grain.
_DEFAULT_LAST = {"day": 30, "week": 8, "month": 6}
EVENT_VALUE_LIMIT = 1000
# Crash fuse, not a display cap. Slice-and-dice (grain × property)
# is routinely tens of thousands of rows; grouping by a near-unique
# key is when you hit seven figures. No product max: a report can
# double the LIMIT until the result fits or the process/tab dies.
DEFAULT_QUERY_ROW_LIMIT = 1_000_000
# Catalog DISTINCT is not all-time: unbounded scans blow the 10 GiB job cap.
CATALOG_LOOKBACK_DAYS = 90


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


def _iso_date(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _ISO_DATE.match(value):
        raise ValueError(f"{label} must be YYYY-MM-DD")
    return value


def _week_start(form: dict[str, Any]) -> str:
    raw = str(form.get("week_start") or "monday").strip().lower()
    if raw not in {"monday", "sunday"}:
        raise ValueError("week_start must be monday or sunday")
    return raw


def _range_unit(form: dict[str, Any], default: str = "day") -> str:
    raw = str(form.get("range_unit") or default).strip().lower()
    if raw not in _UNITS:
        raise ValueError("range_unit must be day, week, month, quarter, or year")
    return raw


def _range_n(form: dict[str, Any]) -> int:
    raw = form.get("range_n", 30)
    if raw in (None, ""):
        raw = 30
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("range_n must be an integer") from exc
    if n < 1 or n > 3650:
        raise ValueError("range_n must be between 1 and 3650")
    return n


def _rel_n(form: dict[str, Any], key: str, default: int) -> int:
    raw = form.get(key, default)
    if raw in (None, ""):
        raw = default
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if n < 0 or n > 3650:
        raise ValueError(f"{key} must be between 0 and 3650")
    return n


def _grain(form: dict[str, Any]) -> str:
    raw = str(form.get("grain") or "day").strip().lower()
    if raw not in _GRAINS:
        raise ValueError("grain must be day, week, or month")
    return raw


def _include_current(form: dict[str, Any], grain: str) -> bool:
    """Day windows include today. Week/month last-N is complete periods
    unless include_current is set. Not a library period enum."""
    if grain == "day":
        return True
    raw = form.get("include_current")
    if raw is not None and raw != "":
        return raw in (True, "true", "on", "1", 1)
    if "exclude_current" in form:
        return form.get("exclude_current") not in (True, "true", "on", "1", 1)
    return False


def _exclude_current(form: dict[str, Any]) -> bool:
    """Last-N complete periods: week/month default on, day never."""
    grain = str(form.get("grain") or "day").strip().lower()
    if grain not in _GRAINS:
        grain = "day"
    return not _include_current(form, grain)


def _grain_start(d: date, grain: str, week_start: str) -> date:
    if grain == "day":
        return d
    if grain == "month":
        return d.replace(day=1)
    weekday = d.weekday()
    delta = (weekday + 1) % 7 if week_start == "sunday" else weekday
    return d - timedelta(days=delta)


def _grain_next(d: date, grain: str, week_start: str) -> date:
    start = _grain_start(d, grain, week_start)
    if grain == "day":
        return start + timedelta(days=1)
    if grain == "week":
        return start + timedelta(days=7)
    month = 1 if start.month == 12 else start.month + 1
    year = start.year + 1 if start.month == 12 else start.year
    return date(year, month, 1)


def _parse_bucket(value: Any) -> date | None:
    raw = str(value or "")
    match = _ISO_DATE.match(raw[:10]) if raw else None
    if not match:
        return None
    return date.fromisoformat(match.group(0))


def _ps(expr: str, unit: str, form: dict[str, Any], n: int) -> str:
    return (
        f"factcat_period_start_shifted({expr}, '{unit}', "
        f"'{_week_start(form)}', {n})"
    )


def _normalize_range(
    form: dict[str, Any],
) -> tuple[str, str, int]:
    grain = str(form.get("grain") or "day").strip().lower()
    if grain not in _GRAINS:
        grain = "day"
    mode = str(form.get("range_mode") or "").strip().lower()
    if mode not in _MODES:
        preset = str(form.get("range_preset") or "").strip()
        if preset in _LAST_N:
            mode, unit, n = "last", "day", int(preset)
        elif preset == "this_week":
            mode, unit, n = "this", "week", 1
        elif preset == "this_month":
            mode, unit, n = "this", "month", 1
        elif preset == "custom":
            mode, unit, n = "custom", "day", 1
        else:
            mode, unit, n = "last", "day", _lookback_days(form)
    else:
        unit = _range_unit(form)
        n = _range_n(form) if mode == "last" else 1
    if mode == "custom":
        return mode, grain, 1
    if mode == "last":
        if unit != grain:
            n = _DEFAULT_LAST[grain]
        return mode, grain, n
    # this / previous: window may be coarser than the chart grain
    # (this week, daily bars). A finer window than the grain is the
    # partial-bucket trap — bump it up.
    if _GRAIN_RANK.get(unit, -1) < _GRAIN_RANK.get(grain, 0):
        unit = grain
    return mode, unit, 1


def _time_clauses(form: dict[str, Any], event_time: str) -> list[str]:
    """Sugar on event_time. Window unit follows the chart grain."""
    mode, unit, n = _normalize_range(form)
    if mode == "custom":
        kind = str(form.get("custom_kind") or "absolute").strip().lower()
        if kind == "relative":
            start_n = _rel_n(form, "rel_start_n", 12)
            end_n = _rel_n(form, "rel_end_n", 0)
            if start_n < end_n:
                raise ValueError("relative from must be at least as far back as to")
            if unit == "day":
                clauses = [f"{event_time} >= current_date - {start_n}"]
                if end_n > 0:
                    clauses.append(f"{event_time} < current_date - {end_n - 1}")
                return clauses
            clauses = [f"{event_time} >= {_ps('current_date', unit, form, -start_n)}"]
            if end_n > 0:
                clauses.append(
                    f"{event_time} < {_ps('current_date', unit, form, -(end_n - 1))}"
                )
            return clauses
        start = date.fromisoformat(
            _iso_date(str(form.get("start_date") or ""), "start_date")
        )
        end = date.fromisoformat(
            _iso_date(str(form.get("end_date") or ""), "end_date")
        )
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        week_start = _week_start(form)
        start = _grain_start(start, unit, week_start)
        end_exclusive = _grain_next(end, unit, week_start)
        return [
            f"{event_time} >= DATE {_sql_string(start.isoformat())}",
            f"{event_time} < DATE {_sql_string(end_exclusive.isoformat())}",
        ]
    if mode == "this":
        return [f"{event_time} >= {_ps('current_date', unit, form, 0)}"]
    if mode == "previous":
        return [
            f"{event_time} >= {_ps('current_date', unit, form, -1)}",
            f"{event_time} < {_ps('current_date', unit, form, 0)}",
        ]
    if unit == "day":
        return [f"{event_time} >= current_date - {n}"]
    if _exclude_current(form):
        return [
            f"{event_time} >= {_ps('current_date', unit, form, -n)}",
            f"{event_time} < {_ps('current_date', unit, form, 0)}",
        ]
    return [f"{event_time} >= {_ps('current_date', unit, form, -(n - 1))}"]


def annotate_incomplete(
    rows: list[dict[str, Any]],
    form: dict[str, Any],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Mark the current grain when the window includes it. Trailing only."""
    today = today or date.today()
    grain = str(form.get("grain") or "day").strip().lower()
    if grain not in _GRAINS:
        grain = "day"
    current = _grain_start(today, grain, _week_start(form))
    mode, _unit, _n = _normalize_range(form)
    hide_current = mode == "previous" or (
        mode == "last" and not _include_current(form, grain)
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        bucket = _parse_bucket(row.get("bucket"))
        item = dict(row)
        item["incomplete"] = bool(
            bucket is not None and bucket == current and not hide_current
        )
        out.append(item)
    return out


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
    grain = _grain(form)
    exact_raw = form.get("exact")
    exact = exact_raw in (True, "true", "on", "1", 1)

    # Integer days, not INTERVAL: sqlglot's INTERVAL '30' DAY is not BigQuery.
    clauses = _time_clauses(form, event_time)
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
        bucket=(
            f"CAST({_ps(event_time, 'week', form, 0)} AS DATE)"
            if grain == "week"
            else f"CAST(date_trunc('{grain}', {event_time}) AS DATE)"
        ),
        where=" AND ".join(clauses),
        exact=exact,
    )


def query_row_limit(form: dict[str, Any]) -> int:
    """Crash fuse on aggregated result rows. Most recent N."""
    raw = form.get("query_row_limit_run")
    if raw in (None, ""):
        raw = form.get("query_row_limit", DEFAULT_QUERY_ROW_LIMIT)
    if raw in (None, ""):
        raw = DEFAULT_QUERY_ROW_LIMIT
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("query_row_limit must be an integer") from exc
    if n < 1:
        raise ValueError("query_row_limit must be at least 1")
    return n


def events_sql_from_form(form: dict[str, Any]) -> str:
    """Events SQL with a result-row crash fuse (most recent rows)."""
    sql = events_sql(spec_from_form(form), dialect="bigquery").rstrip()
    n = query_row_limit(form)
    return (
        f"SELECT * FROM (\n"
        f"  SELECT * FROM (\n{sql}\n  )\n"
        f"  ORDER BY bucket DESC\n"
        f"  LIMIT {n}\n"
        f")\n"
        f"ORDER BY bucket"
    )


def job_bytes_cap(form: dict[str, Any]) -> int | None:
    """Factcat job cap in bytes. None is unlimited. Not a GCP default."""
    override = form.get("override_cap") in (True, "true", "on", "1", 1)
    raw = form.get("bytes_cap_override_gb" if override else "bytes_cap_gb")
    if raw in (None, ""):
        return None if override else DEFAULT_MAXIMUM_BYTES_BILLED
    try:
        gb = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("bytes cap must be a number of GB") from exc
    if gb <= 0:
        return None
    return int(gb * (1024**3))


def event_values_sql(form: dict[str, Any]) -> str:
    """DISTINCT event names. Catalog mode is last 90 days on event_time so
    a partitioned events table can prune; it is not an all-time scan.
    """
    table = _ident_table(str(form.get("table") or ""), "table")
    event_column = _ident_column(str(form.get("event_column") or ""), "event_column")
    event_time = _ident_column(str(form.get("event_time") or ""), "event_time")
    catalog = form.get("catalog") in (True, "true", "on", "1", 1)
    if catalog:
        where = (
            f"{event_column} IS NOT NULL "
            f"AND {event_time} >= current_date - {CATALOG_LOOKBACK_DAYS}"
        )
    else:
        where = " AND ".join(
            [f"{event_column} IS NOT NULL"] + _time_clauses(form, event_time)
        )
    sql = (
        f"SELECT DISTINCT {event_column} AS fc_value "
        f"FROM {table} "
        f"WHERE {where} "
        f"ORDER BY 1 "
        f"LIMIT {EVENT_VALUE_LIMIT}"
    )
    return splice_placeholders(transpile(sql, "bigquery"), "bigquery")


def connection_from_form(form: dict[str, Any]) -> dict[str, Any]:
    project = (form.get("project") or "").strip()
    location = (form.get("location") or "").strip()
    if not project:
        raise ValueError("project is required")
    if not location:
        raise ValueError("location is required")
    out: dict[str, Any] = {
        "project": project,
        "location": location,
        "maximum_bytes_billed": job_bytes_cap(form),
    }
    credentials = (form.get("credentials") or "").strip()
    if credentials:
        out["credentials"] = credentials
    return out
