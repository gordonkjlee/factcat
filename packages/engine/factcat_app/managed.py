"""Factcat-managed tables: the column index and its bookkeeping.

``fc_column_index`` holds, for a handful of breakdown columns, every row
where that column had a value — entity, instant, value, event name — so
the expensive Value-at modes (carried, range anchors, first / latest ever)
read a small relation plus a short live tail instead of the events table's
full history. The library side is ``Breakdown.values_table``; this module
decides when to build, refresh, attach, or drop, and emits the SQL.

Rules that shaped it (spec: item 12):

* The registry lives in the index table's own description (the shipped
  fingerprint-as-comment pattern); ``.factcat.json`` keeps a status
  mirror. Warehouse-side is the authority when the two disagree.
* Build on Run, sequentially: the first run of a qualifying column builds
  its index first, then queries through it. Never on an estimate — an
  estimate is a free dry run and must not bill a probe or write a table.
* Correctness never depends on freshness: every query reads the index
  plus the live rows after its bookmark, so a stale index is a cost, not
  a wrong answer. Refresh folds the tail in when it is older than the
  staleness target.
* Removal is automatic and lazy: a daily sweep drops columns unused for
  the TTL. Adding costs a scan, so it waits for a real need; dropping
  costs nothing, so it needs nobody.
* Bounds on the events table compare the STORED time column through the
  existing date placeholder, never a function around it: partitions prune
  and the day of overlap only yields duplicate values, which the LOCF
  stream and the anchor ranking are proven indifferent to.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from factcat.dialects import (
    create_table_as,
    set_relation_comment,
    splice_placeholders,
)
from factcat._emit import transpile
from factcat.warehouses import AdapterError, BytesCapError, QueryResult, is_missing_relation

from factcat_app.query import (
    _as_event_time,
    _breakdown_slot,
    _breakdown_slot_dicts,
    _event_names_clause,
    _event_time_kind,
    _emit_relation,
    _event_time_lhs,
    _ident_column,
    _series_units,
    _slot_fill_names,
    _slot_semantics,
    _sql_string,
    _window_time_lhs,
    event_name_cache_census_sql,
    form_kind,
    qualified_table,
    stored_event_name_cache,
    write_relation,
)

INDEX_TABLE = "fc_column_index"
REGISTRY_VERSION = 1

# Not knobs (design record: knobs need destinations an admin can reason
# about). Density above this is "the value is already on the rows" —
# Value at: each event reads it for free, an index would be table-sized.
DENSITY_MAX = 0.25
PROBE_DAYS = 30
PROBE_TTL_DAYS = 7
SWEEP_HOURS = 24
USAGE_BUMP_MINUTES = 60
# BigQuery caps a table description at 16 KB; the census snapshot is the
# only part that can grow, so it is the part that gets trimmed.
COMMENT_BUDGET = 12_000

MODES = ("auto", "off")
DEFAULTS = {
    "managed_mode": "auto",
    "managed_drop_days": 60,
    "managed_refresh_days": 7,
    "managed_lookback_days": 3,
}

RunFn = Callable[..., QueryResult]


# ---------------------------------------------------------------- settings


def _int_setting(form: dict[str, Any], key: str, default: int, *, low: int) -> int:
    raw = form.get(key)
    if raw in (None, ""):
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a whole number of days") from exc
    if n < low:
        raise ValueError(f"{key} must be at least {low}")
    return n


def settings(form: dict[str, Any]) -> dict[str, Any]:
    mode = str(form.get("managed_mode") or DEFAULTS["managed_mode"]).strip().lower()
    if mode not in MODES:
        raise ValueError("managed_mode must be auto or off")
    return {
        "mode": mode,
        "drop_days": _int_setting(form, "managed_drop_days", 60, low=1),
        "refresh_days": _int_setting(form, "managed_refresh_days", 7, low=0),
        "lookback_days": _int_setting(form, "managed_lookback_days", 3, low=0),
    }


def index_table(form: dict[str, Any]) -> str | None:
    """Qualified ``fc_column_index`` ident, or None when write-back is off."""
    return write_relation(form, INDEX_TABLE)


# ---------------------------------------------------------------- registry


def config_fingerprint(form: dict[str, Any]) -> dict[str, Any]:
    """What a rebuild depends on. A change here rebuilds every column."""
    return {
        "v": REGISTRY_VERSION,
        "table": str(form.get("table") or "").strip(),
        "entity": str(form.get("entity") or "").strip(),
        "event_time": str(form.get("event_time") or "").strip(),
        "time_kind": _event_time_kind(form),
        "event_column": str(form.get("event_column") or "").strip(),
        # A new write destination is a new (empty) table: the mirror's
        # bookmarks must not attach to it.
        "dest": index_table(form) or "",
    }


def column_key(expr: str, label: str) -> str:
    """The ``fc_column`` key: the column name, or a hash for an expression."""
    text = (label or "").strip()
    if text and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) and text == expr.strip():
        return text
    digest = hashlib.sha1(expr.strip().encode("utf-8")).hexdigest()[:10]
    return f"expr_{digest}"


def registry_from_form(form: dict[str, Any]) -> dict[str, Any]:
    """The status mirror carried in the form / config (never authoritative)."""
    raw = form.get("managed_tables")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    # Never hand back the caller's object: the planner mutates the registry
    # (probe cache, bookmarks) and a shared default dict would leak across
    # configs — a probe from one run appeared in the next process's plan.
    return copy.deepcopy(raw)


def parse_registry(comment: str | None) -> dict[str, Any]:
    """Registry JSON from a table description; {} when absent or foreign."""
    text = (comment or "").strip()
    if not text.startswith("{"):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict) or "columns" not in data:
        return {}
    return data


def registry_comment(registry: dict[str, Any]) -> str:
    """JSON for the table description, trimmed to the description budget
    by dropping census snapshots first (they are a cache of a cache)."""
    body = dict(registry)
    text = json.dumps(body, separators=(",", ":"), sort_keys=True)
    if len(text) <= COMMENT_BUDGET:
        return text
    cols = {k: dict(v) for k, v in (body.get("columns") or {}).items()}
    for col in cols.values():
        col.pop("names", None)
    body["columns"] = cols
    # Say so: without the snapshot the census diff cannot repair, and a
    # silent drop would look like a healthy registry.
    body["names_dropped"] = True
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()




def _parse_iso(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------- slots


@dataclass
class IndexColumn:
    key: str
    expr: str
    label: str
    fill: str  # "any" | "names" | "expr"
    names: list[str] | None


def expensive_columns(form: dict[str, Any]) -> list[IndexColumn]:
    """Breakdown slots whose Value-at mode reads history (anything but
    each event + keep NULL), de-duplicated by column key."""
    dialect = form_kind(form)
    out: dict[str, IndexColumn] = {}
    per_series = bool(form.get("breakdown_by_series") in (True, "true", "on", "1", 1))
    sources: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    if per_series:
        for unit in _series_units(form):
            sources.append((unit, unit))
    else:
        sources.append((form, None))
    for src, unit in sources:
        for slot in _breakdown_slot_dicts(src):
            parsed = _breakdown_slot(slot, dialect)
            if parsed is None:
                continue
            expr, label = parsed
            value_at, missing = _slot_semantics(slot, form)
            if value_at == "event" and missing == "null":
                continue
            fill, names = _slot_fill_names(slot, form, unit if per_series else None)
            key = column_key(expr, label)
            if key not in out:
                out[key] = IndexColumn(key, expr, label, fill, names)
    return list(out.values())


# ---------------------------------------------------------------- SQL


def _emit(sql: str, dialect: str) -> str:
    return splice_placeholders(transpile(sql, dialect), dialect)


def _mapping(form: dict[str, Any]) -> tuple[str, str, str, str, str | None]:
    """(table, entity, event_time_expr, raw_time_col, event_column|None)."""
    table = qualified_table(form)
    entity = _ident_column(str(form.get("entity") or ""), "entity")
    raw = _ident_column(str(form.get("event_time") or ""), "event_time")
    event_time = _event_time_lhs(raw, form)
    event_column = (form.get("event_column") or "").strip()
    event_col = _ident_column(event_column, "event_column") if event_column else None
    return table, entity, event_time, _window_time_lhs(raw, form), event_col


def _select_values(
    form: dict[str, Any], key: str, expr: str, where: list[str]
) -> str:
    table, entity, event_time, _raw, event_col = _mapping(form)
    name_sel = event_col if event_col else "CAST(NULL AS VARCHAR)"
    conds = [
        f"({expr}) IS NOT NULL",
        f"({entity}) IS NOT NULL",
        f"({event_time}) IS NOT NULL",
        *where,
    ]
    return (
        f"SELECT {_sql_string(key)} AS fc_column, {entity} AS fc_entity, "
        f"{event_time} AS fc_at, {expr} AS fc_value, {name_sel} AS fc_event_name "
        f"FROM {table} WHERE " + "\n  AND ".join(conds)
    )


def _time_column_is_date(form: dict[str, Any]) -> bool:
    """Mapping knowledge, not SQL: is the stored time column a DATE?"""
    if _event_time_kind(form) != "reporting":
        return False
    for col in form.get("columns") or []:
        if isinstance(col, dict) and str(col.get("name") or "") == str(
            form.get("event_time") or ""
        ):
            return str(col.get("type") or "").upper() == "DATE"
    return False


def ensure_table_sql(form: dict[str, Any], registry: dict[str, Any]) -> str:
    """CREATE TABLE IF NOT EXISTS with the contract columns, empty."""
    dest = index_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    dialect = form_kind(form)
    table, entity, event_time, _raw, event_col = _mapping(form)
    name_sel = event_col if event_col else "CAST(NULL AS VARCHAR)"
    select = _emit(
        f"SELECT CAST('' AS VARCHAR) AS fc_column, {entity} AS fc_entity, "
        f"{event_time} AS fc_at, CAST(NULL AS VARCHAR) AS fc_value, "
        f"{name_sel} AS fc_event_name FROM {table} WHERE 1 = 0",
        dialect,
    )
    return create_table_as(
        _emit_relation(dest, dialect),
        select,
        dialect,
        partition_day="fc_at",
        partition_is_date=_time_column_is_date(form),
        cluster=("fc_column", "fc_entity"),
        comment=registry_comment(registry),
    )


def backfill_select_sql(
    form: dict[str, Any], key: str, expr: str, *, names: list[str] | None = None
) -> str:
    """The SELECT a backfill inserts: every recorded value of ``expr``
    (optionally only from ``names``). Priced by the estimate on its own —
    the INSERT form needs a table that may not exist yet."""
    where: list[str] = []
    if names:
        _t, _e, _ts, _raw, event_col = _mapping(form)
        if event_col is None:
            raise ValueError("event names need an event column")
        where.append(_event_names_clause(event_col, names))
    return _emit(_select_values(form, key, expr, where), form_kind(form))


def backfill_sql(
    form: dict[str, Any], key: str, expr: str, *, names: list[str] | None = None
) -> str:
    """INSERT every recorded value of ``expr`` (optionally only from ``names``)."""
    dest = index_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    where: list[str] = []
    if names:
        _t, _e, _ts, _raw, event_col = _mapping(form)
        if event_col is None:
            raise ValueError("event names need an event column")
        where.append(_event_names_clause(event_col, names))
    select = _select_values(form, key, expr, where)
    dialect = form_kind(form)
    return _emit(f"INSERT INTO {dest} {select}", dialect)


def bookmarks_sql(form: dict[str, Any], key: str) -> str:
    dest = index_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    return _emit(
        f"SELECT fc_event_name, MAX(fc_at) AS fc_bookmark, COUNT(*) AS fc_rows "
        f"FROM {dest} WHERE fc_column = {_sql_string(key)} GROUP BY 1",
        form_kind(form),
    )


def _date_bound(form: dict[str, Any], day: date) -> str:
    return _as_event_time(_sql_string(day.isoformat()), form)


def refresh_sql(
    form: dict[str, Any],
    key: str,
    expr: str,
    *,
    bookmarks: dict[str | None, date],
    lookback_days: int,
    names: list[str] | None = None,
) -> str:
    """Rows newer than each event name's bookmark (less the lookback),
    anti-joined against the index so a re-read day never doubles up.

    Bounds compare the stored time column through the date placeholder so
    partitions prune; a day of overlap costs a few duplicate values, which
    the engine is proven indifferent to.
    """
    dest = index_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    table, entity, event_time, raw, event_col = _mapping(form)
    if not bookmarks:
        raise ValueError("refresh needs at least one bookmark")
    back = timedelta(days=max(0, lookback_days))
    floors = {name: (day - back) for name, day in bookmarks.items()}
    floor_all = min(floors.values())
    conds = [f"({raw}) >= {_date_bound(form, floor_all)}"]
    if event_col is not None and any(name is not None for name in floors):
        parts = []
        known = [name for name in floors if name is not None]
        for name in known:
            parts.append(
                f"({event_col} = {_sql_string(name)} AND ({raw}) >= "
                f"{_date_bound(form, floors[name])})"
            )
        # Rows with no event name have no name bookmark; they ride the
        # NULL bookmark when there is one, else the overall floor. Without
        # this branch they would never fold in after the first build.
        null_floor = floors.get(None, floor_all)
        parts.append(
            f"({event_col} IS NULL AND ({raw}) >= {_date_bound(form, null_floor)})"
        )
        # Names the index has never seen are NOT folded in here: they are
        # backfilled whole (backfill_sql with names=...), because a new
        # name's history sits behind every bookmark.
        conds.append("(" + " OR ".join(parts) + ")")
    if names:
        if event_col is None:
            raise ValueError("event names need an event column")
        conds.append(_event_names_clause(event_col, names))
    name_match = (
        f"AND (i.fc_event_name = {event_col} OR (i.fc_event_name IS NULL AND {event_col} IS NULL))"
        if event_col is not None
        else ""
    )
    anti = (
        f"NOT EXISTS (SELECT 1 FROM {dest} i WHERE i.fc_column = {_sql_string(key)} "
        f"AND i.fc_entity = {entity} AND i.fc_at = {event_time} "
        f"AND i.fc_value = ({expr}) {name_match})"
    )
    select = _select_values(form, key, expr, conds + [anti])
    return _emit(f"INSERT INTO {dest} {select}", form_kind(form))


def delete_column_sql(
    form: dict[str, Any], key: str, *, names: list[str] | None = None
) -> str:
    dest = index_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    where = f"fc_column = {_sql_string(key)}"
    if names:
        where += " AND " + _event_names_clause("fc_event_name", names)
    return _emit(f"DELETE FROM {dest} WHERE {where}", form_kind(form))


def drop_table_sql(form: dict[str, Any]) -> str:
    dest = index_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    return _emit(f"DROP TABLE IF EXISTS {dest}", form_kind(form))


def registry_comment_sql(form: dict[str, Any], registry: dict[str, Any]) -> str:
    dest = index_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    dialect = form_kind(form)
    return set_relation_comment(
        _emit_relation(dest, dialect), registry_comment(registry), dialect
    )


def density_probe_sql(form: dict[str, Any], expr: str, *, today: date) -> str:
    """How often ``expr`` is set on recent rows. Recent partitions only."""
    table, _entity, _ts, raw, _ec = _mapping(form)
    since = today - timedelta(days=PROBE_DAYS)
    return _emit(
        f"SELECT COUNT(*) AS fc_rows, COUNT({expr}) AS fc_present "
        f"FROM {table} WHERE ({raw}) >= {_date_bound(form, since)}",
        form_kind(form),
    )


def values_relation_sql(
    form: dict[str, Any], key: str, names: list[str] | None
) -> str:
    """The ``Breakdown.values_table`` relation for one column: the caller
    narrowing (fill from these events) is baked in here, never applied by
    the engine."""
    dest = index_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    where = f"fc_column = {_sql_string(key)}"
    if names:
        where += " AND " + _event_names_clause("fc_event_name", names)
    return (
        f"(SELECT fc_entity, fc_at AS fc_t, fc_value FROM {dest} WHERE {where}) "
        f"AS fc_idx_{re.sub(r'[^A-Za-z0-9_]', '_', key)}"
    )


def watermark_sql(form: dict[str, Any], bookmark: datetime) -> str:
    """The day the bookmark falls on, in the stored column's type: the
    live tail re-reads from that midnight. Overlap is duplicates only."""
    return _date_bound(form, bookmark.date())


# ---------------------------------------------------------------- plan


@dataclass
class ColumnPlan:
    column: IndexColumn
    action: str  # attach | build | refresh | rebuild | live
    reason: str = ""
    bookmark: datetime | None = None
    statements: list[str] = field(default_factory=list)
    names_to_backfill: list[str] = field(default_factory=list)


@dataclass
class Plan:
    columns: list[ColumnPlan]
    dest: str | None
    event_time_column: str | None
    registry: dict[str, Any]
    settings: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    # The exceptions behind `failures`, in order: a scan-cap rejection keeps
    # its figures so Setup can show them (a string would flatten it).
    errors: list[BaseException] = field(default_factory=list)
    built: list[str] = field(default_factory=list)

    def builds(self) -> list[ColumnPlan]:
        return [c for c in self.columns if c.action in ("build", "rebuild", "refresh")]

    def maybe(self) -> list[ColumnPlan]:
        """Eligible but unprobed: the Run will check density and may build."""
        return [c for c in self.columns if c.action == "live" and c.reason == NOT_CHECKED]

    def attachable(self) -> dict[str, ColumnPlan]:
        return {c.column.key: c for c in self.columns if c.action != "live"}

    def attachment(
        self, form: dict[str, Any], expr: str, label: str, fill: str, names: list[str] | None
    ) -> tuple[str, str | None] | None:
        """``(values_table, values_watermark)`` for a slot, or None."""
        if fill == "expr":
            return None
        col = self.attachable().get(column_key(expr, label))
        if col is None or col.action == "live":
            return None
        if col.bookmark is None:
            return None
        return (
            values_relation_sql(form, col.column.key, names),
            watermark_sql(form, col.bookmark),
        )


def _mode_open(form: dict[str, Any], plan_settings: dict[str, Any]) -> bool:
    return plan_settings["mode"] == "auto"


def _write_ok(form: dict[str, Any]) -> bool:
    status = str(form.get("write_access_status") or "").strip().lower()
    return status != "denied"


def _mapped_type(form: dict[str, Any], expr: str) -> str:
    """The mapped type of a bare column expression, upper-cased; "" for an
    expression or an unmapped name. Mapping knowledge, not SQL."""
    name = expr.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return ""
    for col in form.get("columns") or []:
        if isinstance(col, dict) and str(col.get("name") or "").lower() == name.lower():
            return str(col.get("type") or "").upper()
    return ""


def is_text_column(form: dict[str, Any], expr: str) -> bool:
    """False only for a bare column whose mapped type is known and not text:
    ``fc_value`` is text, and an INT64 or DATE column would fail the INSERT
    on every Run. Expressions and unknown types pass (the caller casts)."""
    kind = _mapped_type(form, expr)
    return not (kind and kind.startswith(_TEXTLESS))


def text_columns(form: dict[str, Any]) -> list[dict[str, Any]]:
    """The mapped columns Setup may offer to Index a column now: the text
    ones (the same gate the planner applies), in mapping order."""
    out = []
    for col in form.get("columns") or []:
        if isinstance(col, dict) and col.get("name") and is_text_column(form, str(col["name"])):
            out.append({"name": str(col["name"]), "type": str(col.get("type") or "")})
    return out


def _probe_density(
    form: dict[str, Any], run: RunFn, expr: str, probes: dict[str, Any], key: str, now: datetime
) -> float | None:
    cached = probes.get(key) if isinstance(probes, dict) else None
    if isinstance(cached, dict):
        at = _parse_iso(cached.get("at"))
        if at is not None and now - at < timedelta(days=PROBE_TTL_DAYS):
            value = cached.get("density")
            return float(value) if value is not None else None
    res = run(density_probe_sql(form, expr, today=now.date()))
    rows = res.rows[0] if res.rows else {}
    total = rows.get("fc_rows")
    present = rows.get("fc_present")
    density = None
    if total:
        density = float(present or 0) / float(total)
    probes[key] = {"density": density, "at": now_iso(now)}
    return density


NOT_CHECKED = "not yet checked"
NON_TEXT = "not a text column (index CAST(... AS STRING) as an expression instead)"
_TEXTLESS = (
    "INT", "BIGINT", "SMALLINT", "TINYINT", "NUMBER", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL",
    "BOOL", "DATE", "TIME", "TIMESTAMP", "DATETIME", "ARRAY", "STRUCT", "JSON", "VARIANT", "OBJECT",
    "GEOGRAPHY", "BYTES", "BINARY",
)
NO_PREVIEW = (
    "automatic preparation needs a cost preview, which this warehouse cannot "
    "give; use Index a column now on Setup"
)


def build_plan(
    form: dict[str, Any],
    run: RunFn | None,
    *,
    now: datetime | None = None,
    allow_probe: bool = False,
    auto_ok: bool = True,
) -> Plan:
    """Decide per expensive column: attach the index, build it first,
    refresh it first, rebuild it, or run live.

    ``run`` may be None (estimate path): only cached facts are used and
    nothing is billed. ``allow_probe`` lets the run path measure density
    for a column the mirror has not seen. ``auto_ok`` is whether this
    warehouse can price a job before running it (dry run): without a
    preview there is no consent moment, so nothing builds automatically —
    an admin's explicit Index-a-column-now still can.
    """
    now = now or datetime.now(timezone.utc)
    cfg = settings(form)
    dest = index_table(form)
    registry = registry_from_form(form)
    columns = expensive_columns(form)
    plan = Plan(
        columns=[], dest=dest, event_time_column=None, registry=registry, settings=cfg
    )
    if not columns:
        return plan
    if dest is None:
        plan.columns = [ColumnPlan(c, "live", "no write destination") for c in columns]
        return plan
    fp = config_fingerprint(form)
    reg_fp = registry.get("fp") if isinstance(registry.get("fp"), dict) else None
    reg_cols = registry.get("columns") if isinstance(registry.get("columns"), dict) else {}
    probes = registry.setdefault("probes", {}) if isinstance(registry, dict) else {}
    stale_config = bool(reg_cols) and reg_fp != fp
    _t, _e, _ts, raw, _ec = _mapping(form)
    plan.event_time_column = raw
    for col in columns:
        entry = reg_cols.get(col.key) if not stale_config else None
        if col.fill == "expr":
            plan.columns.append(ColumnPlan(col, "live", "fill from is a SQL expression"))
            continue
        # Mode: Off is the kill-switch for every automatic build, refresh,
        # rebuild and drop (the hourly usage bump on an attached column is
        # metadata-only and keeps last_used_at honest, so nothing is evicted
        # the moment Mode returns to auto). An index whose fingerprint still matches
        # is read as-is (the live tail keeps results exact however stale it
        # is); Setup's own actions remain the way to change it.
        mode_open = _mode_open(form, cfg)
        if entry:
            bookmark = _parse_iso(entry.get("bookmark"))
            refreshed = _parse_iso(entry.get("refreshed_at")) or _parse_iso(entry.get("built_at"))
            days = cfg["refresh_days"]
            override = (entry.get("overrides") or {}).get("refresh_days")
            if override not in (None, ""):
                days = int(override)
            if bookmark is None:
                if mode_open:
                    plan.columns.append(ColumnPlan(col, "rebuild", "index has no bookmark", None))
                else:
                    plan.columns.append(ColumnPlan(col, "live", "index has no bookmark; automatic indexing is off"))
                continue
            if mode_open and refreshed is not None and days >= 0 and now - refreshed > timedelta(days=days) and run is not None:
                plan.columns.append(ColumnPlan(col, "refresh", "older than the staleness target", bookmark))
            else:
                plan.columns.append(ColumnPlan(col, "attach", "", bookmark))
            continue
        if stale_config and col.key in reg_cols:
            if mode_open:
                plan.columns.append(ColumnPlan(col, "rebuild", "mapping changed since the index was built"))
            else:
                plan.columns.append(ColumnPlan(col, "live", "mapping changed since the index was built; automatic indexing is off"))
            continue
        if not mode_open:
            plan.columns.append(ColumnPlan(col, "live", "automatic indexing is off"))
            continue
        if not is_text_column(form, col.expr):
            plan.columns.append(ColumnPlan(col, "live", NON_TEXT))
            continue
        if not auto_ok:
            plan.columns.append(ColumnPlan(col, "live", NO_PREVIEW))
            continue
        if not _write_ok(form):
            plan.columns.append(ColumnPlan(col, "live", "no create rights on the write destination"))
            continue
        if run is None or not allow_probe:
            cached = probes.get(col.key) if isinstance(probes, dict) else None
            density = cached.get("density") if isinstance(cached, dict) else None
            if density is None:
                plan.columns.append(ColumnPlan(col, "live", NOT_CHECKED))
                continue
        else:
            density = _probe_density(form, run, col.expr, probes, col.key, now)
        if density is not None and density > DENSITY_MAX:
            plan.columns.append(
                ColumnPlan(col, "live", "the value is on most rows already")
            )
            continue
        plan.columns.append(ColumnPlan(col, "build", "first use"))
    return plan


# ---------------------------------------------------------------- apply


def _census(form: dict[str, Any], run: RunFn) -> dict[str, dict[str, Any]] | None:
    """Name → {rows, first, last} from the event-name census, if it exists."""
    stored = stored_event_name_cache(form)
    if not stored or int(stored.get("v") or 0) < 2:
        return None
    try:
        res = run(event_name_cache_census_sql(form))
    except AdapterError:
        return None
    out: dict[str, dict[str, Any]] = {}
    for row in res.rows:
        name = row.get("fc_value")
        if name is None:
            continue
        out[str(name)] = {
            "rows": int(row.get("fc_rows") or 0),
            "first": _iso_or_none(row.get("fc_first")),
            "last": _iso_or_none(row.get("fc_last")),
        }
    return out


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return str(value)


def _read_bookmarks(form: dict[str, Any], run: RunFn, key: str) -> dict[str | None, datetime]:
    res = run(bookmarks_sql(form, key))
    out: dict[str | None, datetime] = {}
    for row in res.rows:
        bm = row.get("fc_bookmark")
        if bm is None:
            continue
        if not isinstance(bm, datetime):
            parsed = _parse_iso(bm)
            if parsed is None:
                continue
            bm = parsed
        if bm.tzinfo is None:
            bm = bm.replace(tzinfo=timezone.utc)
        name = row.get("fc_event_name")
        out[str(name) if name is not None else None] = bm
    return out


def _census_repairs(
    entry: dict[str, Any], census: dict[str, dict[str, Any]] | None, known: set[str]
) -> tuple[list[str], list[str]]:
    """(names to backfill whole, names to rebuild) from the census diff."""
    if census is None:
        return [], []
    snapshot = entry.get("names") if isinstance(entry.get("names"), dict) else {}
    new_names = [n for n in census if n not in known and n not in snapshot]
    rebuild: list[str] = []
    for name, now_row in census.items():
        was = snapshot.get(name)
        if not isinstance(was, dict):
            continue
        if int(now_row.get("rows") or 0) < int(was.get("rows") or 0):
            rebuild.append(name)
            continue
        first_now = _parse_iso(now_row.get("first"))
        first_was = _parse_iso(was.get("first"))
        if first_now and first_was and first_now < first_was:
            rebuild.append(name)
    return new_names, rebuild


def apply_plan(
    plan: Plan, form: dict[str, Any], run: RunFn, *, now: datetime | None = None
) -> dict[str, Any]:
    """Run the builds / refreshes the plan asked for, sequentially, then
    write the registry to the table description. A failure turns that
    column live for this run and is reported, never raised.

    Returns the registry mirror to persist.
    """
    now = now or datetime.now(timezone.utc)
    registry = plan.registry
    if not plan.builds():
        return registry
    if plan.dest is None:
        return registry
    fp = config_fingerprint(form)
    if registry.get("fp") != fp:
        registry = {"v": REGISTRY_VERSION, "fp": fp, "columns": {}, "probes": registry.get("probes") or {}}
        plan.registry = registry
    cols: dict[str, Any] = registry.setdefault("columns", {})
    census = _census(form, run)
    ensured = False
    for cp in plan.builds():
        key = cp.column.key
        label = cp.column.label
        try:
            if not ensured:
                run(ensure_table_sql(form, registry))
                ensured = True
            if cp.action in ("build", "rebuild"):
                if cp.action == "rebuild":
                    run(delete_column_sql(form, key))
                run(backfill_sql(form, key, cp.column.expr))
                entry = {
                    "expr": cp.column.expr,
                    "label": label,
                    "built_at": now_iso(now),
                    "refreshed_at": now_iso(now),
                    "last_used_at": now_iso(now),
                    "use_count": int((cols.get(key) or {}).get("use_count") or 0),
                    "pinned": bool((cols.get(key) or {}).get("pinned")),
                    "overrides": dict((cols.get(key) or {}).get("overrides") or {}),
                }
            else:
                entry = dict(cols.get(key) or {})
                known = _read_bookmarks(form, run, key)
                new_names, rebuild_names = _census_repairs(
                    entry, census, {n for n in known if n is not None}
                )
                lookback = plan.settings["lookback_days"]
                override = (entry.get("overrides") or {}).get("lookback_days")
                if override not in (None, ""):
                    lookback = int(override)
                if rebuild_names:
                    run(delete_column_sql(form, key, names=rebuild_names))
                    run(backfill_sql(form, key, cp.column.expr, names=rebuild_names))
                    known = {n: bm for n, bm in known.items() if n not in rebuild_names}
                if new_names:
                    run(backfill_sql(form, key, cp.column.expr, names=new_names))
                if known:
                    days = {n: bm.date() for n, bm in known.items()}
                    run(refresh_sql(form, key, cp.column.expr, bookmarks=days, lookback_days=lookback))
                entry["refreshed_at"] = now_iso(now)
            marks = _read_bookmarks(form, run, key)
            entry["bookmark"] = now_iso(max(marks.values())) if marks else None
            if census is not None:
                entry["names"] = {n: census[n] for n in census}
            cols[key] = entry
            cp.bookmark = _parse_iso(entry.get("bookmark"))
            plan.built.append(label)
            cp.action = "attach" if cp.bookmark is not None else "live"
            if cp.bookmark is None:
                cp.reason = "no values recorded yet"
        except (AdapterError, ValueError) as exc:
            cp.action = "live"
            cp.reason = str(exc)
            plan.failures.append(f"{label}: {exc}")
            plan.errors.append(exc)
    try:
        run(registry_comment_sql(form, registry))
    except AdapterError as exc:
        plan.failures.append(f"registry: {exc}")
        plan.errors.append(exc)
    return registry


def bump_usage(
    plan: Plan, form: dict[str, Any], run: RunFn | None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Touch ``last_used_at`` for attached columns, at most hourly per
    column; the description write is a metadata statement, batched."""
    now = now or datetime.now(timezone.utc)
    registry = plan.registry
    cols = registry.get("columns") if isinstance(registry.get("columns"), dict) else {}
    dirty = False
    for cp in plan.columns:
        if cp.action != "attach":
            continue
        entry = cols.get(cp.column.key)
        if not isinstance(entry, dict):
            continue
        last = _parse_iso(entry.get("last_used_at"))
        entry["use_count"] = int(entry.get("use_count") or 0) + 1
        if last is None or now - last >= timedelta(minutes=USAGE_BUMP_MINUTES):
            entry["last_used_at"] = now_iso(now)
            dirty = True
    if dirty and run is not None and plan.dest is not None:
        try:
            run(registry_comment_sql(form, registry))
        except AdapterError:
            pass
    return registry


# ---------------------------------------------------------------- sweep


def sweep(
    form: dict[str, Any], run: RunFn, *, now: datetime | None = None
) -> tuple[dict[str, Any], list[str], bool]:
    """Drop unpinned columns unused for the TTL. Returns (registry,
    dropped labels, ran). At most once per SWEEP_HOURS per config file."""
    now = now or datetime.now(timezone.utc)
    registry = registry_from_form(form)
    last = _parse_iso(form.get("managed_last_sweep"))
    if last is not None and now - last < timedelta(hours=SWEEP_HOURS):
        return registry, [], False
    cfg = settings(form)
    if not _mode_open(form, cfg):
        # Off: no automatic write of any kind, and the clock does not advance.
        return registry, [], False
    dest = index_table(form)
    cols = registry.get("columns") if isinstance(registry.get("columns"), dict) else {}
    dropped: list[str] = []
    if dest is None or not cols:
        return registry, dropped, True
    ttl = timedelta(days=cfg["drop_days"])
    for key in list(cols):
        entry = cols[key]
        if not isinstance(entry, dict) or entry.get("pinned"):
            continue
        used = _parse_iso(entry.get("last_used_at")) or _parse_iso(entry.get("built_at"))
        if used is None or now - used < ttl:
            continue
        try:
            run(delete_column_sql(form, key))
        except AdapterError as exc:
            if not is_missing_relation(exc):
                continue
        dropped.append(str(entry.get("label") or key))
        del cols[key]
    if dropped:
        try:
            if cols:
                run(registry_comment_sql(form, registry))
            else:
                run(drop_table_sql(form))
        except AdapterError:
            pass
    return registry, dropped, True


# ---------------------------------------------------------------- setup list


def notes_for(plan: Plan, *, bytes_build: int | None = None, bytes_after: int | None = None) -> str:
    """The Events second line, in the business register: cost, reason,
    next-run cost. Never 'index'."""
    labels = [cp.column.label for cp in plan.builds()]
    if not labels:
        return ""
    what = ", ".join(f"`{l}`" for l in labels)
    verb = "prepares" if len(labels) == 1 else "prepare"
    text = f"Also {verb} {what} for faster breakdowns"
    if bytes_build is not None:
        text += f" (one-time ~ {_gb(bytes_build)})"
    text += "."
    if bytes_after is not None:
        text += f" Later runs ~ {_gb(bytes_after)}."
    return text


def rows_mode_form(form: dict[str, Any]) -> dict[str, Any]:
    """The same chart with every expensive Value-at slot downgraded to
    each-event + keep NULL: its dry-run bytes are the honest "later runs"
    price before an index exists. Deliberately approximate ("~" in the
    copy): a real post-index run also reads the index and its live tail,
    and the first Run pays a density probe (~PROBE_DAYS of one column,
    cached PROBE_TTL_DAYS). Do not "fix" this by pricing the spliced query:
    the index does not exist yet, so that dry run cannot be made."""

    out = copy.deepcopy(form)

    def downgrade(slots: Any) -> None:
        if not isinstance(slots, list):
            return
        for slot in slots:
            if isinstance(slot, dict):
                slot["value_at"] = "event"
                slot["if_missing"] = "null"

    downgrade(out.get("breakdowns"))
    for unit in out.get("series") or []:
        if isinstance(unit, dict):
            downgrade(unit.get("breakdowns"))
    out["breakdown_at"] = "rows"
    return out


def maybe_note(plan: Plan, *, bytes_after: int | None = None) -> str:
    """Before the first build: preparing a column costs about what reading
    its history once costs, which this run pays anyway — so the chip already
    covers it; say what later runs cost."""
    labels = [cp.column.label for cp in plan.maybe()]
    if not labels:
        return ""
    what = ", ".join(f"`{l}`" for l in labels)
    text = f"May also prepare {what} for faster breakdowns (one-time, included above)."
    if bytes_after is not None:
        text += f" Later runs ~ {_gb(bytes_after)}."
    return text


def built_note(plan: Plan) -> str:
    """After a run that prepared columns: what happened, in the business
    register, past tense."""
    if not plan.built:
        return ""
    what = ", ".join(f"`{l}`" for l in plan.built)
    return f"Prepared {what} for faster breakdowns. Later runs cost less."


def failure_note(plan: Plan) -> str:
    if not plan.failures:
        return ""
    first = plan.failures[0]
    label, _, why = first.partition(": ")
    return (
        f"Could not prepare `{label}` for faster breakdowns: {why} "
        f"This run read the full history instead; the chart is correct."
    )


def _gb(n: int) -> str:
    gb = n / (1024 ** 3)
    if gb >= 10:
        return f"{gb:.0f} GB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = n / (1024 ** 2)
    return f"{mb:.0f} MB"


# ---------------------------------------------------------------- setup: list + actions


def _stats(form: dict[str, Any], table: str) -> dict[str, Any]:
    """Size / rows / description of a managed relation via metadata calls
    (BigQuery get_table; Snowflake SHOW TABLES). Lazy adapter imports keep
    the default install free of warehouse SDKs."""
    kind = form_kind(form)
    if kind == "snowflake":
        from factcat.warehouses.snowflake import table_stats
        from factcat_app.catalog import _sf_auth

        return table_stats(
            database=str(form.get("write_database") or "").strip(),
            schema=str(form.get("write_schema") or "").strip(),
            table=table,
            **_sf_auth(form),
        )
    from factcat.warehouses.bigquery import table_stats
    from factcat_app.catalog import _creds

    return table_stats(
        project=str(form.get("write_project") or "").strip(),
        dataset=str(form.get("write_dataset") or "").strip(),
        table=table,
        credentials=_creds(form),
    )


def authoritative_registry(form: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The registry from the index table's description (the authority),
    plus its stats; ``({}, None)`` when the table does not exist."""
    try:
        stats = _stats(form, INDEX_TABLE)
    except AdapterError as exc:
        if is_missing_relation(exc):
            return {}, None
        raise
    return parse_registry(stats.get("description")), stats


def reconcile_registry(form: dict[str, Any]) -> dict[str, Any]:
    """The registry to plan from on the Run path: the index table's own
    description when the table exists (the authority), an empty registry
    when it does not — never the mirror alone, which can outlive a table
    dropped out of band or point at a new, empty destination. The mirror's
    probe cache is kept either way (it is about the events table)."""
    mirror = registry_from_form(form)
    probes = mirror.get("probes") if isinstance(mirror.get("probes"), dict) else {}
    try:
        authority, stats = authoritative_registry(form)
    except AdapterError:
        # Metadata unavailable: plan from the mirror rather than fail the run.
        return mirror
    if stats is None:
        return {"probes": probes}
    if not authority:
        return {"probes": probes}
    authority.setdefault("probes", {}).update(probes)
    return authority


def list_payload(form: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """What the Setup section renders: settings, the event-name cache row,
    one row per indexed column, the mirror to persist."""
    now = now or datetime.now(timezone.utc)
    cfg = settings(form)
    dest = index_table(form)
    payload: dict[str, Any] = {"settings": cfg, "dest": dest, "tables": [], "columns": []}
    if dest is None:
        return payload
    registry, stats = authoritative_registry(form)
    if not registry:
        registry = registry_from_form(form)
        if stats is None:
            registry = {**registry, "columns": {}} if registry else {}
    payload["registry"] = registry
    if stats is not None:
        payload["tables"].append(
            {
                "name": INDEX_TABLE,
                "bytes": stats.get("bytes"),
                "rows": stats.get("rows"),
            }
        )
    try:
        names_stats = _stats(form, "fc_event_names")
        cache_meta = stored_event_name_cache(form)
        payload["tables"].append(
            {
                "name": "fc_event_names",
                "bytes": names_stats.get("bytes"),
                "rows": names_stats.get("rows"),
                "kind": names_stats.get("kind"),
                "census": int(cache_meta.get("v") or 0) >= 2,
            }
        )
    except AdapterError as exc:
        if not is_missing_relation(exc):
            raise
    cols = registry.get("columns") if isinstance(registry.get("columns"), dict) else {}
    for key, entry in cols.items():
        if not isinstance(entry, dict):
            continue
        payload["columns"].append(
            {
                "key": key,
                "label": entry.get("label") or key,
                "expr": entry.get("expr"),
                "built_at": entry.get("built_at"),
                "refreshed_at": entry.get("refreshed_at"),
                "last_used_at": entry.get("last_used_at"),
                "use_count": entry.get("use_count") or 0,
                "bookmark": entry.get("bookmark"),
                "pinned": bool(entry.get("pinned")),
                "overrides": dict(entry.get("overrides") or {}),
                "stale": (registry.get("fp") != config_fingerprint(form)),
            }
        )
    return payload


def _raise_first_failure(plan: Plan) -> None:
    """Setup actions surface the first failure. A cap rejection is re-raised
    as itself so the handler can report bytes and cap; anything else as the
    labelled message."""
    if not plan.failures:
        return
    first = plan.errors[0] if plan.errors else None
    if isinstance(first, BytesCapError):
        raise first
    raise AdapterError(plan.failures[0])


def apply_action(
    form: dict[str, Any],
    run: RunFn,
    *,
    action: str,
    key: str = "",
    expr: str = "",
    label: str = "",
    value: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One Setup action on the index. Returns the registry mirror to persist.

    refresh / rebuild / drop act on an indexed column; ``index`` adds a
    column now (bypasses the density gate — the admin chose); ``pin`` and
    ``override`` edit bookkeeping only.
    """
    now = now or datetime.now(timezone.utc)
    dest = index_table(form)
    if dest is None:
        raise ValueError("Set a write destination on Setup first")
    registry, _stats_ = authoritative_registry(form)
    if not registry:
        registry = registry_from_form(form) or {}
    fp = config_fingerprint(form)
    if registry.get("fp") != fp:
        registry = {"v": REGISTRY_VERSION, "fp": fp, "columns": {}, "probes": registry.get("probes") or {}}
    cols: dict[str, Any] = registry.setdefault("columns", {})
    if action == "pin":
        entry = cols.get(key)
        if not isinstance(entry, dict):
            raise ValueError("no such indexed column")
        entry["pinned"] = bool(value)
        run(registry_comment_sql(form, registry))
        return registry
    if action == "override":
        entry = cols.get(key)
        if not isinstance(entry, dict):
            raise ValueError("no such indexed column")
        clean: dict[str, Any] = {}
        for name in ("refresh_days", "lookback_days", "loaded_at_column"):
            raw = (value or {}).get(name) if isinstance(value, dict) else None
            if raw in (None, ""):
                continue
            if name == "loaded_at_column":
                clean[name] = _ident_column(str(raw), "loaded_at_column")
            else:
                n = int(raw)
                if n < 0:
                    raise ValueError(f"{name} must be at least 0")
                clean[name] = n
        entry["overrides"] = clean
        run(registry_comment_sql(form, registry))
        return registry
    if action == "drop":
        if key not in cols:
            raise ValueError("no such indexed column")
        try:
            run(delete_column_sql(form, key))
        except AdapterError as exc:
            if not is_missing_relation(exc):
                raise
        del cols[key]
        if cols:
            run(registry_comment_sql(form, registry))
        else:
            run(drop_table_sql(form))
        return registry
    if action == "index":
        if not expr:
            raise ValueError("a column or expression is required")
        if not is_text_column(form, expr):
            raise ValueError(f"{label or expr} is {NON_TEXT}")
        column = IndexColumn(column_key(expr, label or expr), expr, label or expr, "any", None)
        plan = Plan([ColumnPlan(column, "build", "admin")], dest, None, registry, settings(form))
        registry = apply_plan(plan, form, run, now=now)
        _raise_first_failure(plan)
        return registry
    entry = cols.get(key)
    if not isinstance(entry, dict):
        raise ValueError("no such indexed column")
    column = IndexColumn(key, str(entry.get("expr") or ""), str(entry.get("label") or key), "any", None)
    if action == "rebuild":
        plan = Plan([ColumnPlan(column, "rebuild", "admin")], dest, None, registry, settings(form))
    elif action == "refresh":
        bookmark = _parse_iso(entry.get("bookmark"))
        plan = Plan(
            [ColumnPlan(column, "refresh" if bookmark else "rebuild", "admin", bookmark)],
            dest, None, registry, settings(form),
        )
    else:
        raise ValueError("action must be refresh, rebuild, drop, index, pin, or override")
    registry = apply_plan(plan, form, run, now=now)
    _raise_first_failure(plan)
    return registry

