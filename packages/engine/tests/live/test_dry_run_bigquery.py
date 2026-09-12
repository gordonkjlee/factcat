"""Every statement shape the product emits, compiled by the sandbox
warehouse itself. Dry runs only, after one bootstrap: they bill nothing.

Why the hermetic suite is not enough: sqlglot does not type-check, and
DuckDB coerces where BigQuery refuses. Proof that this tier earns its
place - drop the CAST from the folded ``'(other)'`` branch in
``factcat/events.py`` (the CASE that ``tests/test_dialects.py`` guards by
text alone). The hermetic suite keeps passing everywhere but that one
textual assertion, and would be entirely green without it; here the
numeric-breakdown variant goes red, because BigQuery rejects a CASE whose
arms have no common type. A dry run is the warehouse's own compile and
type-check, priced at zero bytes billed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

import pytest

from factcat.warehouses import (
    ADAPTERS,
    CAP_DRY_RUN,
    AdapterError,
    capabilities,
    is_missing_relation,
)
from factcat_app import managed
from factcat_app.query import (
    event_name_cache_census_sql,
    event_name_cache_read_sql,
    event_name_cache_rebuild_sql,
    event_values_sql,
    events_sql_from_form,
)

pytestmark = pytest.mark.live

# An event name the sandbox need not contain: name-scoped statements only
# have to compile, and a dry run never looks for rows.
PROBE_EVENT = "fc_live_probe"


def _ready(kind: str, live: Any) -> None:
    if CAP_DRY_RUN not in capabilities(kind):
        pytest.skip(f"{kind} cannot dry-run")
    if kind != live.kind:
        pytest.skip(f"the mapping is {live.kind}, not {kind}")


def _dry(live: Any, sql: str, sqlglot_warnings: Any) -> None:
    assert sqlglot_warnings.messages == [], sqlglot_warnings.messages
    result = live.adapter.run(sql, dry_run=True)
    # A dry run reports an estimate in bytes_processed and bills nothing;
    # an empty sandbox estimates 0, a populated one does not, so only the
    # billed figure is pinned.
    assert result.rows == []
    assert result.bytes_processed is not None, "the warehouse returned no estimate"
    assert result.bytes_billed in (None, 0), result.bytes_billed


def _text(live: Any, n: int) -> list[str]:
    cols = live.columns("event_column")
    if len(cols) < n:
        pytest.skip(f"the mapping lists {len(cols)} text column(s); this shape needs {n}")
    return cols[:n]


# ---------------------------------------------------------------- bootstrap


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_00_bootstrap(kind, live):
    """The managed relations the later dry runs reference, created by the
    product's own DDL: the column index empty, the event-name cache as the
    app would build it (materialized view first, table when refused)."""
    _ready(kind, live)
    form = live.form()
    live.adapter.run(managed.ensure_table_sql(form))
    live.adapter.run(managed.ensure_table_sql(form))  # IF NOT EXISTS: idempotent
    # The index's own recovery read: no rows means no column was ever built
    # here, which is what a sandbox destination must look like.
    assert live.adapter.run(managed.recovered_columns_sql(form)).rows == []
    last: AdapterError | None = None
    for materialized in (True, False):
        try:
            live.adapter.run(event_name_cache_rebuild_sql(form, materialized=materialized))
            break
        except AdapterError as exc:
            last = exc
    else:
        raise last or AdapterError("could not create the event-name cache")


# ---------------------------------------------------------------- events


def _base(live: Any) -> dict[str, Any]:
    return live.form()


def _week(live: Any) -> dict[str, Any]:
    return live.form(grain="week", range_mode="last", range_n=8, range_unit="week")


def _hour(live: Any) -> dict[str, Any]:
    return live.form(grain="hour", range_mode="last", range_n=24, range_unit="hour")


def _day_of_week(live: Any) -> dict[str, Any]:
    return live.form(grain="day_of_week")


def _hour_of_day(live: Any) -> dict[str, Any]:
    return live.form(grain="hour_of_day")


def _one_breakdown(live: Any) -> dict[str, Any]:
    (col,) = _text(live, 1)
    return live.form(breakdown_column=col)


def _two_breakdowns(live: Any) -> dict[str, Any]:
    a, b = _text(live, 2)
    return live.form(breakdowns=[{"breakdown_column": a}, {"breakdown_column": b}])


def _value_semantics(live: Any) -> dict[str, Any]:
    """Value at x If missing x Fill from, on as many slots as the mapping
    has text columns for (at least one)."""
    _text(live, 1)
    cols = live.columns("event_column")[:3]
    slots = [
        {
            "breakdown_column": cols[0],
            "value_at": "event",
            "if_missing": "fill",
            "fill_from_event": PROBE_EVENT,
        },
        {"breakdown_column": cols[min(1, len(cols) - 1)], "value_at": "range_start"},
        {
            "breakdown_column": cols[min(2, len(cols) - 1)],
            "value_at": "range_end",
            "if_missing": "fill",
        },
    ][: len(cols)]
    return live.form(
        range_mode="custom",
        custom_kind="absolute",
        start_date="2026-01-01",
        end_date="2026-01-31",
        breakdowns=slots,
    )


def _numeric_fold(live: Any) -> dict[str, Any]:
    """The proof shape: a numeric breakdown folded into '(other)'."""
    cols = live.columns(numeric=True)
    if not cols:
        pytest.skip("the mapping lists no numeric column")
    return live.form(breakdown_column=cols[0], include_other=True, top_n=8)


def _epoch_millis(live: Any) -> dict[str, Any]:
    cols = live.columns("event_time_unix")
    if not cols:
        pytest.skip("the mapping lists no integer column to read as epoch milliseconds")
    return live.form(event_time=cols[0], event_time_epoch="milliseconds", event_time_tz="utc")


def _named_zone(live: Any) -> dict[str, Any]:
    cols = live.columns("event_time_wallclock")
    if not cols:
        pytest.skip("the mapping lists no wall-clock column to read in a named zone")
    return live.form(event_time=cols[0], event_time_epoch="", event_time_tz="Europe/Berlin")


EVENT_SHAPES: dict[str, Callable[[Any], dict[str, Any]]] = {
    "base": _base,
    "week": _week,
    "hour": _hour,
    "day_of_week": _day_of_week,
    "hour_of_day": _hour_of_day,
    "one_breakdown": _one_breakdown,
    "two_breakdowns": _two_breakdowns,
    "value_semantics": _value_semantics,
    "numeric_fold": _numeric_fold,
    "epoch_millis": _epoch_millis,
    "named_zone": _named_zone,
}


@pytest.mark.parametrize("kind", list(ADAPTERS))
@pytest.mark.parametrize("shape", list(EVENT_SHAPES))
def test_events_shapes_dry_run(kind, shape, live, sqlglot_warnings):
    _ready(kind, live)
    form = EVENT_SHAPES[shape](live)
    _dry(live, events_sql_from_form(form), sqlglot_warnings)


# ---------------------------------------------------------------- managed


def _indexed_form(live: Any, col: str, now: datetime) -> dict[str, Any]:
    """A form whose registry says ``col`` is indexed, so the chart reads
    the index and the live tail (values relation and watermark are
    fragments; they reach the warehouse only through this statement)."""
    stamp = now.isoformat()
    form = live.form(
        breakdowns=[
            {
                "breakdown_column": col,
                "value_at": "event",
                "if_missing": "fill",
                "fill_from_event": "__charted__",
            }
        ],
        event_values=[PROBE_EVENT],
    )
    form["managed_tables"] = {
        "v": 1,
        "fp": managed.config_fingerprint(form),
        "columns": {
            col: {
                "expr": col,
                "label": col,
                "built_at": stamp,
                "refreshed_at": stamp,
                "last_used_at": stamp,
                "bookmark": stamp,
                "use_count": 1,
                "pinned": False,
                "overrides": {},
            }
        },
    }
    return form


def _attached_chart(live: Any, col: str, today: date) -> str:
    now = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    form = _indexed_form(live, col, now)
    plan = managed.build_plan(form, None, now=now)
    assert [c.action for c in plan.columns] == ["attach"]
    return events_sql_from_form(form, managed=plan)


MANAGED_SHAPES: dict[str, Callable[[Any, dict[str, Any], str, date], str]] = {
    "ensure_table": lambda live, f, col, today: managed.ensure_table_sql(f),
    "backfill_select": lambda live, f, col, today: managed.backfill_select_sql(f, col, col),
    "backfill": lambda live, f, col, today: managed.backfill_sql(f, col, col),
    "backfill_names": lambda live, f, col, today: managed.backfill_sql(
        f, col, col, names=[PROBE_EVENT]
    ),
    "bookmarks": lambda live, f, col, today: managed.bookmarks_sql(f, col),
    "recovered_columns": lambda live, f, col, today: managed.recovered_columns_sql(f),
    "refresh": lambda live, f, col, today: managed.refresh_sql(
        f, col, col, bookmarks={PROBE_EVENT: today, None: today}, lookback_days=3
    ),
    "delete_column": lambda live, f, col, today: managed.delete_column_sql(f, col),
    "delete_column_names": lambda live, f, col, today: managed.delete_column_sql(
        f, col, names=[PROBE_EVENT]
    ),
    "density_probe": lambda live, f, col, today: managed.density_probe_sql(f, col, today=today),
    "drop_table": lambda live, f, col, today: managed.drop_table_sql(f),
    "attached_chart": lambda live, f, col, today: _attached_chart(live, col, today),
    "event_name_cache_census": lambda live, f, col, today: event_name_cache_census_sql(f),
    "event_name_cache_read": lambda live, f, col, today: event_name_cache_read_sql(f),
    "event_values_catalog": lambda live, f, col, today: event_values_sql({**f, "catalog": True}),
    "event_values_window": lambda live, f, col, today: event_values_sql(f),
}


@pytest.mark.parametrize("kind", list(ADAPTERS))
@pytest.mark.parametrize("shape", list(MANAGED_SHAPES))
def test_managed_shapes_dry_run(kind, shape, live, sqlglot_warnings):
    _ready(kind, live)
    (col,) = _text(live, 1)
    form = live.form()
    sql = MANAGED_SHAPES[shape](live, form, col, date.today())
    _dry(live, sql, sqlglot_warnings)


# ------------------------------------------------ the event-name cache's two shapes


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_one_event_name_cache_shape_is_always_legal(kind, live, sqlglot_warnings):
    """The cache is a materialized view or a table at one destination, and the
    app picks by trying one shape and falling back when the warehouse refuses
    it (`catalog_event_values` -> `create_then_read`). A dry run is validated
    against the object that is already there, so on a destination that holds
    one shape the other is refused for its type. What must hold is the property
    the fallback rests on: at least one shape estimates at zero billed bytes,
    and a refusal names the type rather than the SQL.

    This replaces two cases that dry-ran both shapes unconditionally. They
    passed on 2026-09-06 only because the destination did not exist yet; the
    first run after the app had built the cache reported the second shape's
    type refusal as a release-blocking failure.

    Mutation: accept `is_missing_relation` as a type refusal, and a destination
    that has vanished reads as a healthy fallback.
    """
    _ready(kind, live)
    form = live.form()
    legal, refusals = [], []
    for name, materialized in (("materialized_view", True), ("table", False)):
        sql = event_name_cache_rebuild_sql(form, materialized=materialized)
        try:
            _dry(live, sql, sqlglot_warnings)
        except AdapterError as exc:
            assert not is_missing_relation(exc), f"{name}: {exc}"
            message = str(exc).lower()
            assert "type" in message or "not allowed" in message, f"{name}: {exc}"
            refusals.append(name)
        else:
            legal.append(name)
    assert legal, f"the warehouse refused both cache shapes: {refusals}"


# ---------------------------------------------------------------- connection


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_select_one(kind, live):
    """The one statement that executes rather than dry-runs: a constant,
    so it reads no table and bills nothing."""
    _ready(kind, live)
    result = live.adapter.run("SELECT 1 AS n")
    assert result.rows[0]["n"] == 1
