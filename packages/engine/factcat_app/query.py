"""Turn the form into an EventsSpec. Identifiers only — no free SQL."""

from __future__ import annotations

import re
from typing import Any

from datetime import date, datetime, timedelta, timezone

from factcat import EVENT_MEASURES, PROPERTY_MEASURES, EventsSpec, events_sql
from factcat.spec import BREAKDOWN_AT
from factcat.warehouses import CAP_SCAN_CAP, capabilities
from factcat.warehouses.bigquery import DEFAULT_MAXIMUM_BYTES_BILLED
from factcat.warehouses.snowflake import passphrase_from_env
from factcat._emit import transpile
from factcat.dialects import json_value_sql, splice_placeholders
from factcat_app.config import WAREHOUSE_KINDS

_TABLE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_]+)+$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_JSON_KEY = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
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
            f"{label} must be dataset.table, project.dataset.table, "
            "or database.schema.table"
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


REPORTING_TIMEZONES = (
    "UTC",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Amsterdam",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Dubai",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Pacific/Auckland",
)


def _reporting_timezone(form: dict[str, Any]) -> str:
    raw = str(form.get("reporting_timezone") or "UTC").strip() or "UTC"
    if raw not in REPORTING_TIMEZONES:
        raise ValueError("reporting_timezone must be an IANA name from Setup")
    return raw


_EPOCH = {
    "seconds": "unix_s",
    "s": "unix_s",
    "unix_s": "unix_s",
    "milliseconds": "unix_ms",
    "ms": "unix_ms",
    "unix_ms": "unix_ms",
    "microseconds": "unix_us",
    "us": "unix_us",
    "unix_us": "unix_us",
}


def _event_time_kind(form: dict[str, Any]) -> str:
    epoch = str(form.get("event_time_epoch") or "").strip().lower()
    if epoch:
        mapped = _EPOCH.get(epoch)
        if mapped is None:
            raise ValueError(
                "event_time_epoch must be seconds, milliseconds, or microseconds"
            )
        return mapped
    raw = str(form.get("event_time_tz") or "utc").strip().lower()
    if raw not in {"utc", "reporting", "instant"}:
        raise ValueError("event_time_tz must be utc, reporting, or instant")
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
    """Last-N: include the in-progress grain (today / this week / this month).

    Unset defaults: day includes today; week and month are complete periods.
    Not a library period enum.
    """
    raw = form.get("include_current")
    if raw is not None and raw != "":
        return raw in (True, "true", "on", "1", 1)
    if "exclude_current" in form:
        return form.get("exclude_current") not in (True, "true", "on", "1", 1)
    return grain == "day"


def _exclude_current(form: dict[str, Any]) -> bool:
    """Last-N complete periods. Inverse of ``_include_current``."""
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


def form_kind(form: dict[str, Any]) -> str:
    raw = str(form.get("kind") or "bigquery").strip().lower() or "bigquery"
    if raw not in WAREHOUSE_KINDS:
        raise ValueError("kind must be " + ", ".join(WAREHOUSE_KINDS))
    return raw


def _today_sql(form: dict[str, Any]) -> str:
    return _ps("current_date", "day", form, 0)


def _as_event_time(date_sql: str, form: dict[str, Any]) -> str:
    """Bound an event_time column with a DATE expression in the column's type."""
    tz = _reporting_timezone(form)
    kind = _event_time_kind(form)
    return f"factcat_ts_at_date({date_sql}, {_sql_string(tz)}, '{kind}')"


def _event_time_lhs(event_time: str, form: dict[str, Any]) -> str:
    """Canonical instant. DATETIME stored as UTC CASTs to TIMESTAMP.

    Used for the window compare, ``fc_event_ts`` in the SELECT list, and
    first/last attribution. sqlglot rewrites a raw CAST to DATETIME;
    ``factcat_as_instant`` is spliced after transpile.
    """
    kind = _event_time_kind(form)
    if kind == "reporting":
        return event_time
    if kind.startswith("unix_"):
        return f"factcat_as_instant({event_time}, '{kind}')"
    return f"factcat_as_instant({event_time})"


def _ps(expr: str, unit: str, form: dict[str, Any], n: int) -> str:
    return (
        f"factcat_period_start_shifted({expr}, '{unit}', "
        f"'{_week_start(form)}', {n}, "
        f"{_sql_string(_reporting_timezone(form))}, "
        f"'{_event_time_kind(form)}')"
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
    lhs = _event_time_lhs(event_time, form)
    if mode == "custom":
        kind = str(form.get("custom_kind") or "absolute").strip().lower()
        if kind == "relative":
            start_n = _rel_n(form, "rel_start_n", 12)
            end_n = _rel_n(form, "rel_end_n", 0)
            if start_n < end_n:
                raise ValueError("relative from must be at least as far back as to")
            if unit == "day":
                clauses = [
                    f"{lhs} >= {_as_event_time(_ps('current_date', 'day', form, -start_n), form)}"
                ]
                if end_n > 0:
                    clauses.append(
                        f"{lhs} < {_as_event_time(_ps('current_date', 'day', form, -(end_n - 1)), form)}"
                    )
                return clauses
            clauses = [
                f"{lhs} >= {_as_event_time(_ps('current_date', unit, form, -start_n), form)}"
            ]
            if end_n > 0:
                clauses.append(
                    f"{lhs} < {_as_event_time(_ps('current_date', unit, form, -(end_n - 1)), form)}"
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
            f"{lhs} >= {_as_event_time('DATE ' + _sql_string(start.isoformat()), form)}",
            f"{lhs} < {_as_event_time('DATE ' + _sql_string(end_exclusive.isoformat()), form)}",
        ]
    if mode == "this":
        return [
            f"{lhs} >= {_as_event_time(_ps('current_date', unit, form, 0), form)}"
        ]
    if mode == "previous":
        return [
            f"{lhs} >= {_as_event_time(_ps('current_date', unit, form, -1), form)}",
            f"{lhs} < {_as_event_time(_ps('current_date', unit, form, 0), form)}",
        ]
    if unit == "day":
        clauses = [
            f"{lhs} >= {_as_event_time(_ps('current_date', 'day', form, -n), form)}"
        ]
        if _exclude_current(form):
            clauses.append(
                f"{lhs} < {_as_event_time(_today_sql(form), form)}"
            )
        return clauses
    if _exclude_current(form):
        return [
            f"{lhs} >= {_as_event_time(_ps('current_date', unit, form, -n), form)}",
            f"{lhs} < {_as_event_time(_ps('current_date', unit, form, 0), form)}",
        ]
    return [
        f"{lhs} >= {_as_event_time(_ps('current_date', unit, form, -(n - 1)), form)}"
    ]


def annotate_incomplete(
    rows: list[dict[str, Any]],
    form: dict[str, Any],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Mark the current grain when the window includes it. Trailing only."""
    if today is None:
        tz = _reporting_timezone(form)
        if tz == "UTC":
            today = datetime.now(timezone.utc).date()
        else:
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo(tz)).date()
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


def _bool(form: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = form.get(key)
    if raw in (None, ""):
        return default
    return raw in (True, "true", "on", "1", 1)


def _top_n(form: dict[str, Any]) -> int:
    raw = form.get("top_n", 8)
    if raw in (None, ""):
        raw = 8
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("top_n must be an integer") from exc
    if n < 1:
        raise ValueError("top_n must be at least 1")
    return n


def _json_path(key: str, label: str) -> str:
    raw = (key or "").strip()
    if raw.startswith("$."):
        raw = raw[2:]
    if not raw or not _JSON_KEY.match(raw):
        raise ValueError(f"{label} JSON key must be a dotted name (plan or user.plan)")
    return "$." + raw


def _json_value_sql(
    column: str, key: str, label: str, *, numeric: bool, dialect: str
) -> str:
    col = _ident_column(column, label)
    path = _json_path(key, label)
    return json_value_sql(col, path, dialect, numeric=numeric)


def _json_label(key: str) -> str:
    return key.strip().removeprefix("$.").replace(".", "_")


def _single_expr(raw: str, label: str) -> str:
    expr = (raw or "").strip()
    if not expr:
        return ""
    lowered = expr.lower()
    if ";" in expr or "--" in expr or "/*" in expr:
        raise ValueError(f"{label} must be a single SQL expression")
    if lowered.startswith("select ") or " drop " in f" {lowered} ":
        raise ValueError(f"{label} must be a single SQL expression")
    return expr


def _breakdown_from_form(form: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
    """Fill ``breakdowns`` from a column, JSON key, or SQL expression. Expression wins."""
    expr = _single_expr(str(form.get("breakdown_expr") or ""), "breakdown expression")
    column = (form.get("breakdown_column") or "").strip()
    if expr:
        return (expr,), None
    if not column:
        return (), None
    json_key = (form.get("breakdown_json_key") or "").strip()
    if json_key:
        extracted = _json_value_sql(
            column, json_key, "breakdown", numeric=False, dialect=form_kind(form)
        )
        label = _json_label(json_key)
        if not _COLUMN.match(label):
            label = "json_key"
        return (extracted,), (label,)
    column = _ident_column(column, "breakdown")
    return (column,), (column,)


def _of_from_form(form: dict[str, Any], *, measure: str) -> str:
    """Property ``of=``. Expression wins over JSON key over column. Empty if unused."""
    expr = _single_expr(str(form.get("of_expr") or ""), "of")
    if expr:
        return expr
    column = (form.get("of_column") or "").strip()
    if not column:
        return ""
    json_key = (form.get("of_json_key") or "").strip()
    if json_key:
        numeric = measure in {"sum", "average", "median"}
        return _json_value_sql(
            column, json_key, "of", numeric=numeric, dialect=form_kind(form)
        )
    return _ident_column(column, "of")


def _measure_from_form(form: dict[str, Any]) -> tuple[str, str]:
    """Return ``(on, measure)``. ``property_average`` is the form value for property Average."""
    raw = str(form.get("measure") or "uniques").strip().lower()
    on_raw = str(form.get("on") or "").strip().lower()
    if raw == "property_average" or (on_raw == "property" and raw == "average"):
        return "property", "average"
    if raw in PROPERTY_MEASURES and raw != "average":
        return "property", raw
    if raw in EVENT_MEASURES:
        return "events", raw
    raise ValueError("measure must be total, uniques, average, sum, median, or distinct")


def spec_from_form(form: dict[str, Any]) -> EventsSpec:
    table = _ident_table(str(form.get("table") or ""), "table")
    entity = (form.get("entity") or "").strip()
    if not entity:
        raise ValueError("entity is required (no default column)")
    entity = _ident_column(entity, "entity")
    event_time_col = _ident_column(str(form.get("event_time") or ""), "event_time")
    on, measure = _measure_from_form(form)
    grain = _grain(form)
    exact_raw = form.get("exact")
    exact = exact_raw in (True, "true", "on", "1", 1)

    clauses = _time_clauses(form, event_time_col)
    event_column = (form.get("event_column") or "").strip()
    event_value = (form.get("event_value") or "").strip()
    if event_value and not event_column:
        raise ValueError("event name requires an event column")
    if event_column and event_value:
        event_column = _ident_column(event_column, "event_column")
        clauses.append(f"{event_column} = {_sql_string(event_value)}")
    event_time = _event_time_lhs(event_time_col, form)

    of = _of_from_form(form, measure=measure) if on == "property" else None
    if on == "property" and not of:
        raise ValueError("of is required for property measures")

    breakdowns, breakdown_labels = _breakdown_from_form(form)
    breakdown_at = str(form.get("breakdown_at") or "rows").strip().lower()
    if breakdown_at not in BREAKDOWN_AT:
        raise ValueError("breakdown_at must be rows, first, or last")

    return EventsSpec(
        table=table,
        entity=entity,
        event_time=event_time,
        measure=measure,  # type: ignore[arg-type]
        on=on,  # type: ignore[arg-type]
        of=of,
        bucket=f"CAST({_ps('fc_event_ts', grain, form, 0)} AS DATE)",
        where=" AND ".join(clauses),
        exact=exact,
        breakdowns=breakdowns,
        breakdown_at=breakdown_at,  # type: ignore[arg-type]
        breakdown_labels=breakdown_labels,
        top_n=_top_n(form),
        include_other=_bool(form, "include_other", True),
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


def _indent_sql(sql: str, n: int = 4) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in sql.splitlines())


def events_sql_from_form(form: dict[str, Any]) -> str:
    """Events SQL with a result-row crash fuse (most recent rows)."""
    sql = events_sql(spec_from_form(form), dialect=form_kind(form)).rstrip()
    n = query_row_limit(form)
    inner = _indent_sql(sql, 4)
    return (
        f"SELECT * FROM (\n"
        f"  SELECT * FROM (\n{inner}\n  ) AS _fc_inner\n"
        f"  ORDER BY CAST(bucket AS DATE) DESC\n"
        f"  LIMIT {n}\n"
        f") AS _fc_recent\n"
        f"ORDER BY CAST(bucket AS DATE)"
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
            f"AND {_event_time_lhs(event_time, form)} >= {_as_event_time(_ps('current_date', 'day', form, -CATALOG_LOOKBACK_DAYS), form)}"
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
    dialect = form_kind(form)
    return splice_placeholders(transpile(sql, dialect), dialect)


def connection_from_form(form: dict[str, Any]) -> dict[str, Any]:
    kind = form_kind(form)
    if kind == "snowflake":
        out: dict[str, Any] = {
            "account": (form.get("account") or "").strip(),
            "user": (form.get("user") or "").strip(),
            "warehouse": (form.get("warehouse") or "").strip(),
            "database": (form.get("database") or "").strip(),
            "schema": (form.get("schema") or "").strip(),
            "private_key_path": (form.get("private_key_path") or "").strip(),
            "authenticator": (form.get("snowflake_auth") or "key_pair").strip()
            or "key_pair",
        }
        role = (form.get("role") or "").strip()
        if role:
            out["role"] = role
        passphrase = passphrase_from_env()
        if passphrase:
            out["private_key_passphrase"] = passphrase
        return out
    project = (form.get("project") or "").strip()
    location = (form.get("location") or "").strip()
    if not project:
        raise ValueError("project is required")
    if not location:
        raise ValueError("location is required")
    out = {
        "project": project,
        "location": location,
    }
    if CAP_SCAN_CAP in capabilities(kind):
        out["maximum_bytes_billed"] = job_bytes_cap(form)
    credentials = (form.get("credentials") or "").strip()
    if credentials:
        out["credentials"] = credentials
    return out
