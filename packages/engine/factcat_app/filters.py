"""Filter operator matrix. Compile lives in query.py so SQL helpers stay one place."""

from __future__ import annotations

import re
from typing import Any

from .sql_display import sql_chrome, sql_plain

# value: none (no input), one, two (between), list (comma/newline tokens).
FILTER_OP_META: dict[str, dict[str, str]] = {
    "is": {"label": "is", "value": "one"},
    "is_not": {"label": "is not", "value": "one"},
    "is_any_of": {"label": "is any of", "value": "list"},
    "is_none_of": {"label": "is none of", "value": "list"},
    "contains": {"label": "contains", "value": "list"},
    "not_contains": {"label": "does not contain", "value": "list"},
    "starts_with": {"label": "starts with", "value": "list"},
    "not_starts_with": {"label": "does not start with", "value": "list"},
    "ends_with": {"label": "ends with", "value": "list"},
    "not_ends_with": {"label": "does not end with", "value": "list"},
    "is_empty": {"label": "is empty", "value": "none"},
    "is_not_empty": {"label": "is not empty", "value": "none"},
    "is_true": {"label": "is true", "value": "none"},
    "is_false": {"label": "is false", "value": "none"},
    "gt": {"label": "greater than", "value": "one"},
    "gte": {"label": "at least", "value": "one"},
    "lt": {"label": "less than", "value": "one"},
    "lte": {"label": "at most", "value": "one"},
    "before": {"label": "before", "value": "one"},
    "on_or_before": {"label": "on or before", "value": "one"},
    "after": {"label": "after", "value": "one"},
    "on_or_after": {"label": "on or after", "value": "one"},
    "between": {"label": "between (inclusive)", "value": "two"},
    "is_null": {"label": "is null", "value": "none"},
    "is_not_null": {"label": "is not null", "value": "none"},
}

FILTER_FAMILY_OPS: dict[str, tuple[str, ...]] = {
    "string": (
        "is",
        "is_not",
        "is_any_of",
        "is_none_of",
        "contains",
        "not_contains",
        "starts_with",
        "not_starts_with",
        "ends_with",
        "not_ends_with",
        "is_empty",
        "is_not_empty",
        "is_null",
        "is_not_null",
    ),
    "boolean": ("is_true", "is_false", "is_null", "is_not_null"),
    "numeric": (
        "is",
        "is_not",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "is_any_of",
        "is_none_of",
        "is_null",
        "is_not_null",
    ),
    "date": (
        "is",
        "is_not",
        "before",
        "on_or_before",
        "after",
        "on_or_after",
        "between",
        "is_null",
        "is_not_null",
    ),
    "time": (
        "is",
        "is_not",
        "before",
        "on_or_before",
        "after",
        "on_or_after",
        "between",
        "is_null",
        "is_not_null",
    ),
    "timestamp": (
        "is",
        "is_not",
        "before",
        "on_or_before",
        "after",
        "on_or_after",
        "between",
        "is_null",
        "is_not_null",
    ),
    "weekday": (
        "is",
        "is_not",
        "is_any_of",
        "is_none_of",
        "is_null",
        "is_not_null",
    ),
    "hourname": (
        "is",
        "is_not",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "is_any_of",
        "is_none_of",
        "is_null",
        "is_not_null",
    ),
    "monthname": (
        "is",
        "is_not",
        "is_any_of",
        "is_none_of",
        "is_null",
        "is_not_null",
    ),
    "other": ("is_null", "is_not_null"),
}

# Trunc (DATE_TRUNC) stays a date. Extract (EXTRACT / FORMAT_DATE) retargets ops.
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
FILTER_DATE_PARTS: tuple[dict[str, Any], ...] = (
    {"id": "", "label": "The date", "group": "", "family": None},
    {
        "id": "hour",
        "label": "Hour (e.g. 18 May 2026, 14:00)",
        "group": "Start of",
        "family": "timestamp",
        "trunc": "hour",
        "hour": True,
        "picker": "hour",
    },
    {
        "id": "day",
        "label": "Day (e.g. 18 May 2026)",
        "group": "Start of",
        "family": "date",
        "trunc": "day",
        "picker": "day",
    },
    {
        "id": "week",
        "label": "Week (e.g. 2026 week 21)",
        "group": "Start of",
        "family": "date",
        "trunc": "week",
        "picker": "week",
    },
    {
        "id": "month",
        "label": "Month (e.g. May 2026)",
        "group": "Start of",
        "family": "date",
        "trunc": "month",
        "picker": "month",
    },
    {
        "id": "quarter",
        "label": "Quarter (e.g. Q2 2026)",
        "group": "Start of",
        "family": "date",
        "trunc": "quarter",
        "picker": "quarter",
    },
    {
        "id": "hour_of_day",
        "label": "Hour of day (0-23)",
        "group": "Extract",
        "family": "hourname",
        "extract": "HOUR",
        "hour": True,
        "min": 0,
        "max": 23,
    },
    {
        "id": "day_of_week",
        "label": "Day of week (e.g. Monday)",
        "group": "Extract",
        "family": "weekday",
    },
    {
        "id": "day_of_month",
        "label": "Day of month (1-31)",
        "group": "Extract",
        "family": "numeric",
        "extract": "DAY",
        "min": 1,
        "max": 31,
    },
    {
        "id": "day_of_year",
        "label": "Day of year (1-366)",
        "group": "Extract",
        "family": "numeric",
        "extract": "DAYOFYEAR",
        "min": 1,
        "max": 366,
    },
    {
        "id": "week_of_year",
        "label": "Week of year (1-53)",
        "group": "Extract",
        "family": "numeric",
        "extract": "ISOWEEK",
        "min": 1,
        "max": 53,
    },
    {
        "id": "month_of_year",
        "label": "Month of year (e.g. May)",
        "group": "Extract",
        "family": "monthname",
    },
    {
        "id": "year",
        "label": "Year (e.g. 2026)",
        "group": "Extract",
        "family": "numeric",
        "extract": "YEAR",
        "min": 1000,
        "max": 9999,
    },
)

# Whole numbers only. NUMERIC / FLOAT still allow a decimal.
FILTER_INTEGER_TYPES = frozenset({"INT64", "INTEGER", "INT", "BIGINT"})
FILTER_OP_NUMERIC_LABELS = {
    "is": "is (=)",
    "is_not": "is not (\u2260)",
    "gt": "greater than (>)",
    "gte": "at least (\u2265)",
    "lt": "less than (<)",
    "lte": "at most (\u2264)",
}

FILTER_OP_SQL_LABELS = {
    "is": "=",
    "is_not": "<>",
    "is_any_of": "IN",
    "is_none_of": "NOT IN",
    "contains": "LIKE '%s%'",
    "not_contains": "NOT LIKE '%s%'",
    "starts_with": "LIKE 's%'",
    "not_starts_with": "NOT LIKE 's%'",
    "ends_with": "LIKE '%s'",
    "not_ends_with": "NOT LIKE '%s'",
    "is_empty": "= (empty)",
    "is_not_empty": "<> (empty)",
    "is_true": "IS TRUE",
    "is_false": "IS FALSE",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "before": "<",
    "on_or_before": "<=",
    "after": ">",
    "on_or_after": ">=",
    "between": "BETWEEN",
    "is_null": "IS NULL",
    "is_not_null": "IS NOT NULL",
}

CHROME = {
    "plain": {
        "add_filter": "Add filter",
        "combine": "Combine",
        "any_of": "Any of",
        "event_or": "or",
        "add_group_event": "Add event",
        "breakdown": "Break down by",
        "breakdown_each": "Break down each series",
        "breakdown_th": "Break down",
        "add_breakdown": "Add breakdown",
        "sql_expr": "SQL expression",
        "sql_expr_ellipsis": "SQL expression…",
        "bd_value_at": "Value at",
        "bd_at_event": "each event",
        "bd_at_range_start": "range start",
        "bd_at_range_end": "range end",
        "bd_at_first": "first record",
        "bd_at_latest": "latest record",
        "bd_if_missing": "If missing",
        "bd_missing_null": "leave (null)",
        "bd_missing_fill": "fill from history",
        "bd_fill_from": "Fill from",
        "bd_fill_any": "Any event",
        "of": "Of",
        "volume": "Volume",
        "unique": "Unique {plural}",
        "average_per": "Average per {singular}",
        "property_sum": "Sum",
        "property_average": "Average",
        "property_median": "Median",
        "property_distinct": "Distinct per {singular}",
        "filter_column": "Filter column",
        "filter_op": "Filter operator",
        "filter_sql": "Filter SQL",
    },
    "sql": {
        "add_filter": "`WHERE`",
        "combine": "`OR`",
        "any_of": "`OR`",
        "event_or": "`OR`",
        "add_group_event": "Add event",
        "breakdown": "`GROUP BY`",
        "breakdown_each": "`GROUP BY` each series",
        "breakdown_th": "`GROUP BY`",
        "add_breakdown": "`GROUP BY`",
        "sql_expr": "`SQL`",
        "sql_expr_ellipsis": "`SQL`…",
        "bd_value_at": "value at",
        "bd_at_event": "each row",
        "bd_at_range_start": "range start",
        "bd_at_range_end": "range end",
        "bd_at_first": "first non-null",
        "bd_at_latest": "last non-null",
        "bd_if_missing": "if `NULL`",
        "bd_missing_null": "keep `NULL`",
        "bd_missing_fill": "fill from history",
        "bd_fill_from": "fill from",
        "bd_fill_any": "any row",
        "of": "of",
        "volume": "`COUNT(*)`",
        "unique": "`COUNT(DISTINCT id)`",
        "average_per": "`COUNT(*)`/`COUNT(DISTINCT id)`",
        "property_sum": "`SUM(x)`",
        "property_average": "`AVERAGE(x)`",
        "property_median": "`MEDIAN(x)`",
        "property_distinct": "`AVG(COUNT(DISTINCT x))`",
        "filter_column": "column",
        "filter_op": "op",
        "filter_sql": "`SQL`",
    },
}

FILTER_TYPE_FAMILY: dict[str, str] = {
    "STRING": "string",
    "BOOL": "boolean",
    "BOOLEAN": "boolean",
    "INT64": "numeric",
    "INTEGER": "numeric",
    "NUMERIC": "numeric",
    "BIGNUMERIC": "numeric",
    "FLOAT64": "numeric",
    "FLOAT": "numeric",
    "DATE": "date",
    "TIME": "time",
    "TIMESTAMP": "timestamp",
    "DATETIME": "timestamp",
    "JSON": "string",
}

DATE_PART_GROUP_SQL = {
    "Start of": "DATE_TRUNC",
    "Extract": "EXTRACT",
}

LIKE_OPS: dict[str, tuple[str, bool]] = {
    "contains": ("contains", False),
    "not_contains": ("contains", True),
    "starts_with": ("starts_with", False),
    "not_starts_with": ("starts_with", True),
    "ends_with": ("ends_with", False),
    "not_ends_with": ("ends_with", True),
}

EXACT_STRING_OPS = frozenset({"is", "is_not", "is_any_of", "is_none_of"})


def _short_names(names: tuple[str, ...]) -> list[str]:
    return [name[:3] for name in names]


def display_weekdays(style: str) -> list[str]:
    return _short_names(WEEKDAYS) if style == "short" else list(WEEKDAYS)


def display_months(style: str) -> list[str]:
    return _short_names(MONTHS) if style == "short" else list(MONTHS)


_SQL_MARK = re.compile(r"`([^`]*)`")


def _sql_letter_case(text: str, case: str) -> str:
    if case != "lower":
        return text
    if "`" not in text:
        return text
    return _SQL_MARK.sub(lambda m: "`" + m.group(1).lower() + "`", text)


def _sql_neq(text: str, neq: str) -> str:
    return text.replace("<>", "!=") if neq == "!=" else text


def _sql_label(text: str, case: str, neq: str) -> str:
    text = _sql_neq(text, neq)
    return text.lower() if case == "lower" else text


def filter_ui(prefs: dict[str, Any] | None = None) -> dict[str, Any]:
    """One payload for the Events form. Do not copy this matrix in JS."""
    if prefs is None:
        from .prefs import load as load_prefs

        prefs = load_prefs()
    vocab = str(prefs.get("vocab") or "plain")
    if vocab not in CHROME:
        vocab = "plain"
    sql_case = str(prefs.get("sql_case") or "upper")
    if sql_case not in {"upper", "lower"}:
        sql_case = "upper"
    sql_neq = str(prefs.get("sql_neq") or "<>")
    if sql_neq not in {"<>", "!="}:
        sql_neq = "<>"
    ops = {key: dict(meta) for key, meta in FILTER_OP_META.items()}
    numeric_labels = dict(FILTER_OP_NUMERIC_LABELS)
    chrome = dict(CHROME[vocab])
    if vocab == "sql":
        for key, label in FILTER_OP_SQL_LABELS.items():
            if key in ops:
                ops[key]["label"] = _sql_label(label, sql_case, sql_neq)
        numeric_labels = {
            key: _sql_label(FILTER_OP_SQL_LABELS.get(key, label), sql_case, sql_neq)
            for key, label in FILTER_OP_NUMERIC_LABELS.items()
        }
        chrome = {key: _sql_letter_case(value, sql_case) for key, value in chrome.items()}
    from .prefs import _validate, format_hour, hour_labels

    prefs = _validate(prefs)

    pad_day = bool(prefs.get("pad_day"))
    hours = hour_labels(prefs)
    hod_lo = format_hour(0, data=prefs)
    hod_hi = format_hour(23, data=prefs)
    parts = []
    for item in FILTER_DATE_PARTS:
        row = dict(item)
        if row.get("id") == "hour_of_day":
            row["label"] = f"Hour of day ({hod_lo}–{hod_hi})"
        if row.get("id") == "day_of_month":
            row["label"] = (
                "Day of month (01-31)" if pad_day else "Day of month (1-31)"
            )
        if vocab == "sql" and row.get("group") in DATE_PART_GROUP_SQL:
            group = DATE_PART_GROUP_SQL[row["group"]]
            row["group"] = group.lower() if sql_case == "lower" else group
        parts.append(row)
    return {
        "type_family": dict(FILTER_TYPE_FAMILY),
        "ops": ops,
        "families": {key: list(ops_ids) for key, ops_ids in FILTER_FAMILY_OPS.items()},
        "exact_string_ops": sorted(EXACT_STRING_OPS),
        "like_ops": sorted(LIKE_OPS),
        "date_parts": parts,
        "weekdays": display_weekdays(str(prefs.get("weekday_style") or "long")),
        "months": display_months(str(prefs.get("month_style") or "long")),
        "hours": hours,
        "integer_types": sorted(FILTER_INTEGER_TYPES),
        "numeric_op_labels": numeric_labels,
        "chrome": chrome,
        "chrome_html": {key: str(sql_chrome(value)) for key, value in chrome.items()},
        "chrome_plain": {key: sql_plain(value) for key, value in chrome.items()},
        "vocab": vocab,
        "sql_case": sql_case,
        "sql_neq": sql_neq,
        "thousand_sep": str(prefs.get("thousand_sep") or "comma"),
        "decimal_sep": str(prefs.get("decimal_sep") or "period"),
        "pad_day": pad_day,
        "hour_style": str(prefs.get("hour_style") or "3"),
        "weekday_style": str(prefs.get("weekday_style") or "long"),
    }


def date_part(part_id: str) -> dict[str, Any] | None:
    raw = (part_id or "").strip()
    for item in FILTER_DATE_PARTS:
        if item["id"] == raw:
            return item
    if raw:
        return None
    return FILTER_DATE_PARTS[0]
