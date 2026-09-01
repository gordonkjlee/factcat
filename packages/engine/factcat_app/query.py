"""Turn the form into an EventsSpec. Identifiers only — no free SQL."""

from __future__ import annotations

import json
import re
from typing import Any

from datetime import date, datetime, timedelta, timezone

from factcat import EVENT_MEASURES, PROPERTY_MEASURES, Breakdown, EventsSpec, events_sql
from factcat.spec import BREAKDOWN_AT
from factcat.warehouses import (
    AdapterError,
    CAP_SCAN_CAP,
    QueryResult,
    capabilities,
    is_missing_relation,
)
from factcat.warehouses.bigquery import DEFAULT_MAXIMUM_BYTES_BILLED
from factcat.warehouses.snowflake import passphrase_from_env
from factcat._emit import transpile
from factcat.dialects import (
    as_text,
    create_or_replace_relation,
    json_value_sql,
    splice_placeholders,
)
from factcat_app.config import WAREHOUSE_KINDS
from .filters import (
    EXACT_STRING_OPS,
    FILTER_FAMILY_OPS,
    FILTER_INTEGER_TYPES,
    FILTER_OP_META,
    FILTER_TYPE_FAMILY,
    LIKE_OPS,
    MONTHS,
    WEEKDAYS,
    date_part,
)

_TABLE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_]+)+$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_JSON_KEY = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TIME = re.compile(r"^\d{2}:\d{2}(?::\d{2})?$")
_GRAINS = ("day", "week", "month", "hour", "day_of_week", "hour_of_day")
_CHRONO_GRAINS = frozenset({"day", "week", "month", "hour"})
_CYCLIC_GRAINS = frozenset({"day_of_week", "hour_of_day"})
_RANGE_COUPLED = frozenset({"day", "week", "month"})
_GRAIN_RANK = {"day": 0, "week": 1, "month": 2, "quarter": 3, "year": 4}
_UNITS = frozenset({"day", "week", "month", "quarter", "year", "hour"})
_MODES = frozenset({"last", "this", "previous", "custom"})
_LAST_N = frozenset({"7", "30", "90", "365"})
# Last-N defaults when the saved window unit does not match the chart grain.
_DEFAULT_LAST = {
    "day": 30,
    "week": 8,
    "month": 6,
    "hour": 30,
    "day_of_week": 8,
    "hour_of_day": 14,
}
_RANGE_WINDOW = {
    "hour": "day",
    "day_of_week": "week",
    "hour_of_day": "day",
}
EVENT_VALUE_LIMIT = 1000
# Crash fuse, not a display cap. Slice-and-dice (grain × property)
# is routinely tens of thousands of rows; grouping by a near-unique
# key is when you hit seven figures. No product max: a report can
# double the LIMIT until the result fits or the process/tab dies.
DEFAULT_QUERY_ROW_LIMIT = 1_000_000
# Catalog DISTINCT default window. 0 in the form is all-time. The catalog
# job does not use the report scan cap; lookback is the cost control.
CATALOG_LOOKBACK_DAYS = 90
EVENT_NAME_CACHE_TABLE = "fc_event_names"
EVENT_NAME_CACHE_VERSION = 1


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
    if re.fullmatch(r"\d{4}-\d{2}$", value):
        value = value + "-01"
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
        raise ValueError(
            "range_unit must be day, week, month, quarter, year, or hour"
        )
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
        raise ValueError(
            "grain must be day, week, month, hour, day_of_week, or hour_of_day"
        )
    return raw


def _window_grain(grain: str) -> str:
    """Calendar unit used to bound the query. Cyclic grains use a date filter."""
    return _RANGE_WINDOW.get(grain, grain)


def _include_current(
    form: dict[str, Any], grain: str, unit: str | None = None
) -> bool:
    """Last-N: include the in-progress *window* (today / this month / …).

    Unset defaults follow the effective window unit, not a mismatched form
    unit: day and hour include the current period; week / month / quarter /
    year are complete. Cyclic grains still honour this — it is the filter
    window, not a trailing bar. Not a library period enum.
    """
    raw = form.get("include_current")
    if raw is not None and raw != "":
        return raw in (True, "true", "on", "1", 1)
    if "exclude_current" in form:
        return form.get("exclude_current") not in (True, "true", "on", "1", 1)
    effective = (unit or str(form.get("range_unit") or "")).strip().lower()
    if effective in {"day", "hour"}:
        return True
    if effective in {"week", "month", "quarter", "year"}:
        return False
    return grain in {"day", "hour", "hour_of_day"}


def _exclude_current(form: dict[str, Any], unit: str | None = None) -> bool:
    """Last-N complete periods. Inverse of ``_include_current``."""
    grain = str(form.get("grain") or "day").strip().lower()
    if grain not in _GRAINS:
        grain = "day"
    return not _include_current(form, grain, unit)


def _grain_start(d: date, grain: str, week_start: str) -> date:
    snap = grain if grain in {"day", "week", "month"} else "day"
    if snap == "day":
        return d
    if snap == "month":
        return d.replace(day=1)
    weekday = d.weekday()
    delta = (weekday + 1) % 7 if week_start == "sunday" else weekday
    return d - timedelta(days=delta)


def _grain_next(d: date, grain: str, week_start: str) -> date:
    snap = grain if grain in {"day", "week", "month"} else "day"
    start = _grain_start(d, snap, week_start)
    if snap == "day":
        return start + timedelta(days=1)
    if snap == "week":
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
    """Canonical instant for SELECT ``fc_event_ts`` and extract filters.

    DATETIME stored as UTC CASTs to TIMESTAMP here. Window compares use
    ``_window_time_lhs`` so they do not wrap the column. sqlglot rewrites
    a raw CAST to DATETIME; ``factcat_as_instant`` is spliced after
    transpile.
    """
    kind = _event_time_kind(form)
    if kind == "reporting":
        return event_time
    if kind.startswith("unix_"):
        return f"factcat_as_instant({event_time}, '{kind}')"
    return f"factcat_as_instant({event_time})"


def _window_time_lhs(event_time: str, form: dict[str, Any]) -> str:
    """Time-window filter LHS. Isolate the column so the warehouse can prune.

    Unix epochs are integers, so they still wrap. CAST on DATETIME or
    TIMESTAMP is not required for the compare and defeats BigQuery
    partition pruning and Snowflake micro-partition pruning. Unpartitioned
    tables get the same SQL; pruning is a benefit when the column is the
    partition (or clustering) key, not a requirement.
    """
    kind = _event_time_kind(form)
    if kind.startswith("unix_"):
        return f"factcat_as_instant({event_time}, '{kind}')"
    return event_time


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
    window = _window_grain(grain)
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
        unit = _range_unit(form, default=window)
        n = _range_n(form) if mode == "last" else 1
    if mode == "custom":
        kind = str(form.get("custom_kind") or "absolute").strip().lower()
        if kind == "relative":
            unit = _range_unit(form, default=window)
            if grain in _RANGE_COUPLED:
                unit = grain
            elif unit not in {"day", "week", "month", "quarter", "year"}:
                unit = window if window in _UNITS else "day"
            return mode, unit, 1
        snap = grain if grain in _RANGE_COUPLED else window
        if snap not in {"day", "week", "month"}:
            snap = "day"
        return mode, snap, 1
    if mode == "last":
        if unit == "hour":
            if grain != "hour":
                unit, n = window, _DEFAULT_LAST[grain]
            return mode, unit, n
        if grain in _RANGE_COUPLED and unit != grain:
            n = _DEFAULT_LAST[grain]
            return mode, grain, n
        if grain not in _RANGE_COUPLED and unit not in {"day", "week", "month", "quarter", "year"}:
            unit, n = window, _DEFAULT_LAST[grain]
        return mode, unit, n
    # this / previous: window may be coarser than the chart grain
    # (this week, daily bars). A finer window than the grain is the
    # partial-bucket trap — bump it up. Cyclic / hour windows are dates.
    rank_grain = window if grain not in _RANGE_COUPLED else grain
    if _GRAIN_RANK.get(unit, -1) < _GRAIN_RANK.get(rank_grain, 0):
        unit = rank_grain
    return mode, unit, 1


def _window_recipes(
    form: dict[str, Any],
) -> tuple[tuple[str, Any], tuple[str, Any] | None]:
    """Boundary recipes for the chart window: (start, end-exclusive).

    Each recipe is ``("date", date_sql)``, ``("hours_ago", n)``, or
    ``("hour_start", None)``; the end is ``None`` when the window runs to
    now. One definition — the window WHERE clauses and the breakdown
    anchor expressions both render from these, so they cannot drift.
    """
    mode, unit, n = _normalize_range(form)
    if mode == "custom":
        kind = str(form.get("custom_kind") or "absolute").strip().lower()
        if kind == "relative":
            start_n = _rel_n(form, "rel_start_n", 12)
            end_n = _rel_n(form, "rel_end_n", 0)
            if start_n < end_n:
                raise ValueError("relative from must be at least as far back as to")
            start = ("date", _ps("current_date", unit, form, -start_n))
            if end_n > 0:
                return start, ("date", _ps("current_date", unit, form, -(end_n - 1)))
            return start, None
        start_d = date.fromisoformat(
            _iso_date(str(form.get("start_date") or ""), "start_date")
        )
        end_d = date.fromisoformat(
            _iso_date(str(form.get("end_date") or ""), "end_date")
        )
        if start_d > end_d:
            raise ValueError("start_date must be on or before end_date")
        week_start = _week_start(form)
        start_d = _grain_start(start_d, unit, week_start)
        end_exclusive = _grain_next(end_d, unit, week_start)
        return (
            ("date", "DATE " + _sql_string(start_d.isoformat())),
            ("date", "DATE " + _sql_string(end_exclusive.isoformat())),
        )
    if mode == "this":
        return ("date", _ps("current_date", unit, form, 0)), None
    if mode == "previous":
        return (
            ("date", _ps("current_date", unit, form, -1)),
            ("date", _ps("current_date", unit, form, 0)),
        )
    if unit == "hour":
        end = ("hour_start", None) if _exclude_current(form, unit) else None
        return ("hours_ago", n), end
    if unit == "day":
        end = ("date", _today_sql(form)) if _exclude_current(form, unit) else None
        return ("date", _ps("current_date", "day", form, -n)), end
    if _exclude_current(form, unit):
        return (
            ("date", _ps("current_date", unit, form, -n)),
            ("date", _ps("current_date", unit, form, 0)),
        )
    return ("date", _ps("current_date", unit, form, -(n - 1))), None


def _window_rhs(recipe: tuple[str, Any], form: dict[str, Any]) -> str:
    """A recipe in the event_time column's own type (prune-friendly)."""
    kind, payload = recipe
    if kind == "date":
        return _as_event_time(payload, form)
    tz = _sql_string(_reporting_timezone(form))
    tk = _event_time_kind(form)
    if kind == "hours_ago":
        return f"factcat_hours_ago({payload}, {tz}, '{tk}')"
    return f"factcat_current_hour_start({tz}, '{tk}')"


def _anchor_rhs(recipe: tuple[str, Any], form: dict[str, Any]) -> str:
    """A recipe as an instant comparable with the spec's ``event_time``.

    Breakdown anchor bounds compare against the canonical instant
    (``_event_time_lhs``), not the raw window column, so the boundary is
    rendered in the instant type — ``reporting`` columns stay naive.
    """
    tz = _sql_string(_reporting_timezone(form))
    tk = "reporting" if _event_time_kind(form) == "reporting" else "instant"
    kind, payload = recipe
    if kind == "date":
        return f"factcat_ts_at_date({payload}, {tz}, '{tk}')"
    if kind == "hours_ago":
        return f"factcat_hours_ago({payload}, {tz}, '{tk}')"
    return f"factcat_current_hour_start({tz}, '{tk}')"


def _time_clauses(form: dict[str, Any], event_time: str) -> list[str]:
    """Sugar on event_time. Window unit is the range filter, not always the grain."""
    lhs = _window_time_lhs(event_time, form)
    start, end = _window_recipes(form)
    clauses = [f"{lhs} >= {_window_rhs(start, form)}"]
    if end is not None:
        clauses.append(f"{lhs} < {_window_rhs(end, form)}")
    return clauses


def _now_in_zone(form: dict[str, Any]) -> datetime:
    tz = _reporting_timezone(form)
    if tz == "UTC":
        return datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz))


def _parse_hour_bucket(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("T", " ").replace("Z", "+00:00")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return datetime.strptime(raw[:n], fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def annotate_incomplete(
    rows: list[dict[str, Any]],
    form: dict[str, Any],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Mark the current grain when the window includes it. Trailing only."""
    grain = str(form.get("grain") or "day").strip().lower()
    if grain not in _GRAINS:
        grain = "day"
    if grain in _CYCLIC_GRAINS:
        return [dict(row, incomplete=False) for row in rows]
    now = _now_in_zone(form)
    if today is None:
        today = now.date()
    mode, unit, _n = _normalize_range(form)
    hide_current = mode == "previous" or (
        mode == "last" and not _include_current(form, grain, unit)
    )
    out: list[dict[str, Any]] = []
    if grain == "hour":
        current = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
        for row in rows:
            bucket = _parse_hour_bucket(row.get("bucket"))
            item = dict(row)
            marked = False
            if bucket is not None and not hide_current:
                b = bucket.replace(minute=0, second=0, microsecond=0, tzinfo=None)
                marked = b == current.replace(tzinfo=None) or (
                    b.year,
                    b.month,
                    b.day,
                    b.hour,
                ) == (current.year, current.month, current.day, current.hour)
            item["incomplete"] = marked
            out.append(item)
        return out
    current = _grain_start(today, grain, _week_start(form))
    for row in rows:
        bucket = _parse_bucket(row.get("bucket"))
        item = dict(row)
        item["incomplete"] = bool(
            bucket is not None and bucket == current and not hide_current
        )
        out.append(item)
    return out


def fill_cyclic_buckets(
    rows: list[dict[str, Any]], form: dict[str, Any]
) -> list[dict[str, Any]]:
    """Ensure 7 weekday / 24 hour-of-day keys, missing as 0. Display order."""
    grain = str(form.get("grain") or "day").strip().lower()
    if grain not in _CYCLIC_GRAINS:
        return rows

    def canon(value: Any) -> str:
        raw = str(value or "").strip()
        try:
            return str(int(float(raw)))
        except (TypeError, ValueError):
            return raw

    if grain == "hour_of_day":
        keys = [str(i) for i in range(24)]
    elif _week_start(form) == "sunday":
        keys = [str(i) for i in (6, 0, 1, 2, 3, 4, 5)]
    else:
        keys = [str(i) for i in range(7)]
    skip = frozenset({"bucket", "value", "incomplete"})

    def series_key(row: dict[str, Any]) -> str:
        if "series" in row and row.get("series") not in (None, ""):
            return str(row["series"])
        extras = {k: row[k] for k in row if k not in skip}
        if not extras:
            return ""
        return json.dumps(extras, default=str, sort_keys=True)

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    series_order: list[str] = []
    for row in rows:
        series = series_key(row)
        if series not in grouped:
            grouped[series] = {}
            series_order.append(series)
        grouped[series][canon(row.get("bucket"))] = row
    if not series_order:
        series_order = [""]
        grouped[""] = {}
    out: list[dict[str, Any]] = []
    for series in series_order:
        got = grouped.get(series) or {}
        for key in keys:
            if key in got:
                item = dict(got[key])
                item["bucket"] = key
                item["incomplete"] = False
                out.append(item)
                continue
            template = next(iter(got.values()), {})
            filled = {
                k: v
                for k, v in template.items()
                if k not in {"bucket", "value", "incomplete"}
            }
            filled.update(
                {"bucket": key, "value": 0, "incomplete": False}
            )
            if "series" in template:
                filled["series"] = template["series"]
            out.append(filled)
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


def _breakdown_slot(
    slot: dict[str, Any], dialect: str
) -> tuple[str, str | None] | None:
    """One Break down by slot. Expression wins. Empty is omitted."""
    expr = _single_expr(str(slot.get("breakdown_expr") or slot.get("expr") or ""), "breakdown expression")
    column = str(slot.get("breakdown_column") or slot.get("column") or "").strip()
    if expr:
        return expr, None
    if not column:
        return None
    json_key = str(slot.get("breakdown_json_key") or slot.get("json_key") or "").strip()
    if json_key:
        extracted = _json_value_sql(
            column, json_key, "breakdown", numeric=False, dialect=dialect
        )
        label = _json_label(json_key)
        if not _COLUMN.match(label):
            label = "json_key"
        return extracted, label
    return _ident_column(column, "breakdown"), column


VALUE_ATS = ("event", "range_start", "range_end", "first_record", "latest_record")

# Legacy spec-level breakdown_at, as a per-slot default for payloads that
# predate the Value at control.
_LEGACY_VALUE_AT = {
    "rows": ("event", "null"),
    "carried": ("event", "fill"),
    "first": ("first_record", "null"),
    "last": ("latest_record", "null"),
}


def _slot_fill_from(slot: dict[str, Any], form: dict[str, Any]) -> str | None:
    """Which rows may stamp a value: an event pick, or a SQL predicate."""
    expr = _single_expr(str(slot.get("fill_from_expr") or ""), "fill from")
    if expr:
        return expr
    event = str(slot.get("fill_from_event") or "").strip()
    if not event:
        return None
    event_column = (form.get("event_column") or "").strip()
    if not event_column:
        raise ValueError("Fill from an event requires an event column")
    return _event_names_clause(event_column, [event])


def _slot_breakdown(
    expr: str,
    slot: dict[str, Any],
    form: dict[str, Any],
    anchors: tuple[str, str | None],
) -> str | Breakdown:
    """Map (Value at × If missing × Fill from) onto the library config.

    Chrome the UI hides for a combination (Fill from under each-event +
    leave, If missing on the ever anchors) is ignored, not an error —
    hidden fields keep whatever state they last had. The all-default slot
    returns the plain string so untouched reports build the same spec.
    """
    value_at = str(slot.get("value_at") or "").strip().lower()
    missing = str(slot.get("if_missing") or "").strip().lower()
    if not value_at:
        legacy = str(form.get("breakdown_at") or "rows").strip().lower()
        value_at, legacy_missing = _LEGACY_VALUE_AT.get(legacy, ("event", "null"))
        missing = missing or legacy_missing
    if value_at not in VALUE_ATS:
        raise ValueError(
            "value_at must be event, range_start, range_end, "
            "first_record, or latest_record"
        )
    missing = missing or "null"
    if missing not in ("null", "fill"):
        raise ValueError("if_missing must be null or fill")
    if value_at == "event" and missing == "null":
        # The shipped default. Fill from is hidden chrome here, so it is
        # not even parsed — a stale fill_from_event must never fail a run
        # the code path is about to discard.
        return expr
    fill_from = _slot_fill_from(slot, form)
    if value_at == "event":
        # The app's contract is own-value-first: "Value at: each event"
        # stays literally true even when Fill from names an authoritative
        # source — the narrowed stream fills only rows with no value of
        # their own. (With no fill_from the flag is a no-op; omit it so
        # the plain carried SQL is unchanged.)
        return Breakdown(
            expr,
            at="carried",
            fill_from=fill_from,
            own_value_first=fill_from is not None,
        )
    if value_at == "first_record":
        return Breakdown(expr, at="first", fill_from=fill_from)
    if value_at == "latest_record":
        return Breakdown(expr, at="last", fill_from=fill_from)
    start_anchor, end_anchor = anchors
    if value_at == "range_start":
        # The start boundary is inside the window: inclusive until.
        return Breakdown(
            expr,
            at="last",
            until=start_anchor,
            fill_from=fill_from,
            backfill=missing == "fill",
        )
    if end_anchor is None:
        # The window runs to now: "at range end" is the latest record,
        # and there is no later stamp for If-missing to fill from.
        return Breakdown(expr, at="last", fill_from=fill_from)
    # The end boundary is EXCLUSIVE (the window is `< end`): strict
    # before, so a stamp at exactly that instant stays outside the
    # window it closes and "never reads past the range" stays true.
    return Breakdown(
        expr,
        at="last",
        before=end_anchor,
        fill_from=fill_from,
        backfill=missing == "fill",
    )


def _breakdown_slot_dicts(src: dict[str, Any]) -> list[dict[str, Any]]:
    raw = src.get("breakdowns")
    if isinstance(raw, list) and raw:
        return [item for item in raw if isinstance(item, dict)][:BREAKDOWN_SLOT_CAP]
    return [
        {
            "breakdown_column": src.get("breakdown_column") or "",
            "breakdown_expr": src.get("breakdown_expr") or "",
            "breakdown_json_key": src.get("breakdown_json_key") or "",
        }
    ]


def _breakdown_from_form(
    form: dict[str, Any], *, unit: dict[str, Any] | None = None
) -> tuple[tuple[str | Breakdown, ...], tuple[str, ...] | None]:
    """Fill ``breakdowns`` from slots (column, JSON key, or SQL). Expression wins.

    Each slot carries its own value semantics (Value at, If missing, Fill
    from); range anchors render from the same window recipes as the WHERE
    clauses, converted to the instant type.
    """
    src: dict[str, Any] = form
    if unit is not None and _bool(form, "breakdown_by_series"):
        src = unit
    dialect = form_kind(form)
    entries: list[str | Breakdown] = []
    labels: list[str | None] = []
    anchors: tuple[str, str | None] | None = None
    for slot in _breakdown_slot_dicts(src):
        parsed = _breakdown_slot(slot, dialect)
        if parsed is None:
            continue
        expr, label = parsed
        if anchors is None:
            start, end = _window_recipes(form)
            anchors = (
                _anchor_rhs(start, form),
                _anchor_rhs(end, form) if end is not None else None,
            )
        entries.append(_slot_breakdown(expr, slot, form, anchors))
        labels.append(label)
    if not entries:
        return (), None
    if any(lab is None for lab in labels):
        return tuple(entries), None
    return tuple(entries), tuple(str(lab) for lab in labels)


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


_FILTER_OPS = frozenset(FILTER_OP_META)
_NUMBER = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_INT = re.compile(r"^-?[0-9]+$")
_LIKE_ESCAPE = "#"


def _event_names_from_form(form: dict[str, Any]) -> list[str]:
    raw = form.get("event_values")
    names: list[str] = []
    if isinstance(raw, list):
        names = [str(item).strip() for item in raw if str(item).strip()]
    elif isinstance(raw, str) and raw.strip():
        names = [raw.strip()]
    if not names:
        single = str(form.get("event_value") or "").strip()
        if single:
            names = [single]
    return names


def _event_names_clause(event_column: str, names: list[str]) -> str:
    col = _ident_column(event_column, "event_column")
    if len(names) == 1:
        return f"{col} = {_sql_string(names[0])}"
    return f"{col} IN ({', '.join(_sql_string(name) for name in names)})"


def _event_clause(form: dict[str, Any]) -> str | None:
    event_column = (form.get("event_column") or "").strip()
    names = _event_names_from_form(form)
    if names and not event_column:
        raise ValueError("event name requires an event column")
    if event_column and not names:
        raise ValueError("event name is required")
    if not event_column:
        return None
    return _event_names_clause(event_column, names)


def _split_literals(raw: str) -> list[str]:
    out: list[str] = []
    for chunk in (raw or "").replace("\n", ",").split(","):
        item = chunk.strip()
        if item:
            out.append(item)
    return out


def _sql_literal(token: str, *, numeric: bool) -> str:
    if numeric and _NUMBER.match(token):
        return token
    return _sql_string(token)


def _filter_family(row: dict[str, Any], form: dict[str, Any] | None) -> str:
    if str(row.get("json_key") or "").strip():
        return "string"
    raw = str(row.get("type") or "").strip().upper()
    if raw in FILTER_TYPE_FAMILY:
        return FILTER_TYPE_FAMILY[raw]
    col = str(row.get("column") or "").strip()
    if form and col and col == str(form.get("event_time") or "").strip():
        return "timestamp"
    if raw:
        return "other"
    return "string"


def _is_event_time_col(row: dict[str, Any], form: dict[str, Any] | None) -> bool:
    if not form or str(row.get("json_key") or "").strip():
        return False
    col = str(row.get("column") or "").strip()
    return bool(col) and col == str(form.get("event_time") or "").strip()


def _filter_lhs(
    row: dict[str, Any], form: dict[str, Any] | None
) -> tuple[str, str]:
    column = str(row.get("column") or "").strip()
    json_key = str(row.get("json_key") or "").strip()
    if json_key:
        return (
            _json_value_sql(
                column,
                json_key,
                "filter",
                numeric=False,
                dialect=form_kind(form or {}),
            ),
            "string",
        )
    ident = _ident_column(column, "filter")
    family = _filter_family(row, form)
    if _is_event_time_col(row, form) and form is not None:
        return _event_time_lhs(ident, form), "timestamp"
    return ident, family


def _date_part_meta(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = str(row.get("date_part") or row.get("part") or "").strip()
    if not raw:
        return None
    meta = date_part(raw)
    if meta is None:
        raise ValueError("date part is not supported")
    if not meta.get("id"):
        return None
    return meta


def _as_filter_date(
    lhs: str, row: dict[str, Any], form: dict[str, Any] | None
) -> str:
    if _is_event_time_col(row, form) and form is not None:
        if _event_time_kind(form) == "reporting":
            return f"DATE({lhs})"
        tz = _sql_string(_reporting_timezone(form))
        return f"DATE({lhs}, {tz})"
    typ = str(row.get("type") or "").strip().upper()
    if typ == "DATE":
        return lhs
    if typ in {"DATETIME", "TIMESTAMP"}:
        return f"DATE({lhs})"
    if form is not None:
        return f"DATE({lhs}, {_sql_string(_reporting_timezone(form))})"
    return f"DATE({lhs})"


def _as_filter_datetime(
    lhs: str, row: dict[str, Any], form: dict[str, Any] | None
) -> str:
    if _is_event_time_col(row, form) and form is not None:
        if _event_time_kind(form) == "reporting":
            return lhs
        tz = _sql_string(_reporting_timezone(form))
        return f"DATETIME({lhs}, {tz})"
    typ = str(row.get("type") or "").strip().upper()
    if typ == "DATETIME":
        return lhs
    if typ == "TIMESTAMP":
        return f"DATETIME({lhs})"
    if form is not None:
        return f"DATETIME({lhs}, {_sql_string(_reporting_timezone(form))})"
    return f"DATETIME({lhs})"


def _trunc_sql(date_expr: str, unit: str, form: dict[str, Any] | None) -> str:
    if unit == "day":
        return date_expr
    if unit == "week":
        week = "MONDAY"
        if form is not None and _week_start(form) == "sunday":
            week = "SUNDAY"
        return f"DATE_TRUNC({date_expr}, WEEK({week}))"
    return f"DATE_TRUNC({date_expr}, {unit.upper()})"


def _canon_name(token: str, names: tuple[str, ...], label: str) -> str:
    raw = (token or "").strip()
    if not raw:
        raise ValueError(f"filter value must be a {label}")
    lowered = raw.lower()
    for name in names:
        if name.lower() == lowered:
            return name
    abbrev = [name for name in names if name[:3].lower() == lowered]
    if len(abbrev) == 1:
        return abbrev[0]
    prefix = [name for name in names if name.lower().startswith(lowered)]
    if len(prefix) == 1:
        return prefix[0]
    raise ValueError(f"filter value must be a {label}")


def _enum_compare(lhs: str, tokens: list[str], *, names: tuple[str, ...], label: str, negated: bool) -> str:
    canon = [_sql_string(_canon_name(tok, names, label)) for tok in tokens]
    if len(canon) == 1:
        return f"{lhs} {'<>' if negated else '='} {canon[0]}"
    inner = ", ".join(canon)
    return f"{lhs} {'NOT IN' if negated else 'IN'} ({inner})"


def _apply_date_part(
    lhs: str,
    family: str,
    row: dict[str, Any],
    form: dict[str, Any] | None,
) -> tuple[str, str]:
    if family not in {"date", "timestamp"}:
        return lhs, family
    meta = _date_part_meta(row)
    if not meta:
        return lhs, family
    if meta.get("hour") and family == "date":
        raise ValueError("hour is not a date part")
    trunc = str(meta.get("trunc") or "")
    if trunc == "hour":
        civil = _as_filter_datetime(lhs, row, form)
        return f"DATETIME_TRUNC({civil}, HOUR)", "timestamp"
    date_expr = _as_filter_date(lhs, row, form)
    if trunc:
        return _trunc_sql(date_expr, trunc, form), "date"
    extract = str(meta.get("extract") or "")
    if extract == "HOUR":
        civil = _as_filter_datetime(lhs, row, form)
        return f"EXTRACT(HOUR FROM {civil})", "numeric"
    if extract:
        return f"EXTRACT({extract} FROM {date_expr})", str(meta.get("family") or "numeric")
    if meta["id"] == "day_of_week":
        return f"FORMAT_DATE('%A', {date_expr})", "weekday"
    if meta["id"] == "month_of_year":
        return f"FORMAT_DATE('%B', {date_expr})", "monthname"
    return lhs, family


def _date_rhs(
    iso: str, row: dict[str, Any], form: dict[str, Any] | None
) -> str:
    lit = _date_lit(iso)
    meta = _date_part_meta(row)
    trunc = str(meta.get("trunc") or "") if meta else ""
    if trunc and trunc != "hour":
        return _trunc_sql(lit, trunc, form)
    return lit


def _hour_trunc_lit(iso: str, time_raw: str) -> str:
    clock = _parse_time_value(time_raw) if time_raw else "00:00:00"
    stamp = f"{_iso_date(iso, 'filter')} {clock}"
    return f"DATETIME_TRUNC(DATETIME {_sql_string(stamp)}, HOUR)"


def _part_numeric_lit(row: dict[str, Any], token: str) -> str:
    meta = _date_part_meta(row)
    if meta and meta.get("extract") == "YEAR":
        raw = token.strip()
        if not re.fullmatch(r"\d{4}", raw):
            raise ValueError("year must be four digits")
        return raw
    if meta and meta.get("extract") == "HOUR":
        from .prefs import parse_hour

        try:
            return str(parse_hour(token))
        except ValueError:
            pass
    integer = False
    lo = hi = None
    if meta and meta.get("extract"):
        integer = True
        lo = meta.get("min")
        hi = meta.get("max")
        if lo is not None:
            lo = int(lo)
        if hi is not None:
            hi = int(hi)
    elif str(row.get("type") or "").strip().upper() in FILTER_INTEGER_TYPES:
        integer = True
    return _numeric_lit(token, integer=integer, lo=lo, hi=hi)


def _case_sensitive(row: dict[str, Any], op: str) -> bool:
    raw = row.get("case_sensitive")
    if raw in (True, "true", "on", "1", 1):
        return True
    if raw in (False, "false", "off", "0", 0):
        return False
    return op in EXACT_STRING_OPS


def _like_escape(token: str) -> str:
    return (
        token.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _like_pattern(token: str, kind: str) -> str:
    body = _like_escape(token)
    if kind == "starts_with":
        return body + "%"
    if kind == "ends_with":
        return "%" + body
    return "%" + body + "%"


def _like_clause(lhs: str, tokens: list[str], *, kind: str, sensitive: bool, negated: bool) -> str:
    parts: list[str] = []
    esc = _sql_string(_LIKE_ESCAPE)
    for token in tokens:
        pat = _sql_string(_like_pattern(token, kind))
        if sensitive:
            pred = f"{lhs} LIKE {pat} ESCAPE {esc}"
        else:
            pred = f"LOWER({lhs}) LIKE LOWER({pat}) ESCAPE {esc}"
        parts.append(f"NOT ({pred})" if negated else pred)
    if len(parts) == 1:
        return parts[0]
    glue = " AND " if negated else " OR "
    return "(" + glue.join(parts) + ")"


def _string_eq(lhs: str, token: str, *, sensitive: bool, negated: bool = False) -> str:
    lit = _sql_string(token)
    cmp_op = "<>" if negated else "="
    if sensitive:
        return f"{lhs} {cmp_op} {lit}"
    return f"LOWER({lhs}) {cmp_op} LOWER({lit})"


def _string_in(lhs: str, tokens: list[str], *, sensitive: bool, negated: bool) -> str:
    if sensitive:
        inner = ", ".join(_sql_string(tok) for tok in tokens)
        return f"{lhs} {'NOT IN' if negated else 'IN'} ({inner})"
    inner = ", ".join(f"LOWER({_sql_string(tok)})" for tok in tokens)
    return f"LOWER({lhs}) {'NOT IN' if negated else 'IN'} ({inner})"


def _tokens_from_row(row: dict[str, Any]) -> list[str]:
    raw = row.get("values")
    if isinstance(raw, list):
        tokens = [str(item).strip() for item in raw if str(item).strip()]
        if tokens:
            return tokens
    return _split_literals(str(row.get("value") or ""))


def _require_tokens(row: dict[str, Any]) -> list[str]:
    tokens = _tokens_from_row(row)
    if not tokens:
        raise ValueError("filter value is required")
    return tokens


def _require_one(row: dict[str, Any]) -> str:
    value = str(row.get("value") or "").strip()
    if not value:
        raise ValueError("filter value is required")
    return value


def _require_two(row: dict[str, Any]) -> tuple[str, str]:
    left = str(row.get("value") or "").strip()
    right = str(row.get("value_to") or "").strip()
    if not left or not right:
        raise ValueError("filter value is required")
    return left, right


def _parse_time_value(raw: str, label: str = "filter") -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if not _ISO_TIME.match(value):
        raise ValueError(f"{label} time must be HH:MM or HH:MM:SS")
    if len(value) == 5:
        return value + ":00"
    return value


def _date_lit(iso: str) -> str:
    return "DATE " + _sql_string(_iso_date(iso, "filter"))


def _time_lit(raw: str) -> str:
    return "TIME " + _sql_string(_parse_time_value(raw))


def _plain_stamp(iso_date: str, time_raw: str, typ: str) -> str:
    clock = _parse_time_value(time_raw) if time_raw else "00:00:00"
    stamp = f"{_iso_date(iso_date, 'filter')} {clock}"
    if typ == "DATETIME":
        return "DATETIME " + _sql_string(stamp)
    return "TIMESTAMP " + _sql_string(stamp)


def _next_iso(iso: str) -> str:
    return (date.fromisoformat(_iso_date(iso, "filter")) + timedelta(days=1)).isoformat()


def _event_time_bound(form: dict[str, Any], iso_date: str, time_raw: str) -> str:
    if time_raw:
        stamp = f"{_iso_date(iso_date, 'filter')} {_parse_time_value(time_raw)}"
        if _event_time_kind(form) == "reporting":
            return "DATETIME " + _sql_string(stamp)
        return (
            f"TIMESTAMP({_sql_string(stamp)}, "
            f"{_sql_string(_reporting_timezone(form))})"
        )
    return _as_event_time(_date_lit(iso_date), form)


def _stamp_typ(row: dict[str, Any], form: dict[str, Any] | None) -> str:
    raw = str(row.get("type") or "").strip().upper()
    if raw == "DATETIME":
        return "DATETIME"
    if raw == "TIMESTAMP":
        return "TIMESTAMP"
    if form and _event_time_kind(form) == "reporting":
        return "DATETIME"
    return "TIMESTAMP"


def _ts_bound(
    row: dict[str, Any],
    form: dict[str, Any] | None,
    iso_date: str,
    time_raw: str,
) -> str:
    if _is_event_time_col(row, form) and form is not None:
        return _event_time_bound(form, iso_date, time_raw)
    return _plain_stamp(iso_date, time_raw, _stamp_typ(row, form))


def _canonical_number(token: str) -> str:
    from .prefs import canonical_number

    return canonical_number(token)


def _numeric_lit(
    token: str, *, integer: bool = False, lo: int | None = None, hi: int | None = None
) -> str:
    raw = (token or "").strip()
    if integer:
        if not _INT.match(raw):
            try:
                raw = _canonical_number(raw)
            except ValueError:
                pass
            if raw.endswith(".0") and _INT.match(raw[:-2]):
                raw = raw[:-2]
        if not _INT.match(raw):
            raise ValueError("filter value must be a whole number")
        n = int(raw)
        if lo is not None and n < lo:
            raise ValueError(f"filter value must be at least {lo}")
        if hi is not None and n > hi:
            raise ValueError(f"filter value must be at most {hi}")
        return str(n)
    if not _NUMBER.match(raw):
        try:
            raw = _canonical_number(raw)
        except ValueError:
            pass
    if not _NUMBER.match(raw):
        raise ValueError("filter value must be a number")
    return raw


def _cmp(lhs: str, op: str, lit: str) -> str:
    return {
        "is": f"{lhs} = {lit}",
        "is_not": f"{lhs} <> {lit}",
        "gt": f"{lhs} > {lit}",
        "gte": f"{lhs} >= {lit}",
        "lt": f"{lhs} < {lit}",
        "lte": f"{lhs} <= {lit}",
        "before": f"{lhs} < {lit}",
        "on_or_before": f"{lhs} <= {lit}",
        "after": f"{lhs} > {lit}",
        "on_or_after": f"{lhs} >= {lit}",
    }[op]


def _day_predicate(
    lhs: str,
    op: str,
    lo: str,
    hi: str,
    lo2: str | None = None,
    hi2: str | None = None,
) -> str:
    """Date-only timestamp: [lo, hi) is that day. between uses [lo, hi2)."""
    day = f"{lhs} >= {lo} AND {lhs} < {hi}"
    if op == "is":
        return f"({day})"
    if op == "is_not":
        return f"NOT ({day})"
    if op == "before":
        return f"{lhs} < {lo}"
    if op == "on_or_before":
        return f"{lhs} < {hi}"
    if op == "after":
        return f"{lhs} >= {hi}"
    if op == "on_or_after":
        return f"{lhs} >= {lo}"
    if op == "between" and lo2 is not None and hi2 is not None:
        return f"{lhs} >= {lo} AND {lhs} < {hi2}"
    raise ValueError("filter operator is not supported")


def _filter_clause(
    row: dict[str, Any], form: dict[str, Any] | None = None
) -> str | None:
    expr = _single_expr(str(row.get("expr") or ""), "filter")
    if expr:
        return expr
    column = str(row.get("column") or "").strip()
    if not column:
        return None
    lhs, family = _filter_lhs(row, form)
    lhs, family = _apply_date_part(lhs, family, row, form)
    op = str(row.get("op") or "is").strip().lower()
    if op not in _FILTER_OPS:
        raise ValueError("filter operator is not supported")
    allowed = FILTER_FAMILY_OPS.get(family, FILTER_FAMILY_OPS["other"])
    if op not in allowed:
        raise ValueError("filter operator is not supported")
    kind = FILTER_OP_META[op]["value"]
    if op == "is_null":
        return f"{lhs} IS NULL"
    if op == "is_not_null":
        return f"{lhs} IS NOT NULL"
    if op == "is_true":
        return f"{lhs} IS TRUE"
    if op == "is_false":
        return f"{lhs} IS FALSE"
    if op == "is_empty":
        return f"{lhs} = ''"
    if op == "is_not_empty":
        return f"{lhs} <> ''"
    if kind == "none":
        raise ValueError("filter operator is not supported")
    if family == "string" and op in LIKE_OPS:
        tokens = _require_tokens(row)
        like_kind, negated = LIKE_OPS[op]
        return _like_clause(
            lhs,
            tokens,
            kind=like_kind,
            sensitive=_case_sensitive(row, op),
            negated=negated,
        )
    if family == "string" and op in {"is_any_of", "is_none_of"}:
        tokens = _require_tokens(row)
        return _string_in(
            lhs,
            tokens,
            sensitive=_case_sensitive(row, op),
            negated=op == "is_none_of",
        )
    if family == "string" and op in {"is", "is_not"}:
        token = _require_one(row)
        return _string_eq(
            lhs,
            token,
            sensitive=_case_sensitive(row, op),
            negated=op == "is_not",
        )
    if family == "numeric":
        if op in {"is_any_of", "is_none_of"}:
            tokens = _require_tokens(row)
            inner = ", ".join(_part_numeric_lit(row, tok) for tok in tokens)
            if op == "is_any_of":
                return f"{lhs} IN ({inner})"
            return f"{lhs} NOT IN ({inner})"
        if op == "between":
            lo, hi = _require_two(row)
            return (
                f"{lhs} >= {_part_numeric_lit(row, lo)} AND "
                f"{lhs} <= {_part_numeric_lit(row, hi)}"
            )
        return _cmp(lhs, op, _part_numeric_lit(row, _require_one(row)))
    if family == "weekday":
        tokens = _require_tokens(row) if op in {"is_any_of", "is_none_of"} else [_require_one(row)]
        return _enum_compare(
            lhs,
            tokens,
            names=WEEKDAYS,
            label="weekday",
            negated=op in {"is_not", "is_none_of"},
        )
    if family == "monthname":
        tokens = _require_tokens(row) if op in {"is_any_of", "is_none_of"} else [_require_one(row)]
        return _enum_compare(
            lhs,
            tokens,
            names=MONTHS,
            label="month",
            negated=op in {"is_not", "is_none_of"},
        )
    if family == "date":
        if op == "between":
            lo, hi = _require_two(row)
            return (
                f"{lhs} >= {_date_rhs(lo, row, form)} AND "
                f"{lhs} <= {_date_rhs(hi, row, form)}"
            )
        return _cmp(lhs, op, _date_rhs(_require_one(row), row, form))
    if family == "time":
        if op == "between":
            lo, hi = _require_two(row)
            return f"{lhs} >= {_time_lit(lo)} AND {lhs} <= {_time_lit(hi)}"
        return _cmp(lhs, op, _time_lit(_require_one(row)))
    if family == "timestamp":
        time_raw = _parse_time_value(str(row.get("value_time") or ""))
        time_to = _parse_time_value(str(row.get("value_to_time") or ""))
        meta = _date_part_meta(row)
        if meta and meta.get("trunc") == "hour":
            if op == "between":
                lo, hi = _require_two(row)
                return (
                    f"{lhs} >= {_hour_trunc_lit(lo, time_raw)} AND "
                    f"{lhs} <= {_hour_trunc_lit(hi, time_to)}"
                )
            return _cmp(lhs, op, _hour_trunc_lit(_require_one(row), time_raw))
        if op == "between":
            lo, hi = _require_two(row)
            start = _ts_bound(row, form, lo, time_raw)
            if time_to:
                return f"{lhs} >= {start} AND {lhs} <= {_ts_bound(row, form, hi, time_to)}"
            return f"{lhs} >= {start} AND {lhs} < {_ts_bound(row, form, _next_iso(hi), '')}"
        iso = _require_one(row)
        lo = _ts_bound(row, form, iso, time_raw)
        if time_raw:
            return _cmp(lhs, op, lo)
        hi = _ts_bound(row, form, _next_iso(iso), "")
        return _day_predicate(lhs, op, lo, hi)
    raise ValueError("filter operator is not supported")


def _filters_from_form(form: dict[str, Any]) -> str:
    raw = form.get("filters")
    if not raw:
        return ""
    if not isinstance(raw, list):
        raise ValueError("filters must be a list")
    parts: list[tuple[str, str]] = []
    for row in raw:
        if row is None:
            continue
        if not isinstance(row, dict):
            raise ValueError("filters must be objects")
        clause = _filter_clause(row, form)
        if not clause:
            continue
        join = str(row.get("join") or "AND").strip().upper()
        if join not in {"AND", "OR"}:
            join = "AND"
        if not parts:
            join = "AND"
        parts.append((join, clause))
    if not parts:
        return ""
    rest = {join for join, _ in parts[1:]}
    if not rest or rest == {"AND"}:
        return " AND ".join(clause for _, clause in parts)
    acc = parts[0][1]
    for join, clause in parts[1:]:
        acc = f"({acc} {join} {clause})"
    return acc


SERIES_CAP = 8
BREAKDOWN_SLOT_CAP = 3


def _and_filters(
    rows: list[Any], form: dict[str, Any] | None = None
) -> str:
    clauses: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("filters must be objects")
        clause = _filter_clause(row, form)
        if clause:
            clauses.append(clause)
    return " AND ".join(clauses)


def _card_predicate(
    card: dict[str, Any],
    event_column: str,
    form: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Event + AND-filters for one card. Label is the event name."""
    event = str(card.get("event") or "").strip()
    names = [event] if event else _event_names_from_form(card)
    filters = card.get("filters") if isinstance(card.get("filters"), list) else []
    parts: list[str] = []
    if names and not event_column:
        raise ValueError("event name requires an event column")
    if event_column and not names:
        raise ValueError("event name is required")
    if event_column and names:
        parts.append(_event_names_clause(event_column, names))
    filt = _and_filters(filters, form)
    if filt:
        parts.append(filt)
    label = " or ".join(names) if names else "Events"
    if not parts:
        return "", label
    return " AND ".join(parts), label


def _unit_predicate(
    unit: dict[str, Any],
    event_column: str,
    form: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """One event series: a card, or a combined (Any-of) series."""
    if not isinstance(unit, dict):
        raise ValueError("series must be objects")
    members = unit.get("members") if unit.get("kind") == "any_of" else None
    if members is None and "any_of" in unit:
        members = unit.get("any_of")
    if members is not None:
        if not isinstance(members, list) or not members:
            raise ValueError("Any of needs at least one event")
        if len(members) > SERIES_CAP:
            raise ValueError("at most 8 events in a series")
        bits: list[str] = []
        labels: list[str] = []
        for member in members:
            if not isinstance(member, dict):
                raise ValueError("series must be objects")
            pred, lab = _card_predicate(member, event_column, form)
            if not pred:
                raise ValueError("event name is required")
            bits.append(f"({pred})" if " AND " in pred else pred)
            labels.append(lab)
        if len(bits) == 1:
            return bits[0], labels[0]
        return "(" + " OR ".join(bits) + ")", " or ".join(labels)
    return _card_predicate(unit, event_column, form)


def _series_units(form: dict[str, Any]) -> list[dict[str, Any]]:
    raw = form.get("series")
    if not isinstance(raw, list) or not raw:
        return []
    if len(raw) > SERIES_CAP:
        raise ValueError("at most 8 event series")
    return raw


def _measure_from_form(
    form: dict[str, Any], unit: dict[str, Any] | None = None
) -> tuple[str, str]:
    """Return ``(on, measure)``. ``property_average`` is the form value for property Average."""
    src: dict[str, Any] = unit if isinstance(unit, dict) and unit.get("measure") else form
    raw = str(src.get("measure") or "uniques").strip().lower()
    on_raw = str(src.get("on") or form.get("on") or "").strip().lower()
    if raw == "property_average" or (on_raw == "property" and raw == "average"):
        return "property", "average"
    if raw in PROPERTY_MEASURES and raw != "average":
        return "property", raw
    if raw in EVENT_MEASURES:
        return "events", raw
    raise ValueError("measure must be total, uniques, average, sum, median, or distinct")


def spec_from_form(
    form: dict[str, Any], *, unit: dict[str, Any] | None = None
) -> EventsSpec:
    table = _ident_table(str(form.get("table") or ""), "table")
    entity = (form.get("entity") or "").strip()
    if not entity:
        raise ValueError("entity is required (no default column)")
    entity = _ident_column(entity, "entity")
    event_time_col = _ident_column(str(form.get("event_time") or ""), "event_time")
    grain = _grain(form)
    exact_raw = form.get("exact")
    exact = exact_raw in (True, "true", "on", "1", 1)

    clauses = _time_clauses(form, event_time_col)
    event_column = (form.get("event_column") or "").strip()
    if unit is None:
        units = _series_units(form)
        if len(units) > 1:
            raise ValueError("overlay series need a UNION query")
        if len(units) == 1:
            unit = units[0]
    on, measure = _measure_from_form(form, unit)
    if unit is not None:
        pred, _label = _unit_predicate(unit, event_column, form)
        if pred:
            clauses.append(pred)
    else:
        event_clause = _event_clause(form)
        if event_clause:
            clauses.append(event_clause)
        filter_group = _filters_from_form(form)
        if filter_group:
            clauses.append(filter_group)
    event_time = _event_time_lhs(event_time_col, form)

    of_src = unit if isinstance(unit, dict) and unit.get("measure") else form
    of = _of_from_form(of_src, measure=measure) if on == "property" else None
    if on == "property" and not of:
        raise ValueError("of is required for property measures")

    breakdowns, breakdown_labels = _breakdown_from_form(form, unit=unit)
    # Legacy payloads still send a spec-level breakdown_at; slots without
    # a value_at already folded it in (_slot_breakdown). The spec must NOT
    # receive the legacy value: a plain-string entry means an explicit
    # each-event + leave choice, and forwarding "first" would silently
    # promote it. Validate the incoming field for junk, forward "rows".
    breakdown_at = str(form.get("breakdown_at") or "rows").strip().lower()
    if breakdown_at not in BREAKDOWN_AT:
        raise ValueError("breakdown_at must be rows, first, last, or carried")

    tz = _sql_string(_reporting_timezone(form))
    kind = _event_time_kind(form)
    if grain == "hour":
        bucket = f"factcat_hour_trunc(fc_event_ts, {tz}, '{kind}')"
    elif grain == "hour_of_day":
        bucket = f"factcat_hour_of_day(fc_event_ts, {tz}, '{kind}')"
    elif grain == "day_of_week":
        bucket = f"factcat_dow(fc_event_ts, {tz}, '{kind}')"
    else:
        bucket = f"CAST({_ps('fc_event_ts', grain, form, 0)} AS DATE)"
    return EventsSpec(
        table=table,
        entity=entity,
        event_time=event_time,
        measure=measure,  # type: ignore[arg-type]
        on=on,  # type: ignore[arg-type]
        of=of,
        bucket=bucket,
        where=" AND ".join(clauses),
        exact=exact,
        breakdowns=breakdowns,
        breakdown_at="rows",
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


def _series_arm_sql(
    inner: str,
    spec,
    label: str,
    alias: str,
    dialect: str,
    extra_cols: tuple[str, ...] = (),
) -> str:
    """Wrap one Events aggregation so overlay UNION ALL has bucket, series, value."""
    quoted = _sql_string(label)
    body = _indent_sql(inner.rstrip(), 4)
    if spec.breakdowns:
        parts = [quoted] + [as_text(lab, dialect) for lab in spec.bd_labels()]
        series_expr = "CONCAT(" + ", ' · ', ".join(parts) + ")"
    else:
        series_expr = quoted
    extra = "".join(f", {col}" for col in extra_cols)
    return (
        f"SELECT\n"
        f"  bucket,\n"
        f"  {series_expr} AS series{extra},\n"
        f"  value\n"
        f"FROM (\n{body}\n) AS {alias}"
    )


def events_sql_from_form(form: dict[str, Any]) -> str:
    """Events SQL with a result-row crash fuse (most recent rows)."""
    dialect = form_kind(form)
    units = _series_units(form)
    if len(units) > 1:
        specs = [spec_from_form(form, unit=unit) for unit in units]
        label_sets = [spec.bd_labels() for spec in specs]
        shared = (
            label_sets[0]
            if label_sets and label_sets[0] and all(s == label_sets[0] for s in label_sets)
            else ()
        )
        arms: list[str] = []
        for i, (unit, spec) in enumerate(zip(units, specs)):
            _pred, label = _unit_predicate(
                unit, (form.get("event_column") or "").strip(), form
            )
            inner = events_sql(spec, dialect=dialect)
            arms.append(
                _series_arm_sql(
                    inner, spec, label, f"_fc_arm_{i}", dialect, extra_cols=shared
                )
            )
        sql = "\nUNION ALL\n".join(arms)
    elif len(units) == 1:
        sql = events_sql(spec_from_form(form, unit=units[0]), dialect=dialect)
    else:
        sql = events_sql(spec_from_form(form), dialect=dialect)
    n = query_row_limit(form)
    inner = _indent_sql(sql.rstrip(), 4)
    grain = str(form.get("grain") or "day").strip().lower()
    order = (
        "bucket"
        if grain == "hour" or grain in _CYCLIC_GRAINS
        else "CAST(bucket AS DATE)"
    )
    cap_note = '-- safety cap, not meant to be hit; adjust "Result row limit" in Setup'
    if grain in _CYCLIC_GRAINS:
        return (
            f"SELECT * FROM (\n"
            f"  SELECT * FROM (\n{inner}\n  ) AS _fc_inner\n"
            f"  LIMIT {n} {cap_note}\n"
            f") AS _fc_recent\n"
            f"ORDER BY {order}"
        )
    return (
        f"SELECT * FROM (\n"
        f"  SELECT * FROM (\n{inner}\n  ) AS _fc_inner\n"
        f"  ORDER BY {order} DESC\n"
        f"  LIMIT {n} {cap_note}\n"
        f") AS _fc_recent\n"
        f"ORDER BY {order}"
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


def catalog_lookback_days(form: dict[str, Any]) -> int | None:
    """Days of event_time for catalog DISTINCT. None is all-time."""
    raw = form.get("catalog_lookback_days", CATALOG_LOOKBACK_DAYS)
    if raw in (None, ""):
        return CATALOG_LOOKBACK_DAYS
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("catalog lookback must be a number of days") from exc
    if n < 0:
        raise ValueError("catalog lookback must be >= 0")
    if n == 0:
        return None
    return n


def write_cache_table(form: dict[str, Any]) -> str | None:
    """Qualified ``fc_event_names`` ident, or None when write-back is off.

    Destination is caller-supplied (project + dataset, or database + schema).
    Do not fall back to the billing project or the events table's dataset.
    """
    kind = form_kind(form)
    if kind == "snowflake":
        database = (form.get("write_database") or "").strip()
        schema = (form.get("write_schema") or "").strip()
        if not database or not schema:
            return None
        return _ident_table(
            f"{database}.{schema}.{EVENT_NAME_CACHE_TABLE}", "write_schema"
        )
    project = (form.get("write_project") or "").strip()
    dataset = (form.get("write_dataset") or "").strip()
    if not project or not dataset:
        return None
    if "." in dataset:
        raise ValueError("write dataset must be a dataset id, not project.dataset")
    return _ident_table(
        f"{project}.{dataset}.{EVENT_NAME_CACHE_TABLE}", "write_dataset"
    )


def _emit_relation(ident: str, dialect: str) -> str:
    sql = splice_placeholders(transpile(f"SELECT 1 FROM {ident}", dialect), dialect)
    match = re.search(r"(?i)\bFROM\s+", sql)
    if not match:
        raise RuntimeError("could not emit relation name")
    return sql[match.end() :].strip()


def event_name_cache_rebuild_sql(form: dict[str, Any], *, materialized: bool) -> str:
    """CREATE OR REPLACE the event-name cache (view or table)."""
    dest = write_cache_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    table = _ident_table(str(form.get("table") or ""), "table")
    event_column = _ident_column(str(form.get("event_column") or ""), "event_column")
    dialect = form_kind(form)
    select = splice_placeholders(
        transpile(
            f"SELECT {event_column} AS fc_value FROM {table} "
            f"WHERE {event_column} IS NOT NULL GROUP BY 1",
            dialect,
        ),
        dialect,
    )
    return create_or_replace_relation(
        _emit_relation(dest, dialect),
        select,
        dialect,
        materialized=materialized,
        comment=event_name_cache_comment(form),
    )


def event_name_cache_read_sql(form: dict[str, Any]) -> str:
    dest = write_cache_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    dialect = form_kind(form)
    sql = (
        f"SELECT fc_value FROM {dest} "
        f"WHERE fc_value IS NOT NULL "
        f"ORDER BY 1 "
        f"LIMIT {EVENT_VALUE_LIMIT}"
    )
    return splice_placeholders(transpile(sql, dialect), dialect)


def event_name_cache_fingerprint(form: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": EVENT_NAME_CACHE_VERSION,
        "table": str(form.get("table") or "").strip(),
        "event_column": str(form.get("event_column") or "").strip(),
    }


def event_name_cache_comment(form: dict[str, Any]) -> str:
    return json.dumps(
        event_name_cache_fingerprint(form), separators=(",", ":"), sort_keys=True
    )


def stored_event_name_cache(form: dict[str, Any]) -> dict[str, Any]:
    raw = form.get("event_name_cache")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def event_name_cache_matches(
    stored: dict[str, Any], wanted: dict[str, Any]
) -> bool:
    if not stored:
        return False
    try:
        version = int(stored.get("v") or 0)
    except (TypeError, ValueError):
        version = 0
    return (
        version == int(wanted["v"])
        and str(stored.get("table") or "") == wanted["table"]
        and str(stored.get("event_column") or "") == wanted["event_column"]
    )


def catalog_event_values(
    form: dict[str, Any], run: Any
) -> tuple[QueryResult, str | None, dict[str, Any] | None]:
    """Load catalog names: dest cache, or lookback DISTINCT.

    ``run(sql)`` is ``Adapter.run``. Returns ``(result, cache_kind, meta)``.
    ``cache_kind`` is ``cached`` / ``materialized_view`` / ``table``, or
    None when the lookback DISTINCT path ran. ``meta`` is the fingerprint
    plus ``kind`` to persist, or None when dest is unused.
    """
    dest = write_cache_table(form)
    if dest is None:
        return run(event_values_sql(form)), None, None

    wanted = event_name_cache_fingerprint(form)
    stored = stored_event_name_cache(form)
    match = event_name_cache_matches(stored, wanted)
    kind = str(stored.get("kind") or "")
    rebuild = form.get("rebuild") in (True, "true", "on", "1", 1)

    def create_then_read(*, prefer_table: bool) -> tuple[QueryResult, str]:
        order: tuple[tuple[bool, str], ...]
        if prefer_table:
            order = ((False, "table"),)
        else:
            order = ((True, "materialized_view"), (False, "table"))
        last: AdapterError | None = None
        for materialized, name in order:
            try:
                run(
                    event_name_cache_rebuild_sql(form, materialized=materialized)
                )
                return run(event_name_cache_read_sql(form)), name
            except AdapterError as exc:
                last = exc
        raise last or AdapterError("could not create event-name cache")

    def lookback() -> tuple[QueryResult, str | None, dict[str, Any] | None]:
        return run(event_values_sql(form)), None, None

    if rebuild and kind == "table":
        try:
            result, name = create_then_read(prefer_table=True)
        except AdapterError:
            return lookback()
        return result, name, {**wanted, "kind": name}

    selected: QueryResult | None = None
    try:
        selected = run(event_name_cache_read_sql(form))
    except AdapterError as exc:
        if not is_missing_relation(exc):
            raise

    if selected is not None and match:
        meta = {**wanted, "kind": kind or "materialized_view"}
        return selected, "cached", meta

    try:
        result, name = create_then_read(prefer_table=False)
    except AdapterError:
        return lookback()
    return result, name, {**wanted, "kind": name}


def event_values_sql(form: dict[str, Any]) -> str:
    """DISTINCT event names. Catalog mode filters event_time so a
    partitioned table can prune. Lookback comes from Setup (0 = all-time).
    """
    table = _ident_table(str(form.get("table") or ""), "table")
    event_column = _ident_column(str(form.get("event_column") or ""), "event_column")
    event_time = _ident_column(str(form.get("event_time") or ""), "event_time")
    catalog = form.get("catalog") in (True, "true", "on", "1", 1)
    parts = [f"{event_column} IS NOT NULL"]
    if catalog:
        days = catalog_lookback_days(form)
        if days is not None:
            parts.append(
                f"{_window_time_lhs(event_time, form)} >= "
                f"{_as_event_time(_ps('current_date', 'day', form, -days), form)}"
            )
    else:
        parts.extend(_time_clauses(form, event_time))
    where = " AND ".join(parts)
    sql = (
        f"SELECT DISTINCT {event_column} AS fc_value "
        f"FROM {table} "
        f"WHERE {where} "
        f"ORDER BY 1 "
        f"LIMIT {EVENT_VALUE_LIMIT}"
    )
    dialect = form_kind(form)
    return splice_placeholders(transpile(sql, dialect), dialect)


def connection_from_form(
    form: dict[str, Any], *, apply_scan_cap: bool = True
) -> dict[str, Any]:
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
    if apply_scan_cap and CAP_SCAN_CAP in capabilities(kind):
        out["maximum_bytes_billed"] = job_bytes_cap(form)
    credentials = (form.get("credentials") or "").strip()
    if credentials:
        out["credentials"] = credentials
    return out
