"""User preferences. Not project mapping. Follows the person, not the repo."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

PREFS_ENV = "FACTCAT_PREFS"

DEFAULTS: dict[str, Any] = {
    "thousand_sep": "comma",
    "decimal_sep": "period",
    "theme": "light",
    "vocab": "plain",
    "weekday_style": "long",
    "month_style": "long",
    "pad_calendar": False,
}

# Once lived on the project file. Copied into the user file, then stripped.
MIGRATED_KEYS = ("thousand_sep", "decimal_sep")

_SEP_CHAR = {
    "comma": ",",
    "period": ".",
    "space": " ",
    "none": "",
}

_CANON_NUMBER = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")


def prefs_path() -> Path:
    raw = os.environ.get(PREFS_ENV, "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".factcat" / "preferences.json"


def _merge(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw and raw[key] is not None:
            data[key] = raw[key]
    return data


def _as_bool(raw: Any) -> bool:
    if raw in (True, "true", "on", "1", 1):
        return True
    if raw in (False, "false", "off", "0", 0, "", None):
        return False
    return bool(raw)


def _read_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update(data)
    thou = str(out.get("thousand_sep") or "comma")
    dec = str(out.get("decimal_sep") or "period")
    if thou not in _SEP_CHAR:
        thou = "comma"
    if dec not in ("period", "comma"):
        dec = "period"
    if _SEP_CHAR[thou] and _SEP_CHAR[thou] == _SEP_CHAR[dec]:
        raise ValueError("thousand and decimal separators must differ")
    out["thousand_sep"] = thou
    out["decimal_sep"] = dec
    if str(out.get("vocab") or "") not in {"plain", "sql"}:
        out["vocab"] = "plain"
    if str(out.get("theme") or "") not in {"light", "dark"}:
        out["theme"] = "light"
    if str(out.get("weekday_style") or "") not in {"long", "short"}:
        out["weekday_style"] = "long"
    if str(out.get("month_style") or "") not in {"long", "short"}:
        out["month_style"] = "long"
    out["pad_calendar"] = _as_bool(out.get("pad_calendar"))
    return {key: out[key] for key in DEFAULTS}


def _migrate_from_project() -> dict[str, Any]:
    from .config import config_path

    raw = _read_object(config_path())
    if raw is None:
        return dict(DEFAULTS)
    data = dict(DEFAULTS)
    for key in MIGRATED_KEYS:
        if key in raw and raw[key] is not None:
            data[key] = raw[key]
    return data


def _strip_project_seps() -> None:
    from .config import config_path

    path = config_path()
    raw = _read_object(path)
    if raw is None:
        return
    changed = False
    for key in MIGRATED_KEYS:
        if key in raw:
            raw.pop(key, None)
            changed = True
    if changed:
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


def load() -> dict[str, Any]:
    path = prefs_path()
    raw = _read_object(path)
    if raw is not None:
        return _validate(_merge(raw))
    from .config import config_path

    blob = _read_object(config_path()) or {}
    had = any(key in blob for key in MIGRATED_KEYS)
    data = _validate(_migrate_from_project())
    if had:
        save(data)
        _strip_project_seps()
    return data


def save(data: dict[str, Any]) -> None:
    path = prefs_path()
    existing = dict(DEFAULTS)
    raw = _read_object(path)
    if raw is not None:
        existing = _validate(_merge(raw))
    merged = _validate({**existing, **data})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")


def thousand_char(data: dict[str, Any] | None = None) -> str:
    raw = str((data or load()).get("thousand_sep") or "comma")
    return _SEP_CHAR.get(raw, ",")


def decimal_char(data: dict[str, Any] | None = None) -> str:
    raw = str((data or load()).get("decimal_sep") or "period")
    return "," if raw == "comma" else "."


def canonical_number(token: str, data: dict[str, Any] | None = None) -> str:
    """User-typed number → warehouse literal (period decimal, no grouping)."""
    prefs = data if data is not None else load()
    raw = (token or "").strip()
    if not raw:
        raise ValueError("filter value must be a number")
    if _CANON_NUMBER.match(raw):
        return raw
    thou = thousand_char(prefs)
    dec = decimal_char(prefs)
    if thou:
        raw = raw.replace(thou, "")
    if dec != ".":
        if raw.count(dec) > 1:
            raise ValueError("filter value must be a number")
        raw = raw.replace(dec, ".")
    if not _CANON_NUMBER.match(raw):
        raise ValueError("filter value must be a number")
    return raw
