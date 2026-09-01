"""User preferences. Not project mapping. Follows the person, not the repo."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

PREFS_ENV = "FACTCAT_PREFS"

# One complete format. 12-hour has no leading zeros; 03:00 is the padded 24-hour clock.
HOUR_STYLES = (
    "3pm",
    "3 pm",
    "3 p.m.",
    "3PM",
    "3 PM",
    "3 P.M.",
    "3",
    "3h",
    "3:00",
    "03:00",
)
HOUR_STYLE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("12", ("3pm", "3 pm", "3 p.m.", "3PM", "3 PM", "3 P.M.")),
    ("24", ("3", "3h", "3:00", "03:00")),
)
HOUR_PREVIEW = (0, 3, 12, 15)
_HOUR_STYLE_ALIASES = {
    "3am": "3 pm",
    "03am": "3 pm",
    "3:00am": "3 pm",
    "03:00am": "3 pm",
    "3:00pm": "3pm",
    "3:00 pm": "3 pm",
    "3:00 p.m.": "3 p.m.",
    "3:00PM": "3PM",
    "3:00 PM": "3 PM",
    "3:00 P.M.": "3 P.M.",
    "03": "3",
    "03h": "3h",
}
_TWELVE = {
    "3pm": ("", "am", "pm"),
    "3 pm": (" ", "am", "pm"),
    "3 p.m.": (" ", "a.m.", "p.m."),
    "3PM": ("", "AM", "PM"),
    "3 PM": (" ", "AM", "PM"),
    "3 P.M.": (" ", "A.M.", "P.M."),
}
HOUR_CLOCK_DEFAULT = {"12": "3pm", "24": "3"}

DEFAULTS: dict[str, Any] = {
    "thousand_sep": "comma",
    "decimal_sep": "period",
    "theme": "light",
    "vocab": "plain",
    "sql_case": "upper",
    "sql_neq": "<>",
    "weekday_style": "long",
    "month_style": "long",
    "pad_day": False,
    "hour_style": "3",
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


def _canonical_hour_style(raw: str) -> str:
    style = _HOUR_STYLE_ALIASES.get(str(raw or "3"), str(raw or "3"))
    if style in HOUR_STYLES:
        return style
    return "3"


def _combine_hour_style(shape: str, clock: str) -> str:
    shape = _canonical_hour_style(shape)
    if str(clock) == "12":
        return "3 pm"
    return shape


def _apply_legacy_hour(raw: dict[str, Any], dest: dict[str, Any]) -> None:
    if "hour_style" in raw:
        return
    if "hour_clock" in raw or "hour_format" in raw:
        dest["hour_style"] = _combine_hour_style(
            str(raw.get("hour_format") or dest.get("hour_style") or "3"),
            str(raw.get("hour_clock") or "24"),
        )


def _apply_pad_calendar(raw: dict[str, Any], dest: dict[str, Any]) -> None:
    if "pad_calendar" not in raw:
        return
    if "pad_day" not in raw:
        dest["pad_day"] = _as_bool(raw.get("pad_calendar"))
    if "hour_style" not in raw and "hour_format" not in raw:
        dest["hour_style"] = (
            "03:00" if _as_bool(dest.get("pad_day", raw.get("pad_calendar"))) else "3"
        )


def _merge(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw and raw[key] is not None:
            data[key] = raw[key]
    _apply_pad_calendar(raw, data)
    _apply_legacy_hour(raw, data)
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
    incoming = dict(data)
    _apply_pad_calendar(incoming, incoming)
    _apply_legacy_hour(incoming, incoming)
    out = dict(DEFAULTS)
    out.update(incoming)
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
    if str(out.get("sql_case") or "") not in {"upper", "lower"}:
        out["sql_case"] = "upper"
    if str(out.get("sql_neq") or "") not in {"<>", "!="}:
        out["sql_neq"] = "<>"
    if str(out.get("theme") or "") not in {"light", "dark"}:
        out["theme"] = "light"
    if str(out.get("weekday_style") or "") not in {"long", "short"}:
        out["weekday_style"] = "long"
    if str(out.get("month_style") or "") not in {"long", "short"}:
        out["month_style"] = "long"
    out["pad_day"] = _as_bool(out.get("pad_day"))
    style = str(out.get("hour_style") or "3")
    if style not in HOUR_STYLES:
        if "hour_clock" in incoming or "hour_format" in incoming:
            style = _combine_hour_style(
                str(incoming.get("hour_format") or style),
                str(incoming.get("hour_clock") or "24"),
            )
        else:
            style = _canonical_hour_style(style)
    out["hour_style"] = style if style in HOUR_STYLES else "3"
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
    payload = dict(data)
    _apply_pad_calendar(payload, payload)
    _apply_legacy_hour(payload, payload)
    merged = _validate({**existing, **payload})
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


_HOUR_TOKEN = re.compile(
    r"^\s*(\d{1,2})(?::00)?h?\s*([AaPp]\.?[Mm]\.?)?\s*$",
)


def format_hour(
    hour: int,
    style: str | None = None,
    clock: str | None = None,
    data: dict[str, Any] | None = None,
) -> str:
    """0–23 → display. One complete style, never shape × clock."""
    prefs = data if data is not None else {}
    sty = style or str(prefs.get("hour_style") or "")
    if clock is not None:
        sty = _combine_hour_style(sty or str(prefs.get("hour_format") or "3"), clock)
    sty = _canonical_hour_style(sty or str(prefs.get("hour_style") or "3"))
    h = int(hour) % 24
    if sty == "3":
        return str(h)
    if sty == "3h":
        return f"{h}h"
    if sty == "3:00":
        return f"{h}:00"
    if sty == "03:00":
        return f"{h:02d}:00"
    joiner, am, pm = _TWELVE[sty]
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}{joiner}{pm if h >= 12 else am}"


def parse_hour(token: str) -> int:
    """Display or integer → 0–23."""
    raw = (token or "").strip()
    if not raw:
        raise ValueError("filter value must be an hour")
    match = _HOUR_TOKEN.match(raw)
    if not match:
        raise ValueError("filter value must be an hour")
    n = int(match.group(1))
    period = (match.group(2) or "").lower().replace(".", "")
    if period:
        if n < 1 or n > 12:
            raise ValueError("filter value must be an hour")
        if n == 12:
            n = 0 if period == "am" else 12
        elif period == "pm":
            n += 12
    elif n > 23:
        raise ValueError("filter value must be an hour")
    return n


def hour_labels(data: dict[str, Any] | None = None) -> list[str]:
    prefs = data if data is not None else load()
    return [format_hour(h, data=prefs) for h in range(24)]


def hour_style_previews() -> dict[str, list[str]]:
    return {sty: [format_hour(h, sty) for h in HOUR_PREVIEW] for sty in HOUR_STYLES}


def hour_clock_of_style(style: str) -> str:
    sty = _canonical_hour_style(style)
    for clock, styles in HOUR_STYLE_GROUPS:
        if sty in styles:
            return clock
    return "24"
