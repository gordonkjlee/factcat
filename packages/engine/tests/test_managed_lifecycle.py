"""The managed-tables lifecycle, executed on rows.

``test_managed.py`` proves the chassis by counting statement strings from a
fake warehouse. That cannot see whether the SQL those statements carry
actually lands the rows it claims to: an INSERT that selects nothing, an
anti-join that doubles the tail, a rebuild that leaves a hole. Here the
same public functions (``build_plan`` / ``apply_plan`` / ``sweep``) drive
``fc_column_index`` on an in-memory DuckDB, the registry round-trips through
the real config file, and every assertion is on rows and bookmarks against a
hand-written events table.

Each step names the mutation it guards in its docstring. All of them must go
red on ROWS, not on the order of statements.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from factcat.warehouses import AdapterError, QueryResult, is_missing_relation
from factcat_app import config, managed
from factcat_app.query import WAREHOUSE_KINDS

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

# subscription_id, event_name, occurred_at, plan
# A subscription business: `plan` is recorded on the start of a subscription
# (and once, deliberately, on a payment) and NULL on the rest. S5 starts
# with no plan at all, so a start alone must not be indexed.
EVENTS = [
    ("S1", "subscription_started", "2026-06-01 09:00:00", "basic"),
    ("S1", "payment", "2026-06-01 09:05:00", None),
    ("S1", "payment", "2026-07-01 09:00:00", None),
    ("S1", "payment", "2026-08-01 09:00:00", None),
    ("S2", "subscription_started", "2026-06-10 14:00:00", "pro"),
    ("S2", "payment", "2026-06-10 14:02:00", None),
    ("S2", "payment", "2026-07-10 14:00:00", None),
    ("S2", "subscription_started", "2026-07-20 10:00:00", "basic"),  # downgrade
    ("S3", "subscription_started", "2026-07-03 08:30:00", "pro"),
    ("S3", "payment", "2026-07-03 08:31:00", None),
    ("S3", "payment", "2026-08-03 08:30:00", None),
    ("S4", "subscription_started", "2026-08-20 16:00:00", "basic"),
    ("S4", "payment", "2026-08-20 16:01:00", None),
    ("S5", "subscription_started", "2026-08-25 11:00:00", None),
    ("S5", "payment", "2026-08-25 11:01:00", None),
    ("S5", "payment", "2026-08-28 11:00:00", "trial"),  # plan on a payment
    ("S1", "payment", "2026-09-01 09:00:00", None),
    ("S2", "payment", "2026-08-10 14:00:00", None),
    ("S3", "payment", "2026-09-01 08:30:00", None),
    ("S4", "payment", "2026-08-30 16:00:00", None),
]

# Counted by hand from EVENTS: the rows where `plan` is set.
PLAN_ROWS = 6
PLAN_ROWS_BY_NAME = {"subscription_started": 5, "payment": 1}
PLAN_BOOKMARK = "2026-08-28T11:00:00+00:00"  # S5's trial payment
# The density probe reads the 30 days before NOW (from 2026-08-03): ten rows,
# two of them with a plan.
PROBE_DENSITY = 2 / 10

LATER_EVENTS = [
    ("S6", "subscription_started", "2026-09-05 09:00:00", "pro"),
    ("S3", "subscription_started", "2026-09-08 12:00:00", "basic"),
]
PLAN_ROWS_AFTER = 8
PLAN_BOOKMARK_AFTER = "2026-09-08T12:00:00+00:00"

# What the index must hold once both batches are in, as (entity, at, value, name).
INDEXED_AFTER = {
    ("S1", datetime(2026, 6, 1, 9, 0), "basic", "subscription_started"),
    ("S2", datetime(2026, 6, 10, 14, 0), "pro", "subscription_started"),
    ("S2", datetime(2026, 7, 20, 10, 0), "basic", "subscription_started"),
    ("S3", datetime(2026, 7, 3, 8, 30), "pro", "subscription_started"),
    ("S4", datetime(2026, 8, 20, 16, 0), "basic", "subscription_started"),
    ("S5", datetime(2026, 8, 28, 11, 0), "trial", "payment"),
    ("S6", datetime(2026, 9, 5, 9, 0), "pro", "subscription_started"),
    ("S3", datetime(2026, 9, 8, 12, 0), "basic", "subscription_started"),
}

INDEX = '"memory"."main"."fc_column_index"'


class _DuckRun:
    """The ``run`` callable over an in-memory DuckDB: executes what managed
    emits and hands rows back the way an adapter would."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self.calls: list[str] = []

    def __call__(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        self.calls.append(sql)
        try:
            cur = self.con.execute(sql)
            desc = cur.description
            raw = cur.fetchall() if desc else []
        except duckdb.Error as exc:
            raise AdapterError(
                str(exc), not_found=isinstance(exc, duckdb.CatalogException)
            ) from exc
        names = [str(col[0]).lower() for col in desc] if desc else []
        return QueryResult(rows=[dict(zip(names, row)) for row in raw], bytes_processed=0)


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE main.events ("
        "  subscription_id VARCHAR,"
        "  event_name VARCHAR,"
        "  occurred_at TIMESTAMP,"
        "  plan VARCHAR"
        ")"
    )
    connection.executemany("INSERT INTO main.events VALUES (?, ?, ?, ?)", EVENTS)
    return connection


@pytest.fixture()
def lifecycle(con, tmp_path, monkeypatch):
    # form_kind validates against the shipped adapters; DuckDB is the
    # reference dialect, not an adapter, so admit it for this file only.
    monkeypatch.setattr("factcat_app.query.WAREHOUSE_KINDS", (*WAREHOUSE_KINDS, "duckdb"))
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / ".factcat.json"))
    return _Lifecycle(con)


def _form(**extra) -> dict:
    base = {
        "kind": "duckdb",
        "table": "memory.main.events",
        "entity": "subscription_id",
        "event_time": "occurred_at",
        # The stored column is the instant itself: no cast in the bound, so
        # the emitted DuckDB SQL is the same shape the warehouses get.
        "event_time_tz": "reporting",
        "event_column": "event_name",
        "event_values": ["payment"],
        "measure": "total",
        "grain": "day",
        "breakdowns": [
            {
                "breakdown_column": "plan",
                "value_at": "event",
                "if_missing": "fill",
                "fill_from_event": "subscription_started",
            }
        ],
        "write_project": "memory",
        "write_dataset": "main",
    }
    base.update(extra)
    return base


class _Lifecycle:
    """One chart Run at a time, the way the app sequences it: the registry
    is read from the config file before each step and persisted per column
    as the rows land."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self.run = _DuckRun(con)

    def form(self, **extra) -> dict:
        return _form(managed_tables=config.load()["managed_tables"], **extra)

    @staticmethod
    def persist(registry: dict) -> None:
        config.save({"managed_tables": registry})

    def chart_run(self, now: datetime, **extra) -> managed.Plan:
        form = self.form(**extra)
        plan = managed.build_plan(form, self.run, now=now, allow_probe=True)
        managed.apply_plan(plan, form, self.run, persist=self.persist, now=now)
        return plan

    def sweep(self, now: datetime, **extra):
        form = self.form(**extra)
        return managed.sweep(form, self.run, persist=self.persist, now=now)

    def registry_entry(self) -> dict:
        return config.load()["managed_tables"]["columns"]["plan"]

    def index_rows(self) -> set[tuple]:
        return set(
            self.con.execute(
                f"SELECT fc_entity, fc_at, fc_value, fc_event_name FROM {INDEX} "
                "WHERE fc_column = 'plan'"
            ).fetchall()
        )

    def index_count(self) -> int:
        return self.con.execute(f"SELECT COUNT(*) FROM {INDEX}").fetchone()[0]

    def index_missing(self) -> bool:
        try:
            self.run(f"SELECT COUNT(*) FROM {INDEX}")
        except AdapterError as exc:
            return is_missing_relation(exc)
        return False


def _counts(entry: dict) -> dict[str, int]:
    return {name: entry["row_counts"][managed._count_key(name)] for name in PLAN_ROWS_BY_NAME}


# ---------------------------------------------------------------- the cycle


def test_first_run_backfills_exactly_the_recorded_values(lifecycle):
    """Step 1. The first Run builds the column: the index holds one row per
    event where `plan` was set, and nothing else - not the start with no
    plan, not the payments. The bookmark is the newest of those rows and the
    per-name counts are the hand count.

    Mutations: skip the ensure statement (the INSERT has no table); drop the
    `IS NOT NULL` filter from the backfill SELECT (NULL plans land);
    stamp the bookmark from the events table instead of the index.
    """
    plan = lifecycle.chart_run(NOW)
    assert plan.columns[0].action == "attach", plan.columns[0].reason
    assert plan.built == ["plan"] and plan.failures == []
    assert lifecycle.index_count() == PLAN_ROWS
    assert {r[0] for r in lifecycle.index_rows()} == {"S1", "S2", "S3", "S4", "S5"}
    entry = lifecycle.registry_entry()
    assert entry["bookmark"] == PLAN_BOOKMARK
    assert _counts(entry) == PLAN_ROWS_BY_NAME
    assert config.load()["managed_tables"]["probes"]["plan"]["density"] == pytest.approx(PROBE_DENSITY)


def test_refresh_folds_in_only_the_new_rows(lifecycle):
    """Step 2. Two later events land; a Run past the staleness target folds
    them in and nothing else: +2 rows, no duplicate of the overlap window the
    lookback re-reads, bookmark on the newest.

    Mutations: drop the anti-join from refresh_sql (the lookback window
    doubles up); drop the lookback subtraction AND the anti-join together
    (rows on the bookmark day vanish); read the tail from the index's own
    bookmark without the per-name branch (the payment bookmark hides S6).
    """
    lifecycle.chart_run(NOW)
    lifecycle.con.executemany("INSERT INTO main.events VALUES (?, ?, ?, ?)", LATER_EVENTS)
    later = NOW + timedelta(days=8)
    form = lifecycle.form()
    assert managed.build_plan(form, lifecycle.run, now=later).columns[0].action == "refresh"
    plan = lifecycle.chart_run(later)
    assert plan.repaired == [], "an ordinary refresh read as tampering"
    assert lifecycle.index_count() == PLAN_ROWS_AFTER
    assert lifecycle.index_rows() == INDEXED_AFTER
    entry = lifecycle.registry_entry()
    assert entry["bookmark"] == PLAN_BOOKMARK_AFTER
    assert _counts(entry) == {"subscription_started": 7, "payment": 1}
    # a second refresh with nothing new changes nothing
    lifecycle.chart_run(later + timedelta(days=8))
    assert lifecycle.index_rows() == INDEXED_AFTER


def test_a_row_deleted_behind_us_comes_back_whole(lifecycle):
    """Step 3. Something outside Factcat deletes a MIDDLE row from the index.
    The bookmark cannot see it; the per-name count can. The next Run rebuilds
    the column from history rather than appending a tail onto the hole, so
    the index again holds exactly the hand-written set - the deleted row is
    back and nothing is doubled.

    Mutation: ignore `row_counts` in apply_plan (the tail is appended, the
    hole stays, the count is one short).
    """
    lifecycle.chart_run(NOW)
    lifecycle.con.executemany("INSERT INTO main.events VALUES (?, ?, ?, ?)", LATER_EVENTS)
    lifecycle.chart_run(NOW + timedelta(days=8))
    assert lifecycle.index_rows() == INDEXED_AFTER
    lifecycle.con.execute(
        f"DELETE FROM {INDEX} WHERE fc_column = 'plan' AND fc_entity = 'S3' AND fc_value = 'pro'"
    )
    assert lifecycle.index_count() == PLAN_ROWS_AFTER - 1
    plan = lifecycle.chart_run(NOW + timedelta(days=16))
    assert lifecycle.index_count() == PLAN_ROWS_AFTER, "the hole was never filled"
    assert lifecycle.index_rows() == INDEXED_AFTER
    assert plan.repaired == ["plan"], "the deletion went unnoticed"
    entry = lifecycle.registry_entry()
    assert _counts(entry) == {"subscription_started": 7, "payment": 1}
    assert entry["bookmark"] == PLAN_BOOKMARK_AFTER


def test_an_unused_column_is_swept_and_the_empty_table_dropped(lifecycle):
    """Step 4. The chart stops asking for `plan`; past the drop TTL the sweep
    removes its rows and, it being the last column, the table itself. The
    registry says so before the rows go.

    Mutations: skip the DELETE / DROP after the registry write (rows outlive
    their record); drop only the rows and leave an empty shell.
    """
    lifecycle.chart_run(NOW)
    assert lifecycle.index_count() == PLAN_ROWS
    past_ttl = NOW + timedelta(days=61)
    registry, dropped, ran = lifecycle.sweep(past_ttl, breakdowns=[])
    assert ran and dropped == ["plan"]
    assert registry["columns"] == {} and config.load()["managed_tables"]["columns"] == {}
    assert lifecycle.index_missing(), "the empty index table was left behind"


def test_the_sweep_keeps_what_the_chart_still_asks_for(lifecycle):
    """Step 4, the other half. The same clock, but the chart still names
    `plan`: servable demand shelters it and every row stays.

    Mutation: drop the `key in wanted` guard from sweep.
    """
    lifecycle.chart_run(NOW)
    _registry, dropped, ran = lifecycle.sweep(NOW + timedelta(days=61))
    assert ran and dropped == []
    assert lifecycle.index_count() == PLAN_ROWS


def test_mode_off_sweeps_nothing_and_the_index_persists(lifecycle):
    """Step 5. Off stops every automatic write: past the TTL, with nothing
    asking for the column, the sweep returns early and the rows are still
    there for when the toggle goes back on.

    Mutation: drop the mode check from sweep (the rows are deleted and the
    table dropped).
    """
    lifecycle.chart_run(NOW)
    assert lifecycle.index_count() == PLAN_ROWS
    _registry, dropped, ran = lifecycle.sweep(
        NOW + timedelta(days=61), managed_mode="off", breakdowns=[]
    )
    assert not lifecycle.index_missing(), "Off dropped the index"
    assert lifecycle.index_count() == PLAN_ROWS
    assert config.load()["managed_tables"]["columns"]["plan"]["bookmark"] == PLAN_BOOKMARK
    assert (ran, dropped) == (False, [])
