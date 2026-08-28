"""Connection and mapping persist on disk, not in Docker."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_ENV = "FACTCAT_CONFIG"

DEFAULTS: dict[str, Any] = {
    "project": "",
    "location": "",
    "credentials": "",
    "table": "",
    "entity": "",
    "event_time": "",
    "event_column": "",
    "event_value": "",
    "measure": "uniques",
    "grain": "day",
    "exact": False,
    "lookback_days": 30,
}


def config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV, ".factcat.json")
    return Path(raw)


def load() -> dict[str, Any]:
    path = config_path()
    data = dict(DEFAULTS)
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config must be a JSON object")
        data.update(loaded)
    return data


def save(data: dict[str, Any]) -> None:
    merged = dict(DEFAULTS)
    merged.update(data)
    path = config_path()
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
