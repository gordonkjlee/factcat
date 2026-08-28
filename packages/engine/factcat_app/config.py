"""Connection and mapping persist on disk, not in Docker."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_ENV = "FACTCAT_CONFIG"

DEFAULTS: dict[str, Any] = {
    "project": "",
    "data_project": "",
    "location": "",
    "credentials": "",
    "dataset": "",
    "table_name": "",
    "table": "",
    "entity": "",
    "entity_label": "User",
    "event_time": "",
    "event_column": "",
    "event_value": "",
    "measure": "uniques",
    "grain": "day",
    "exact": False,
    "lookback_days": 30,
    "range_preset": "30",
    "range_mode": "last",
    "range_n": 30,
    "range_unit": "day",
    "exclude_current": False,
    "week_start": "monday",
    "start_date": "",
    "end_date": "",
    "event_names": [],
}


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
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config must be a JSON object")
        data = _merge(loaded)
    return data


def mapping_ready(cfg: dict[str, Any] | None = None) -> bool:
    data = cfg if cfg is not None else load()
    return all(
        str(data.get(key) or "").strip()
        for key in ("project", "location", "table", "entity", "event_time")
    )


def save(data: dict[str, Any]) -> None:
    merged = load()
    for key in DEFAULTS:
        if key in data and data[key] is not None:
            merged[key] = data[key]
    path = config_path()
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
