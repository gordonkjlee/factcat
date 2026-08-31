"""Connection and mapping persist on disk, not in Docker."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from factcat.warehouses import ADAPTERS

CONFIG_ENV = "FACTCAT_CONFIG"

DEFAULTS: dict[str, Any] = {
    "kind": "bigquery",
    "project": "",
    "data_project": "",
    "location": "",
    "credentials": "",
    "account": "",
    "user": "",
    "warehouse": "",
    "database": "",
    "schema": "",
    "role": "",
    "private_key_path": "",
    "snowflake_auth": "key_pair",
    "dataset": "",
    "table_name": "",
    "table": "",
    "entity": "",
    "entity_label": "User",
    "entity_label_plural": "Users",
    "event_time": "",
    "event_column": "",
    "event_value": "",
    "event_values": [],
    "filters": [],
    "series": [],
    "measure": "uniques",
    "on": "events",
    "of_column": "",
    "of_expr": "",
    "of_json_key": "",
    "grain": "day",
    "exact": False,
    "lookback_days": 30,
    "range_preset": "30",
    "range_mode": "last",
    "range_n": 30,
    "range_unit": "day",
    "exclude_current": False,
    "include_current": False,
    "week_start": "monday",
    "reporting_timezone": "UTC",
    "event_time_tz": "utc",
    "event_time_epoch": "",
    "start_date": "",
    "end_date": "",
    "custom_kind": "absolute",
    "rel_start_n": 12,
    "rel_end_n": 0,
    "event_names": [],
    "columns": [],
    "chart_type": "auto",
    "chart_labels": False,
    "chart_value_format": "auto",
    "chart_axis_x": True,
    "chart_axis_y": True,
    "chart_grid": "major",
    "chart_title": "",
    "chart_title_locked": False,
    "bytes_cap_gb": 10,
    "query_row_limit": 1_000_000,
    "breakdown_by_series": False,
    "breakdown_column": "",
    "breakdown_expr": "",
    "breakdown_json_key": "",
    "breakdown_at": "rows",
    "top_n": 8,
    "include_other": True,
}

WAREHOUSE_KINDS = tuple(ADAPTERS)


def warehouse_kind(data: dict[str, Any] | None = None) -> str:
    raw = str((data or {}).get("kind") or "bigquery").strip().lower() or "bigquery"
    if raw not in ADAPTERS:
        raise ValueError("kind must be " + ", ".join(ADAPTERS))
    return raw


_ENTITY_PLURALS = {
    "User": "Users",
    "Customer": "Customers",
    "Account": "Accounts",
    "Subscription": "Subscriptions",
}


def entity_plural(singular: str) -> str:
    raw = (singular or "").strip() or "User"
    for key, value in _ENTITY_PLURALS.items():
        if key.lower() == raw.lower():
            return value
    if raw.endswith("s"):
        return raw
    return raw + "s"


def config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV, ".factcat.json")
    return Path(raw)


def _merge(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw and raw[key] is not None:
            data[key] = raw[key]
    return data


def load() -> dict[str, Any]:
    path = config_path()
    data = dict(DEFAULTS)
    loaded: dict[str, Any] = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config must be a JSON object")
        data = _merge(loaded)
    if "entity_label_plural" not in loaded:
        data["entity_label_plural"] = entity_plural(str(data.get("entity_label") or "User"))
    return data


def mapping_ready(cfg: dict[str, Any] | None = None) -> bool:
    data = cfg if cfg is not None else load()
    kind = warehouse_kind(data)
    shared = ("table", "entity", "event_time")
    if kind == "snowflake":
        keys = (
            "account",
            "user",
            "warehouse",
            "database",
            "schema",
        ) + shared
        if str(data.get("snowflake_auth") or "key_pair").strip() != "externalbrowser":
            keys = keys + ("private_key_path",)
    else:
        keys = ("project", "location") + shared
    return all(str(data.get(key) or "").strip() for key in keys)


def save(data: dict[str, Any]) -> None:
    merged = load()
    for key in DEFAULTS:
        if key in data and data[key] is not None:
            merged[key] = data[key]
    for key in ("thousand_sep", "decimal_sep"):
        merged.pop(key, None)
    path = config_path()
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
