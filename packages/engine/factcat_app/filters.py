"""Filter operator matrix. Compile lives in query.py so SQL helpers stay one place."""

from __future__ import annotations

from typing import Any

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
        "family": "numeric",
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
    "contains": "LIKE",
    "not_contains": "NOT LIKE",
    "starts_with": "LIKE prefix",
    "not_starts_with": "NOT LIKE prefix",
    "ends_with": "LIKE suffix",
    "not_ends_with": "NOT LIKE suffix",
    "is_empty": "= ''",
    "is_not_empty": "<> ''",
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
        "breakdown": "Break down by",
        "breakdown_each": "Break down each series",
        "breakdown_th": "Break down",
        "sql_expr": "SQL expression",
        "sql_expr_ellipsis": "SQL expression…",
        "of": "Of",
        "volume": "Volume",
        "unique": "Unique {plural}",
        "average_per": "Average per {singular}",
        "filter_column": "Filter column",
        "filter_op": "Filter operator",
        "filter_sql": "Filter SQL",
    },
    "sql": {
        "add_filter": "WHERE",
        "combine": "OR",
        "breakdown": "GROUP BY",
        "breakdown_each": "GROUP BY each series",
        "breakdown_th": "GROUP BY",
        "sql_expr": "SQL",
        "sql_expr_ellipsis": "SQL…",
        "of": "of=",
        "volume": "COUNT",
        "unique": "COUNT DISTINCT",
        "average_per": "COUNT / COUNT DISTINCT",
        "filter_column": "column",
        "filter_op": "op",
        "filter_sql": "SQL",
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


def filter_ui(prefs: dict[str, Any] | None = None) -> dict[str, Any]:
    """One payload for the Events form. Do not copy this matrix in JS."""
    if prefs is None:
        from .prefs import load as load_prefs

        prefs = load_prefs()
    vocab = str(prefs.get("vocab") or "plain")
    if vocab not in CHROME:
        vocab = "plain"
    ops = {key: dict(meta) for key, meta in FILTER_OP_META.items()}
    numeric_labels = dict(FILTER_OP_NUMERIC_LABELS)
    if vocab == "sql":
        for key, label in FILTER_OP_SQL_LABELS.items():
            if key in ops:
                ops[key]["label"] = label
        numeric_labels = {
            key: FILTER_OP_SQL_LABELS.get(key, label)
            for key, label in FILTER_OP_NUMERIC_LABELS.items()
        }
    pad = bool(prefs.get("pad_calendar"))
    parts = []
    for item in FILTER_DATE_PARTS:
        row = dict(item)
        if row.get("id") == "hour_of_day":
            row["label"] = "Hour of day (00-23)" if pad else "Hour of day (0-23)"
        if row.get("id") == "day_of_month":
            row["label"] = "Day of month (01-31)" if pad else "Day of month (1-31)"
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
        "integer_types": sorted(FILTER_INTEGER_TYPES),
        "numeric_op_labels": numeric_labels,
        "chrome": dict(CHROME[vocab]),
        "vocab": vocab,
        "thousand_sep": str(prefs.get("thousand_sep") or "comma"),
        "decimal_sep": str(prefs.get("decimal_sep") or "period"),
        "pad_calendar": pad,
    }


def date_part(part_id: str) -> dict[str, Any] | None:
    raw = (part_id or "").strip()
    for item in FILTER_DATE_PARTS:
        if item["id"] == raw:
            return item
    if raw:
        return None
    return FILTER_DATE_PARTS[0]
