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


def filter_ui() -> dict[str, Any]:
    """One payload for the Events form. Do not copy this matrix in JS."""
    return {
        "type_family": dict(FILTER_TYPE_FAMILY),
        "ops": {key: dict(meta) for key, meta in FILTER_OP_META.items()},
        "families": {key: list(ops) for key, ops in FILTER_FAMILY_OPS.items()},
        "exact_string_ops": sorted(EXACT_STRING_OPS),
        "like_ops": sorted(LIKE_OPS),
        "date_parts": [dict(item) for item in FILTER_DATE_PARTS],
        "weekdays": list(WEEKDAYS),
        "months": list(MONTHS),
        "integer_types": sorted(FILTER_INTEGER_TYPES),
        "numeric_op_labels": dict(FILTER_OP_NUMERIC_LABELS),
    }


def date_part(part_id: str) -> dict[str, Any] | None:
    raw = (part_id or "").strip()
    for item in FILTER_DATE_PARTS:
        if item["id"] == raw:
            return item
    if raw:
        return None
    return FILTER_DATE_PARTS[0]
