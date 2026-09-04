"""Factcat-managed tables: the column index and its bookkeeping.

``fc_column_index`` holds, for a handful of breakdown columns, every row
where that column had a value — entity, instant, value, event name — so
the expensive Value-at modes (carried, range anchors, first / latest ever)
read a small relation plus a short live tail instead of the events table's
full history. The library side is ``Breakdown.values_table``; this module
decides when to build, refresh, attach, or drop, and emits the SQL.

Rules that shaped it:

* The registry lives in ``.factcat.json`` — columns, bookmarks, pin state,
  overrides, use counts, the config fingerprint — written the moment each
  column's build finishes, never batched behind the rest of a run. Nothing
  in the warehouse describes this beyond the rows themselves: a table
  comment used to carry a copy and was the single failure point (a real
  61M-row backfill once landed with the comment write silently lost).
  Single-install scope, by design — the mirror is not shared
  between installs pointed at the same destination. Recovery, used only
  when the mirror cannot answer (missing, or the destination just
  changed), re-derives columns and bookmarks straight from the index
  table's own rows and defaults the rest; it never guesses an unmatched
  column's expression, because a wrong one would corrupt the next refresh.
* Build on Run, sequentially: the first run of a qualifying column builds
  its index first, then queries through it. Never on an estimate — an
  estimate is a free dry run and must not bill a probe or write a table.
* Correctness never depends on freshness: every query reads the index
  plus the live rows after its bookmark, so a stale index is a cost, not
  a wrong answer. Refresh folds the tail in when it is older than the
  staleness target.
* Removal is automatic and lazy: a clean-up during a chart Run - at most
  once every ``SWEEP_HOURS``, never in the background, nothing is
  scheduled - drops columns unused for the TTL, except one the request in
  flight is asking for. Adding costs a scan, so it waits for a real need;
  dropping costs nothing, so it needs nobody.
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
    splice_placeholders,
)
from factcat._emit import transpile
from factcat.warehouses import AdapterError, QueryResult, is_missing_relation

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


def _dest_name(form: dict[str, Any]) -> str:
    """The write destination as a plain dotted name. The fingerprint travels
    as JSON through a table comment, so it carries no quoting: a quoted ident
    would need escaping on the way out and back."""
    # The same parts write_relation uses, and no billing-project fallback:
    # query.py refuses that deliberately, and two definitions of "the write
    # destination" would drift.
    if form_kind(form) == "snowflake":
        parts = [form.get("write_database"), form.get("write_schema")]
    else:
        parts = [form.get("write_project"), form.get("write_dataset")]
    # Keep every part, even when the destination is incomplete: collapsing
    # them all to "" would let two differently half-configured destinations
    # share a fingerprint, and old bookmarks would attach to a new table.
    names = [str(p or "").strip() for p in parts]
    return ".".join(names + [INDEX_TABLE])


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
        # A plain dotted name, never the quoted ident: this document
        # round-trips through a table comment.
        "dest": _dest_name(form),
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
                # An expression slot has no label: without this the user
                # is told "Indexed `None`".
                out[key] = IndexColumn(key, expr, label or key, fill, names)
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


def ensure_table_sql(form: dict[str, Any]) -> str:
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


def recovered_columns_sql(form: dict[str, Any]) -> str:
    """Every ``fc_column`` with real rows, its earliest row and its
    bookmark - derived straight from the index table's own data. The
    recovery path when the registry mirror cannot answer; no WHERE, so
    this reads the whole table, which is why it runs only on a miss, never
    on an ordinary load. ``fc_first_at`` becomes the recovered entry's
    ``built_at``: without it a recovered column never refreshes, because
    the refresh branch requires a non-null ``refreshed_at``."""
    dest = index_table(form)
    if dest is None:
        raise ValueError("write destination is required")
    return _emit(
        f"SELECT fc_column, MIN(fc_at) AS fc_first_at, MAX(fc_at) AS fc_bookmark "
        f"FROM {dest} GROUP BY 1",
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


def watermark_sql(form: dict[str, Any], bookmark: datetime, lookback_days: int = 0) -> str:
    """The day the bookmark falls on LESS the late-arrival lookback, in the
    stored column's type: the live tail re-reads from that midnight.

    The refresh applies the same lookback to its floors (`refresh_sql`) and
    the tail must apply it too. Without it, a row that lands after the scan
    carrying an event time just before the bookmark sits in neither side -
    not in the index (the scan had passed it) and not in the tail (`>`
    bookmark) - so a value goes missing until some later refresh folds it
    in. Subtracting the lookback also swamps the day floor's reporting-
    timezone offset (at most 14 h, against a default of 3 days). The
    overlap is duplicates only, and the engine is proven indifferent."""
    # At least one day, even when the caller sets the lookback to 0: the day
    # floor is computed in the reporting timezone, which can sit up to 14 h
    # after the bookmark, and a tail that starts after it would miss rows
    # that are in neither side. A day of overlap is duplicates only.
    day = (bookmark - timedelta(days=max(1, lookback_days))).date()
    return _date_bound(form, day)


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
    # Failures of the registry write, kept apart from column failures: the
    # index IS in place when one of these happens, so reporting it as
    # "could not index `registry`; this run read the full history" would be
    # false twice over.
    registry_failures: list[str] = field(default_factory=list)
    built: list[str] = field(default_factory=list)
    # Columns rebuilt because rows had gone missing from the index behind
    # us. Kept apart from `built` so the run row can say "Rebuilt" rather
    # than "Indexed": the column was already indexed, and the user is being
    # charged for a full-history backfill they did not ask for. Both lists
    # carry the label - `built` is every column whose rows landed this run,
    # `repaired` is the subset that had to be rebuilt.
    repaired: list[str] = field(default_factory=list)

    def maybe(self) -> list[ColumnPlan]:
        """Columns that would be indexed but have not been measured yet: the
        estimate never probes, so before the first run of a column the answer
        is honestly "may". Says so rather than staying silent."""
        return [cp for cp in self.columns if cp.action == "live" and cp.reason == NOT_CHECKED]

    def builds(self) -> list[ColumnPlan]:
        return [c for c in self.columns if c.action in ("build", "rebuild", "refresh")]

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
            watermark_sql(form, col.bookmark, self.settings["lookback_days"]),
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
    total = _field(rows, "fc_rows")
    present = _field(rows, "fc_present")
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


def build_plan(
    form: dict[str, Any],
    run: RunFn | None,
    *,
    now: datetime | None = None,
    allow_probe: bool = False,
) -> Plan:
    """Decide per expensive column: attach the index, build it first,
    refresh it first, rebuild it, or run live.

    ``run`` may be None (estimate path): only cached facts are used and
    nothing is billed. ``allow_probe`` lets the run path measure density
    for a column the mirror has not seen. Builds are automatic on every
    warehouse: Mode: Automatic is the consent (a build costs about what the
    chart would have scanned, and the chart runs without a preview on
    warehouses that have none).
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
        # Mode: Off says whether Factcat may USE the indexes, not only whether
        # it may maintain them - so it is the kill-switch for every automatic
        # build, refresh, rebuild and drop AND stops an existing index being
        # read. Charts then scan full history, which is the honest cost of
        # turning indexing off. Nothing is deleted: the sweep returns early
        # under Off, and its demand guard means a column the chart asks for
        # survives the switch back on rather than being evicted for having
        # gone unserved. Setup's own actions remain the way to change it.
        mode_open = _mode_open(form, cfg)
        if entry:
            # Off is a USE toggle, not only a maintenance one: it says whether
            # Factcat may read the index at all. Charts then read full history
            # and scan more, which is the honest meaning of turning indexing
            # off and is said where the toggle lives. The rows are kept and
            # nothing is dropped - `sweep` returns early under Off, and the
            # demand guard there means the index is still waiting when the
            # toggle goes back on.
            if not mode_open:
                plan.columns.append(ColumnPlan(col, "live", "indexing is off"))
                continue
            bookmark = _parse_iso(entry.get("bookmark"))
            refreshed = _parse_iso(entry.get("refreshed_at")) or _parse_iso(entry.get("built_at"))
            days = cfg["refresh_days"]
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
            try:
                density = _probe_density(form, run, col.expr, probes, col.key, now)
            except AdapterError as exc:
                # The probe is a billed query over the events table: a cap
                # rejection or a permission error there must leave the chart
                # alone, exactly as a failed build does.
                plan.columns.append(ColumnPlan(col, "live", f"could not measure the column: {exc}"))
                continue
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
        name = _field(row, "fc_value")
        if name is None:
            continue
        out[str(name)] = {
            "rows": int(_field(row, "fc_rows") or 0),
            "first": _iso_or_none(_field(row, "fc_first")),
            "last": _iso_or_none(_field(row, "fc_last")),
        }
    return out


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return str(value)


def _field(row: Any, name: str) -> Any:
    """Read one generated column out of a result row, whatever case the
    warehouse reported it in.

Adapters normalise case now, so this is defence in depth rather than
    the fix: ``run`` is a plain callable here, and a caller that does not
    fold would otherwise return None for a column we named, which
    ``int(None or 0)`` turns into a silent zero. Every column read here is
    one this module named (``fc_*``), so matching case-insensitively can
    only widen what is already ours.
    """
    if not isinstance(row, dict):
        return None
    if name in row:
        return row[name]
    lowered = name.lower()
    for key, value in row.items():
        if str(key).lower() == lowered:
            return value
    return None


def _count_key(name: Any) -> str:
    """A collision-free JSON key for one event name.

    JSON has no null key, and a caller's event column can legitimately hold
    BOTH NULL and the empty string - both warehouses distinguish them, and
    raw event data routinely contains both. Folding NULL to "" put two
    GROUP BY rows in one bucket, last row wins, and GROUP BY order is not
    guaranteed: the same index read as 900 rows or as 3 depending on the
    order the warehouse happened to return. That is a false NEGATIVE in one
    direction (800 rows can then vanish unnoticed - the detector's whole
    purpose) and a false POSITIVE in the other (a healthy column rebuilt
    from full history on every refresh - the expense this exists to avoid).

    So tag the key: "-" is NULL, "=" + the name is a value. The empty name
    is "=", which cannot equal "-".
    """
    return "-" if name is None else "=" + str(name)


def _read_index_state(
    form: dict[str, Any], run: RunFn, key: str
) -> tuple[dict[str | None, datetime], dict[str, int] | None]:
    """One column's per-event-name bookmarks AND row counts.

    ``bookmarks_sql`` has always selected ``COUNT(*) AS fc_rows`` and the
    reader that preceded this one always threw it away. Keeping it makes
    divergence detectable at all: a bookmark only moves when the NEWEST row
    changes, so comparing bookmarks cannot see rows deleted from the middle
    or the old end, nor one event name's rows removed while another's stay -
    which are the shapes that actually lose history. A count that went DOWN
    is unambiguous, because only Factcat writes this table.

    Counts are keyed by ``_count_key``, which keeps NULL and the empty
    string apart.
    """
    res = run(bookmarks_sql(form, key))
    marks: dict[str | None, datetime] = {}
    # `None` means the read is not trustworthy enough to compare against.
    counts: dict[str, int] | None = {}
    for row in res.rows:
        name = _field(row, "fc_event_name")
        if counts is not None:
            try:
                counts[_count_key(name)] = int(_field(row, "fc_rows") or 0)
            except (TypeError, ValueError):
                # Dropping just this name would make it read as a deletion
                # on the next comparison and cost a full-history rebuild of
                # a healthy column. One unreadable count makes the whole
                # read unusable, and saying nothing is the safe answer.
                counts = None
        bm = _field(row, "fc_bookmark")
        if bm is None:
            continue
        if not isinstance(bm, datetime):
            parsed = _parse_iso(bm)
            if parsed is None:
                continue
            bm = parsed
        if bm.tzinfo is None:
            bm = bm.replace(tzinfo=timezone.utc)
        marks[str(name) if name is not None else None] = bm
    return marks, counts


def _rows_went_missing(stored: Any, fresh: dict[str, int] | None) -> bool:
    """True when any event name holds FEWER rows than we last recorded.

    Only Factcat writes this table, and every path that removes rows
    re-stamps in the same breath, so a decrease means something outside
    Factcat deleted them - a retention job, a governance tool, a hand-run
    DELETE. Growth is ordinary (a refresh appends), so only a decrease is a
    signal. A name absent from the fresh read counts as zero. A ``fresh``
    of ``None`` means the read could not be trusted, and says nothing.
    """
    if not isinstance(stored, dict) or not stored or not isinstance(fresh, dict):
        return False
    for key, was in stored.items():
        try:
            if int(was) > int(fresh.get(str(key), 0)):
                return True
        except (TypeError, ValueError):
            continue
    return False


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
    plan: Plan,
    form: dict[str, Any],
    run: RunFn,
    *,
    persist: Callable[[dict[str, Any]], None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the builds / refreshes the plan asked for, sequentially. A
    failure turns that column live for this run and is reported, never
    raised.

    ``persist``, when given, is called with the registry right after EACH
    column's own rows land - not batched behind the rest of the plan. That
    used to be one write to the table's description, after the whole loop,
    swallowed on failure: a real backfill could land while the record of it
    never did. The caller is expected to write ``.factcat.json``
    from it, synchronously, before this function moves on to the next
    column - the local file is the one thing this design counts on to be
    fast and reliable enough not to need batching.

    Returns the registry mirror to persist (the same document `persist` was
    already called with, for a caller that only wants the final state).
    """
    now = now or datetime.now(timezone.utc)
    registry = plan.registry
    if not plan.builds():
        return registry
    if plan.dest is None:
        return registry
    fp = config_fingerprint(form)
    if registry.get("fp") != fp:
        # The mapping moved. Every column in the table was built under the
        # old one, and this plan rebuilds only the columns THIS chart uses:
        # resetting the registry alone would leave the others' rows behind
        # with no entry - invisible to the Setup list and to the sweep - and
        # the next chart to use one would union two generations of fc_at
        # under a single fc_column. The table is derived, so drop it whole
        # and let each column come back on the chart that needs it.
        if registry.get("columns"):
            try:
                run(drop_table_sql(form))
            except AdapterError as exc:
                if not is_missing_relation(exc):
                    plan.registry_failures.append(str(exc))
            for cp in plan.builds():
                cp.action = "build"
                cp.bookmark = None
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
                run(ensure_table_sql(form))
                ensured = True
            if cp.action in ("build", "rebuild"):
                # A backfill always clears the column first, build included.
                # Rows can outlive their registry entry - a Drop whose DELETE
                # failed after the registry write, a second workstation that
                # built the same column - and appending onto them doubles the
                # table. Deleting nothing is cheap; the backfill is not.
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
                # The read the refresh needs anyway, now also carrying the
                # per-name counts. One statement, two answers: nothing extra
                # is billed to find out whether the index still holds what it
                # claimed.
                known, fresh_counts = _read_index_state(form, run, key)
                if _rows_went_missing(entry.get("row_counts"), fresh_counts):
                    # Rows this column had are gone and Factcat did not remove
                    # them. Appending a tail onto a hole would answer from an
                    # index missing history - silently, with no error - so
                    # rebuild the column whole instead of refreshing it.
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
                    plan.repaired.append(label)
                else:
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
            # The post-write read: everything this Run wrote has landed, so
            # these counts are the state to compare against next time. Stamping
            # here rather than before the writes is what stops Factcat's own
            # census repair (which deletes names and re-inserts them) reading
            # as tampering on the next refresh.
            marks, counts = _read_index_state(form, run, key)
            # An untrustworthy read stamps nothing rather than a partial
            # picture: absent reads as "no information" next time, a partial
            # dict would read as deletion.
            entry["row_counts"] = counts if counts is not None else {}
            entry["bookmark"] = now_iso(max(marks.values())) if marks else None
            if census is not None:
                entry["names"] = {n: census[n] for n in census}
            cols[key] = entry
            cp.bookmark = _parse_iso(entry.get("bookmark"))
            cp.action = "attach" if cp.bookmark is not None else "live"
            if cp.bookmark is None:
                # The backfill landed no rows, so the chart runs live. Saying
                # "Indexed `x`" here was false: `built` is what the run row
                # reports, and it must describe what happened rather than
                # what was attempted.
                cp.reason = "no values recorded yet"
            else:
                plan.built.append(label)
            # Persist now, this column, before the next one's rows are even
            # requested: a crash after this line still remembers what really
            # landed. Batching it behind the loop is what lost a
            # completed backfill's record entirely.
            if persist is not None:
                persist(registry)
        except (AdapterError, ValueError) as exc:
            cp.action = "live"
            cp.reason = str(exc)
            plan.failures.append(f"{label}: {exc}")
    return registry


def bump_usage(plan: Plan, *, now: datetime | None = None) -> dict[str, Any]:
    """Touch ``last_used_at`` for attached columns, at most hourly per
    column. Pure in-memory update; the caller persists the mirror same as
    everywhere else — there is no separate warehouse write to batch."""
    now = now or datetime.now(timezone.utc)
    registry = plan.registry
    cols = registry.get("columns") if isinstance(registry.get("columns"), dict) else {}
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
    return registry


# ---------------------------------------------------------------- sweep


def sweep(
    form: dict[str, Any],
    run: RunFn,
    *,
    persist: Callable[[dict[str, Any]], None] | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[str], bool]:
    """Drop unpinned columns unused for the TTL. Returns (registry,
    dropped labels, ran). At most once per SWEEP_HOURS per config file.

    ``persist``, when given, is called with EACH column already removed,
    right BEFORE that column's own DELETE runs - the same order
    `apply_action`'s drop keeps, for the same reason: the other order
    leaves the mirror claiming a bookmark for rows already gone if the
    process dies between a successful DELETE and the local write, and a
    later run then attaches to an empty column and reads only the live
    tail, silently wrong. Persist-first trades that for "not indexed, rows
    possibly still there" on a failure - the same trade `apply_action`
    already makes, safe because a future build for that key always clears
    it before backfilling. Not batched behind the rest of the sweep or the
    chart query that may follow in the same `/api/run` request, either
    way."""

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
    # What THIS request is asking for. The sweep runs before the plan, so
    # without this it evicts on "unused as of a moment ago" while the request
    # in flight is the use: coming back to a chart after the TTL dropped the
    # column, dropped the table, and then rebuilt it from full history in the
    # same Run - the act of using a column destroyed it. Pure form parsing,
    # no warehouse call.
    # `fill == "expr"` is excluded because `build_plan` routes those to live
    # BEFORE it ever looks at the registry: a slot filled from a SQL
    # expression is demand this Run can never serve, and sheltering it would
    # keep a column alive on a chart that will not read it - a cache held up
    # by unservable demand, which is the opposite of demand-shaped.
    wanted = (
        {c.key for c in expensive_columns(form) if c.fill != "expr"}
        if registry.get("fp") == config_fingerprint(form)
        else set()
    )
    for key in list(cols):
        entry = cols[key]
        if not isinstance(entry, dict):
            continue
        # Demand this request can actually be served: the chart asks for it,
        # the mapping still matches, and there is a bookmark to attach to.
        # Not "recently used" - being used right now.
        if key in wanted and entry.get("bookmark"):
            continue
        # `pinned` / `overrides` keys from a pre-trim registry (never shipped)
        # are ignored: use is the only thing that keeps a column.
        used = _parse_iso(entry.get("last_used_at")) or _parse_iso(entry.get("built_at"))
        if used is None or now - used < ttl:
            continue
        label = str(entry.get("label") or key)
        del cols[key]
        if persist is not None:
            persist(registry)
        try:
            run(delete_column_sql(form, key))
        except AdapterError as exc:
            if not is_missing_relation(exc):
                # The mirror already says this column is gone; its rows may
                # not be. Safe either way - a future build for this key
                # always clears it before backfilling, the same trade
                # apply_action's drop already makes on the same failure.
                dropped.append(label)
                continue
        dropped.append(label)
    if dropped and not cols:
        # The last column went: the table is now empty and derived, so drop
        # it whole rather than leave an empty shell. Already persisted above
        # (registry is already columns: {} at this point).
        try:
            run(drop_table_sql(form))
        except AdapterError:
            pass
    return registry, dropped, True


# ---------------------------------------------------------------- setup list


def pending_note(
    plan: Plan, *, bytes_build: int | None = None, bytes_after: int | None = None
) -> str:
    """Before the run: one short line in the same right-aligned slot as the
    line after it, saying that this Run may also index a column and that
    later runs get cheaper for it.

    The owner asked for terse and right-aligned, not for silence: removing
    this outright in the trim lost the only warning that a Run was about to
    do more than chart. Figures ride it only when the estimate already has
    them (the probe is cached, so the build is priced) — the estimate never
    probes, so an unmeasured column is honestly a "may" with no number.
    """
    building = [cp.column.label for cp in plan.builds()]
    if building:
        what = ", ".join(f"`{l}`" for l in building)
        bits = []
        if bytes_build is not None:
            bits.append(f"one-time ~ {_gb(bytes_build)}")
        if bytes_after is not None:
            bits.append(f"later runs ~ {_gb(bytes_after)}")
        tail = " \u00b7 " + ", ".join(bits) if bits else ""
        return f"Also indexes {what}{tail}"
    unsure = [cp.column.label for cp in plan.maybe()]
    if unsure:
        what = ", ".join(f"`{l}`" for l in unsure)
        return f"May also index {what}, which makes later runs cheaper"
    # A refusal the caller can act on is worth one line: an expensive mode on
    # a column Factcat will never index is a permanent silent slow path, and
    # the reason was write-only until an owner asked why nothing appeared.
    for cp in plan.columns:
        if cp.action != "live":
            continue
        if cp.reason == NON_TEXT:
            return (
                f"Not indexing `{cp.column.label}`: the index stores text. "
                f"Use `CAST({cp.column.expr} AS STRING)` as the breakdown to index it."
            )
        if cp.reason.startswith("no create rights"):
            return (
                f"Not indexing `{cp.column.label}`: no rights to create tables in the "
                f"write destination."
            )
    return ""


def built_note(plan: Plan, *, bytes_after: int | None = None) -> str:
    """After a run that indexed columns: one short line, past tense, shown
    on the right of the results toolbar until the next run. Before the run
    nothing is said — the chip already includes the build."""
    if not plan.built:
        return ""
    tail = f"later runs ~ {_gb(bytes_after)}" if bytes_after is not None else "later runs read less"
    # A repaired column was already indexed; the user is paying for a
    # full-history backfill because rows went missing behind us, so the verb
    # has to differ. Same grammar, same backticked label - the accurate verb,
    # not a second sentence family.
    fresh = [l for l in plan.built if l not in plan.repaired]
    repaired = [l for l in plan.built if l in plan.repaired]
    parts = []
    if fresh:
        parts.append("Indexed " + ", ".join(f"`{l}`" for l in fresh))
    if repaired:
        parts.append("Rebuilt " + ", ".join(f"`{l}`" for l in repaired))
    return " \u00b7 ".join(parts) + f" \u00b7 {tail}"


def failure_note(plan: Plan) -> str:
    if plan.failures:
        label, _, why = plan.failures[0].partition(": ")
        return (
            f"Could not index `{label}`: {why} "
            f"This run read the full history instead; the chart is correct."
        )
    if plan.registry_failures:
        # This can only fire from the stale-fingerprint branch now (dropping
        # the PREVIOUS generation's table failed) - the columns THIS plan
        # built are recorded regardless, via `persist`, per column, as they
        # land. What is at risk is the old generation's rows, not this run's.
        return (
            f"Could not clear the previous index generation: {plan.registry_failures[0]} "
            f"This run's columns are correct and recorded; the earlier generation's rows "
            f"may still be present until a later run clears them."
        )
    return ""


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


def recover_registry_from_rows(
    form: dict[str, Any], run: RunFn, *, now: datetime | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Re-derive columns and bookmarks straight from ``fc_column_index``'s
    own rows. The recovery path when the registry mirror is genuinely
    empty - never on an ordinary read, and never on a destination change
    (that resets to empty and lets the ordinary build path re-index
    instead: rows at a destination this mirror never held a record for
    have zero local grounding for a mapping-compatibility guess).
    ``({}, None)`` when the table does not exist.

    A recovered key attaches only when it matches a column the CURRENT
    mapping would produce (by its ``column_key``) - never with a guessed
    expression. An unmatched key is left out rather than attached wrong:
    unindexed-and-rebuilt is safe, a wrong expr silently writing bad values
    into a future refresh is not.
    """
    try:
        stats = _stats(form, INDEX_TABLE)
    except AdapterError as exc:
        if is_missing_relation(exc):
            return {}, None
        raise
    now = now or datetime.now(timezone.utc)

    def _dt(value: Any) -> datetime | None:
        if value is not None and not isinstance(value, datetime):
            value = _parse_iso(value)
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value if isinstance(value, datetime) else None

    known = {c.key: c for c in expensive_columns(form)}
    if not known:
        # Nothing the current chart wants could ever match, so the WHERE-
        # less scan of the whole table would run for nothing on every
        # render, forever, whenever the current mapping has no expensive
        # columns at all (e.g. Value at reset to "each event").
        return {}, stats
    result = run(recovered_columns_sql(form))
    columns: dict[str, Any] = {}
    for row in result.rows:
        cand = known.get(_field(row, "fc_column"))
        if cand is None:
            continue
        first = _dt(_field(row, "fc_first_at"))
        bm = _dt(_field(row, "fc_bookmark"))
        columns[cand.key] = {
            "expr": cand.expr,
            "label": cand.label,
            # `built_at` and `refreshed_at` come from the rows' own history,
            # not from None: an entry with neither can never take the
            # refresh branch (it requires one), so it would attach once and
            # then never fold in a new row again.
            "built_at": now_iso(first) if first else None,
            "refreshed_at": now_iso(bm) if bm else None,
            # `last_used_at` is NOW, though - not the data's age. The sweep
            # evicts on `last_used_at or built_at`, and a recovered column's
            # `built_at` is the oldest event in the index, routinely years
            # back: leaving this None fed the whole recovered backfill
            # straight to the next sweep, which dropped the column and then
            # the table - destroying exactly what recovery just rescued.
            # It was found and used this second, which is what use means.
            "last_used_at": now_iso(now),
            "use_count": 0,
            "pinned": False,
            "overrides": {},
            "bookmark": now_iso(bm) if bm else None,
        }
    registry = {"v": REGISTRY_VERSION, "fp": config_fingerprint(form), "columns": columns}
    return registry, stats


def reconcile_registry(
    form: dict[str, Any], run: RunFn, *, now: datetime | None = None
) -> dict[str, Any]:
    """The registry to plan and display from: the local mirror, trusted
    directly once it is written the moment each column's build completes
    (``apply_plan``'s ``persist``) - not the warehouse, which used to be
    asked on every read and could itself go stale on its own failed write
    on its own failed write. Protected two ways, both cheap: a destination
    invalidates the mirror outright (its columns describe a different
    physical table, and the physical rows there - if any - were not
    necessarily built under THIS entity/table mapping either, so this
    starts clean rather than recovering and trust-attaching what might be
    someone else's data); a mirror
    claiming columns while the table itself is gone out of band resets to
    empty via one metadata call. Recovery - re-deriving from
    ``fc_column_index``'s own rows - runs only when the mirror is genuinely
    empty for the CURRENT destination, never on a destination change and
    never on an ordinary read where the mirror already has an answer. The
    mirror's probe cache is kept either way (it is about the events table,
    not this)."""
    mirror = registry_from_form(form)
    probes = mirror.get("probes") if isinstance(mirror.get("probes"), dict) else {}
    dest = index_table(form)
    if dest is None:
        return {"probes": probes}
    fp = config_fingerprint(form)
    cols = mirror.get("columns") if isinstance(mirror.get("columns"), dict) else {}
    mirror_fp = mirror.get("fp") if isinstance(mirror.get("fp"), dict) else {}
    dest_changed = bool(cols) and mirror_fp.get("dest") != fp.get("dest")
    if dest_changed:
        return {"probes": probes}
    if not cols:
        try:
            recovered, stats = recover_registry_from_rows(form, run, now=now)
        except AdapterError:
            return {"probes": probes}
        if stats is None or not recovered.get("columns"):
            return {"probes": probes}
        recovered["probes"] = probes
        return recovered
    try:
        _stats(form, INDEX_TABLE)
    except AdapterError as exc:
        if is_missing_relation(exc):
            # Dropped out of band: the mirror's bookmarks describe rows
            # that are gone.
            return {"probes": probes}
        # Metadata unavailable for another reason: plan from the mirror
        # rather than fail the run.
        return mirror
    return mirror


def list_payload(form: dict[str, Any], run: RunFn, *, now: datetime | None = None) -> dict[str, Any]:
    """What the Setup section renders: settings, the event-name cache row,
    one row per indexed column, the mirror to persist."""
    now = now or datetime.now(timezone.utc)
    cfg = settings(form)
    dest = index_table(form)
    payload: dict[str, Any] = {"settings": cfg, "dest": dest, "tables": [], "columns": []}
    if dest is None:
        return payload
    registry = reconcile_registry(form, run)
    try:
        stats = _stats(form, INDEX_TABLE)
    except AdapterError as exc:
        if not is_missing_relation(exc):
            raise
        stats = None
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


def apply_action(
    form: dict[str, Any],
    run: RunFn,
    *,
    action: str,
    key: str = "",
    persist: Callable[[dict[str, Any]], None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The one Setup action on the index: ``drop`` a column (and the table
    when it was the last one). Returns the registry mirror to persist.
    Building, refreshing and rebuilding are Automatic mode's job on every
    warehouse; the owner withdrew the manual paths (Index a column now,
    Refresh, Rebuild, Pin, Overrides) as noise for the people who use Setup.

    ``persist``, when given, is called with the reduced registry BEFORE the
    DELETE/DROP runs — record first, rows second. The other order leaves
    the record claiming a bookmark for rows already gone when the DELETE
    fails; a later run then attaches to an empty column and reads only the
    live tail, silently wrong, with no error. The mirror is a local write,
    so this ordering costs nothing to keep.
    """
    now = now or datetime.now(timezone.utc)
    dest = index_table(form)
    if dest is None:
        raise ValueError("Set a write destination on Setup first")
    if action != "drop":
        raise ValueError("action must be drop")
    registry = registry_from_form(form) or {}
    fp = config_fingerprint(form)
    if registry.get("fp") != fp:
        # The mapping moved, so every column in the table is a stale
        # generation. Setup still lists them and the guides promise Drop as
        # the immediate erasure remedy, so honour it by dropping the table
        # whole rather than refusing with "no such indexed column".
        reset = {"v": REGISTRY_VERSION, "fp": fp, "columns": {}, "probes": registry.get("probes") or {}}
        if registry.get("columns"):
            if persist is not None:
                persist(reset)
            try:
                run(drop_table_sql(form))
            except AdapterError as exc:
                if not is_missing_relation(exc):
                    raise
        return reset
    cols: dict[str, Any] = registry.setdefault("columns", {})
    if key not in cols:
        raise ValueError("no such indexed column")
    del cols[key]
    if cols:
        if persist is not None:
            persist(registry)
        try:
            run(delete_column_sql(form, key))
        except AdapterError as exc:
            if not is_missing_relation(exc):
                raise
    else:
        if persist is not None:
            persist(registry)
        run(drop_table_sql(form))
    return registry
