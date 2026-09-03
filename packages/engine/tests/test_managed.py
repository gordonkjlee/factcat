"""Factcat-managed tables: the column index chassis.

Mutations that must go red: skip the ensure statement before a backfill
(the build test counts statements in order); attach an index whose config
fingerprint no longer matches (the stale-config test); run a density
probe from the estimate path (the estimate-plan test forbids ``run``);
drop a pinned column on sweep; let ``notes_for`` say "index" in the
Events register; batch the registry mirror write behind the whole plan
instead of persisting it after each column.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from factcat.warehouses import ADAPTERS, AdapterError, QueryResult
from factcat_app import managed
from factcat_app.managed import (
    ColumnPlan,
    IndexColumn,
    Plan,
    apply_plan,
    backfill_sql,
    bookmarks_sql,
    build_plan,
    bump_usage,
    column_key,
    config_fingerprint,
    delete_column_sql,
    density_probe_sql,
    drop_table_sql,
    ensure_table_sql,
    expensive_columns,
    built_note,
    failure_note,
    index_table,
    now_iso,
    recover_registry_from_rows,
    recovered_columns_sql,
    refresh_sql,
    settings,
    sweep,
    values_relation_sql,
    watermark_sql,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture()
def sqlglot_warnings():
    logger = logging.getLogger("sqlglot")
    handler = _Capture()
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def _form(kind: str = "bigquery", **extra):
    base = {
        "kind": kind,
        "table": "analytics.events",
        "entity": "account_id",
        "event_time": "occurred_at",
        "event_time_tz": "utc",
        "event_column": "event_name",
        "event_values": ["login"],
        "measure": "total",
        "grain": "day",
        "lookback_days": 30,
        "breakdowns": [
            {
                "breakdown_column": "plan",
                "value_at": "event",
                "if_missing": "fill",
                "fill_from_event": "subscription_started",
            }
        ],
        "top_n": 8,
        "include_other": True,
    }
    if kind == "snowflake":
        base.update(write_database="ANALYTICS", write_schema="FC")
    else:
        base.update(write_project="dest-proj", write_dataset="analytics_fc")
    base.update(extra)
    return base


class _Run:
    """Records statements; answers bookmark / probe / census reads."""

    def __init__(self, *, bookmark=None, density=0.02, fail_on=None, recovered=None,
                 rows=5, rows_after=None):
        self.calls: list[str] = []
        self.bookmark = bookmark
        self.density = density
        self.fail_on = fail_on or ()
        # Bookmark reads must be able to DIFFER between calls, or a
        # stored-vs-fresh count comparison reads 5 against 5 in every test and
        # the detector can never fire - a guard that cannot fail. `rows` is
        # what the first read reports, `rows_after` every later one.
        self.rows = rows
        self.rows_after = rows if rows_after is None else rows_after
        self._bookmark_reads = 0
        # Rows for the recovery query (recovered_columns_sql): a bare
        # "SELECT fc_column, ..." with no WHERE, distinct from
        # bookmarks_sql's per-key "WHERE fc_column = ..." — checked first
        # since both select an "fc_bookmark" alias.
        self.recovered = recovered if recovered is not None else []

    def __call__(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        self.calls.append(sql)
        upper = sql.upper()
        for needle in self.fail_on:
            if needle.upper() in upper:
                raise AdapterError(f"boom on {needle}")
        if "GROUP BY" in upper and "WHERE" not in upper:
            return QueryResult(rows=list(self.recovered))
        if "FC_BOOKMARK" in upper:
            self._bookmark_reads += 1
            n = self.rows if self._bookmark_reads == 1 else self.rows_after
            if self.bookmark is None:
                return QueryResult(rows=[])
            return QueryResult(
                rows=[{"fc_event_name": "subscription_started", "fc_bookmark": self.bookmark, "fc_rows": n}]
            )
        if "FC_PRESENT" in upper:
            return QueryResult(rows=[{"fc_rows": 1000, "fc_present": int(1000 * self.density)}])
        return QueryResult(rows=[])


# ---------------------------------------------------------------- settings / keys


def test_settings_defaults_and_validation():
    cfg = settings(_form())
    assert cfg == {"mode": "auto", "drop_days": 60, "refresh_days": 7, "lookback_days": 3}
    assert settings(_form(managed_mode="off", managed_drop_days="90"))["mode"] == "off"
    with pytest.raises(ValueError, match="managed_mode"):
        settings(_form(managed_mode="sometimes"))
    with pytest.raises(ValueError, match="at least 1"):
        settings(_form(managed_drop_days=0))
    with pytest.raises(ValueError, match="whole number"):
        settings(_form(managed_lookback_days="soon"))


def test_column_key_is_the_name_or_a_hash():
    assert column_key("plan", "plan") == "plan"
    key = column_key("LOWER(plan)", "plan")
    assert key.startswith("expr_") and len(key) == 15
    assert column_key("LOWER(plan)", "plan") == key  # stable


def test_index_table_needs_a_destination():
    assert index_table(_form(write_project="", write_dataset="")) is None
    assert index_table(_form()) is not None
    assert "fc_column_index" in index_table(_form())


def test_expensive_columns_skip_each_event_keep_null():
    cheap = _form(breakdowns=[{"breakdown_column": "plan", "value_at": "event", "if_missing": "null"}])
    assert expensive_columns(cheap) == []
    cols = expensive_columns(_form())
    assert [c.key for c in cols] == ["plan"]
    assert cols[0].fill == "names" and cols[0].names == ["subscription_started"]
    sql_fill = _form(breakdowns=[{"breakdown_column": "plan", "value_at": "latest_record", "fill_from_expr": "amount > 0"}])
    assert expensive_columns(sql_fill)[0].fill == "expr"


# ---------------------------------------------------------------- SQL, every adapter


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_index_sql_emits_without_warnings(kind, sqlglot_warnings):
    form = _form(kind)
    stmts = [
        ensure_table_sql(form),
        backfill_sql(form, "plan", "plan"),
        backfill_sql(form, "plan", "plan", names=["subscription_started"]),
        bookmarks_sql(form, "plan"),
        refresh_sql(
            form, "plan", "plan",
            bookmarks={"subscription_started": NOW.date(), None: NOW.date()},
            lookback_days=3,
        ),
        delete_column_sql(form, "plan"),
        delete_column_sql(form, "plan", names=["signup"]),
        drop_table_sql(form),
        recovered_columns_sql(form),
        density_probe_sql(form, "plan", today=NOW.date()),
    ]
    assert sqlglot_warnings.messages == [], sqlglot_warnings.messages
    up = [s.upper() for s in stmts]
    assert up[0].startswith("CREATE TABLE IF NOT EXISTS")
    assert "FC_COLUMN_INDEX" in up[0]
    if kind == "bigquery":
        assert "PARTITION BY DATE(FC_AT)" in up[0]
        assert "CLUSTER BY FC_COLUMN, FC_ENTITY" in up[0]
    else:
        # No CLUSTER BY on Snowflake: a clustering key switches on Automatic
        # Clustering, a standing credit charge the caller never asked for.
        assert "CLUSTER BY" not in up[0]
    assert up[1].startswith("INSERT INTO") and "IS NULL" in up[1]  # sqlglot spells NOT (x) IS NULL
    assert "SUBSCRIPTION_STARTED" in up[2]
    assert "MAX(FC_AT)" in up[3]
    assert "NOT EXISTS" in up[4]
    assert up[5].startswith("DELETE FROM")
    assert up[7].startswith("DROP TABLE IF EXISTS")
    # the recovery query: no comment mechanism left, so no per-kind DDL split
    assert "FC_COLUMN" in up[8] and "GROUP BY" in up[8] and "WHERE" not in up[8]


def test_refresh_bound_is_on_the_stored_column_not_a_cast():
    """Partition pruning: the tail bound compares the bare time column."""
    sql = refresh_sql(
        _form(), "plan", "plan", bookmarks={"login": NOW.date()}, lookback_days=3
    )
    import re as _re
    assert _re.search(r"occurred_at\s*\)?\s*>=", sql), sql
    # the lookback moved the floor three days back
    assert "2026-08-30" in sql
    # the bound never wraps the stored column: no CAST(occurred_at ...) >= ...
    assert not _re.search(r"CAST\(occurred_at AS \w+\)\s*\)?\s*>=", sql)


def test_values_relation_bakes_the_narrowing_and_watermark_is_a_day_floor():
    rel = values_relation_sql(_form(), "plan", ["subscription_started"])
    assert rel.startswith("(SELECT fc_entity, fc_at AS fc_t, fc_value FROM")
    assert "fc_column = 'plan'" in rel and "'subscription_started'" in rel
    assert rel.rstrip().endswith("AS fc_idx_plan")
    # the default call carries no lookback, and the tail still keeps its
    # one-day cushion (see test_the_tail_starts_a_lookback_before_the_bookmark)
    wm = watermark_sql(_form(), datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc))
    assert "'2026-08-31'" in wm and "factcat_ts_at_date" in wm


# ---------------------------------------------------------------- registry


# ---------------------------------------------------------------- the ladder


def _registry(form, **entry):
    base = {
        "expr": "plan", "label": "plan",
        "built_at": (NOW - timedelta(days=1)).isoformat(),
        "refreshed_at": (NOW - timedelta(days=1)).isoformat(),
        "last_used_at": (NOW - timedelta(hours=2)).isoformat(),
        "bookmark": (NOW - timedelta(days=1)).isoformat(),
        "use_count": 3, "pinned": False, "overrides": {},
    }
    base.update(entry)
    return {"v": 1, "fp": config_fingerprint(form), "columns": {"plan": base}, "probes": {}}


def test_plan_no_destination_is_live():
    plan = build_plan(_form(write_project="", write_dataset=""), None, now=NOW)
    assert [c.action for c in plan.columns] == ["live"]


def test_plan_estimate_path_never_runs_a_probe():
    """No run callable, no cached probe: the column stays live for the
    estimate and nothing is billed. Mutation: probe anyway → TypeError."""
    plan = build_plan(_form(), None, now=NOW)
    assert plan.columns[0].action == "live"
    assert plan.columns[0].reason == "not yet checked"


def test_plan_run_path_probes_and_builds_when_sparse():
    run = _Run(density=0.02)
    plan = build_plan(_form(), run, now=NOW, allow_probe=True)
    assert plan.columns[0].action == "build"
    assert any("fc_present" in c for c in run.calls)
    # the probe is cached in the registry mirror
    assert plan.registry["probes"]["plan"]["density"] == pytest.approx(0.02)


def test_plan_dense_column_stays_live():
    run = _Run(density=0.9)
    plan = build_plan(_form(), run, now=NOW, allow_probe=True)
    assert plan.columns[0].action == "live"
    assert "most rows" in plan.columns[0].reason


def test_plan_gates_mode_off_and_denied_rights():
    off = build_plan(_form(managed_mode="off"), _Run(), now=NOW, allow_probe=True)
    assert off.columns[0].reason == "automatic indexing is off"
    denied = build_plan(_form(write_access_status="denied"), _Run(), now=NOW, allow_probe=True)
    assert "create rights" in denied.columns[0].reason


def test_plan_attaches_a_fresh_index_and_refreshes_a_stale_one():
    form = _form()
    fresh = build_plan({**form, "managed_tables": _registry(form)}, None, now=NOW)
    assert fresh.columns[0].action == "attach"
    att = fresh.attachment(form, "plan", "plan", "names", ["subscription_started"])
    assert att is not None and "fc_column = 'plan'" in att[0] and att[1]
    assert fresh.attachment(form, "plan", "plan", "expr", None) is None
    stale_entry = _registry(form, refreshed_at=(NOW - timedelta(days=9)).isoformat())
    # estimate path (run None) still attaches — freshness is a cost knob
    est = build_plan({**form, "managed_tables": stale_entry}, None, now=NOW)
    assert est.columns[0].action == "attach"
    # run path refreshes first
    plan = build_plan({**form, "managed_tables": stale_entry}, _Run(), now=NOW)
    assert plan.columns[0].action == "refresh"


def test_plan_rebuilds_when_the_config_fingerprint_moved():
    form = _form()
    reg = _registry(form)
    reg["fp"] = {**reg["fp"], "entity": "other_id"}
    plan = build_plan({**form, "managed_tables": reg}, _Run(), now=NOW, allow_probe=True)
    assert plan.columns[0].action == "rebuild"
    assert "mapping changed" in plan.columns[0].reason


def test_plan_event_time_column_is_the_stored_column():
    plan = build_plan(_form(), None, now=NOW)
    assert plan.event_time_column == "occurred_at"


# ---------------------------------------------------------------- apply


def test_apply_build_runs_ensure_clear_backfill_bookmarks_in_order():
    """A build clears its column before backfilling: rows can outlive their
    registry entry, and appending onto them doubles the table. No comment
    write follows any more - the mirror is what `persist` gets,
    covered by test_apply_plan_persists_after_each_column_not_batched."""
    form = _form()
    run = _Run(bookmark=NOW - timedelta(hours=3))
    plan = build_plan(form, _Run(density=0.01), now=NOW, allow_probe=True)
    registry = apply_plan(plan, form, run, now=NOW)
    ups = [c.upper() for c in run.calls]
    assert ups[0].startswith("CREATE TABLE IF NOT EXISTS")
    assert ups[1].startswith("DELETE FROM")
    assert ups[2].startswith("INSERT INTO")
    assert "MAX(FC_AT)" in ups[3]
    assert len(ups) == 4, "nothing should run after the bookmark read any more"
    entry = registry["columns"]["plan"]
    assert entry["bookmark"] and entry["built_at"] and entry["expr"] == "plan"
    assert plan.columns[0].action == "attach"
    assert plan.failures == []


def test_apply_plan_persists_after_each_column_not_batched():
    """`persist` is called once PER column, right after its own rows land -
    not once at the end of the whole plan. This is the actual fix for item
    44: a real backfill that finished must be remembered even if the
    process dies before a LATER column in the same plan finishes.

    Mutation: move the `persist(registry)` call outside the for-loop.
    """
    form = _form(breakdowns=[
        {"breakdown_column": "plan", "value_at": "event", "if_missing": "fill", "fill_from_event": "subscription_started"},
        {"breakdown_column": "tier", "value_at": "event", "if_missing": "fill", "fill_from_event": "subscription_started"},
    ])
    run = _Run(bookmark=NOW - timedelta(hours=3))
    plan = build_plan(form, _Run(density=0.01), now=NOW, allow_probe=True)
    assert len(plan.builds()) == 2, "the guard needs two columns to prove ordering"
    seen: list[dict] = []
    apply_plan(plan, form, run, persist=lambda reg: seen.append(copy.deepcopy(reg)), now=NOW)
    assert len(seen) == 2, "persist must fire once per column, not once for the whole plan"
    # after the FIRST persist call, only the first column is recorded yet -
    # proving persistence happens inside the loop, not after it
    assert "plan" in seen[0]["columns"] and "tier" not in seen[0]["columns"]
    assert "plan" in seen[1]["columns"] and "tier" in seen[1]["columns"]


def test_apply_failure_turns_the_column_live_and_reports():
    form = _form()
    plan = build_plan(form, _Run(density=0.01), now=NOW, allow_probe=True)
    run = _Run(fail_on=["INSERT INTO"])
    apply_plan(plan, form, run, now=NOW)
    assert plan.columns[0].action == "live"
    assert plan.failures and "plan" in plan.failures[0]
    note = failure_note(plan)
    assert note.startswith("Could not index `plan`")
    assert "chart is correct" in note


def test_apply_refresh_reads_bookmarks_then_inserts_the_tail():
    form = _form()
    reg = _registry(form, refreshed_at=(NOW - timedelta(days=9)).isoformat())
    plan = build_plan({**form, "managed_tables": reg}, _Run(), now=NOW)
    run = _Run(bookmark=NOW - timedelta(days=9))
    registry = apply_plan(plan, {**form, "managed_tables": reg}, run, now=NOW)
    ups = [c.upper() for c in run.calls]
    assert ups[0].startswith("CREATE TABLE IF NOT EXISTS")  # idempotent
    assert "MAX(FC_AT)" in ups[1]
    assert ups[2].startswith("INSERT INTO") and "NOT EXISTS" in ups[2]
    assert registry["columns"]["plan"]["refreshed_at"] == NOW.replace(microsecond=0).isoformat()


def test_bump_usage_is_hourly():
    """Pure in-memory now (the description write is gone): the
    caller persists the mirror the same way every other path does."""
    form = _form()
    reg = _registry(form, last_used_at=(NOW - timedelta(minutes=10)).isoformat())
    plan = build_plan({**form, "managed_tables": reg}, None, now=NOW)
    stamp = reg["columns"]["plan"]["last_used_at"]
    bump_usage(plan, now=NOW)
    assert plan.registry["columns"]["plan"]["use_count"] == 4
    assert plan.registry["columns"]["plan"]["last_used_at"] == stamp  # within the hour: count only
    reg2 = _registry(form, last_used_at=(NOW - timedelta(hours=3)).isoformat())
    plan2 = build_plan({**form, "managed_tables": reg2}, None, now=NOW)
    bump_usage(plan2, now=NOW)
    assert plan2.registry["columns"]["plan"]["last_used_at"] == NOW.replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- sweep


def test_sweep_drops_unused_unpinned_and_respects_the_daily_clock():
    form = _form()
    # `plan` is what this chart asks for, so the demand guard keeps it however
    # stale it looks. `utm` is not on the chart and is past the TTL, so it
    # goes - and it goes despite `pinned`, because pins are withdrawn and use
    # is the only thing that keeps a column.
    reg = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    reg["columns"]["utm"] = {**reg["columns"]["plan"], "label": "utm", "expr": "utm",
                             "last_used_at": (NOW - timedelta(days=61)).isoformat(), "pinned": True}
    run = _Run()
    registry, dropped, ran = sweep({**form, "managed_tables": reg}, run, now=NOW)
    assert ran and dropped == ["utm"]
    assert "plan" in registry["columns"] and "utm" not in registry["columns"]
    assert any(c.upper().startswith("DELETE FROM") for c in run.calls)
    # swept an hour ago: nothing runs
    run2 = _Run()
    _r, dropped2, ran2 = sweep(
        {**form, "managed_tables": reg, "managed_last_sweep": (NOW - timedelta(hours=1)).isoformat()},
        run2, now=NOW,
    )
    assert not ran2 and dropped2 == [] and run2.calls == []


def test_sweep_drops_the_table_when_the_last_column_goes():
    form = _form()
    # a column this chart does not ask for, so the demand guard does not hold it
    reg = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    reg["columns"] = {"utm": {**reg["columns"]["plan"], "label": "utm", "expr": "utm"}}
    run = _Run()
    sweep({**form, "managed_tables": reg}, run, now=NOW)
    assert any(c.upper().startswith("DROP TABLE IF EXISTS") for c in run.calls)


def test_sweep_persists_once_per_dropped_column_not_batched():
    """persist() fires once per dropped column, right away - not batched
    behind the rest of the sweep, and not left to the caller's
    end-of-request save, which a chart query failing first (a cap
    rejection) can skip.
    Mutation: call persist() once at the end instead of per column."""
    form = _form()
    # neither column is on this chart, so both are sweepable
    reg = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    reg["columns"] = {
        "utm": {**reg["columns"]["plan"], "label": "utm", "expr": "utm"},
        "src": {**reg["columns"]["plan"], "label": "src", "expr": "src"},
    }
    run = _Run()
    seen: list[dict] = []
    sweep({**form, "managed_tables": reg}, run, persist=lambda r: seen.append(copy.deepcopy(r)), now=NOW)
    assert len(seen) == 2, "persist must fire once per dropped column"
    assert seen[0]["columns"] == {} or "plan" not in seen[0]["columns"] or "utm" not in seen[0]["columns"]
    assert seen[-1]["columns"] == {}


def test_sweep_persists_before_the_delete_runs():
    """Record first, rows second - the same ordering `apply_action`'s drop
    keeps, and for the identical reason: the other order leaves the mirror
    claiming a bookmark for rows already gone if the process dies between
    a successful DELETE and the local write, and a later run attaches to
    an empty column and reads only the live tail - silently wrong, no
    error. An earlier version of this fix persisted AFTER the DELETE
    instead.
    Mutation: call persist() after run(), not before."""
    form = _form()
    reg = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    # `utm` is not on the chart so it sweeps; `plan` is, so it stays and the
    # remaining-columns branch (DELETE, not DROP TABLE) is the one exercised
    reg["columns"]["utm"] = {**reg["columns"]["plan"], "label": "utm", "expr": "utm"}
    order: list[str] = []
    run = _Run(fail_on=("DELETE FROM",))
    registry, dropped, ran = sweep(
        {**form, "managed_tables": reg}, run,
        persist=lambda r: order.append("persist"), now=NOW,
    )
    # the DELETE fails (not missing-relation), so run() raises nothing here
    # (sweep swallows it) but never gets the chance to append to `order`
    assert order == ["persist"], "the mirror must be written before the DELETE, not after"
    assert dropped == ["utm"], "the mirror already says the column is gone, rows or not"


# ---------------------------------------------------------------- copy register


def test_events_notes_say_indexed_and_never_relation_or_watermark():
    """One short line after a run: past tense, the owner's word "Indexed",
    what later runs cost; nothing before the run (the chip already includes
    the build). "relation" / "watermark" are mechanism words and stay out."""
    form = _form()
    plan = Plan([], None, None, {}, settings(form))
    plan.built = ["plan"]
    note = built_note(plan, bytes_after=131 * 1024 ** 2)
    assert note == "Indexed `plan` \u00b7 later runs ~ 131 MB"
    assert built_note(plan) == "Indexed `plan` \u00b7 later runs read less"
    assert built_note(Plan([], None, None, {}, settings(form))) == ""
    plan.failures = ["plan: Access Denied."]
    fail = failure_note(plan)
    assert fail.startswith("Could not index `plan`: Access Denied.")
    for word in ("relation", "watermark"):
        assert word not in (note + fail).lower()
    assert not hasattr(managed, "notes_for") and not hasattr(managed, "maybe_note")


def test_fingerprint_includes_the_write_destination():
    """A new destination is a new, empty table: the mirror's bookmarks must
    not attach to it. Mutation: drop 'dest' from config_fingerprint."""
    form = _form()
    moved = _form(write_dataset="analytics_fc_v2")
    assert config_fingerprint(form) != config_fingerprint(moved)
    reg = _registry(form)
    plan = build_plan({**moved, "managed_tables": reg}, _Run(), now=NOW, allow_probe=True)
    assert plan.columns[0].action == "rebuild"


def test_refresh_folds_in_rows_with_no_event_name():
    """Rows whose event name is NULL have no name bookmark; they must still
    fold in (on the NULL bookmark, else the overall floor)."""
    sql = refresh_sql(
        _form(), "plan", "plan",
        bookmarks={"login": NOW.date()}, lookback_days=3,
    )
    import re as _re
    # the OR branch, not the anti-join's NULL-equality clause (which also
    # says "event_name IS NULL"): NULL-named rows bounded by the floor
    branch = _re.compile(r"event_name IS NULL\s+AND\s+\(?\s*occurred_at\s*\)?\s*>=")
    assert branch.search(sql), sql
    with_null = refresh_sql(
        _form(), "plan", "plan",
        bookmarks={"login": NOW.date(), None: (NOW - timedelta(days=10)).date()}, lookback_days=0,
    )
    assert branch.search(with_null) and "2026-08-23" in with_null


def test_backfill_select_is_the_insert_without_the_insert():
    sel = managed.backfill_select_sql(_form(), "plan", "plan")
    ins = backfill_sql(_form(), "plan", "plan")
    assert not sel.upper().startswith("INSERT")
    assert sel.strip() in ins


def test_reconcile_trusts_the_mirror_and_forgets_a_table_dropped_out_of_band(monkeypatch):
    """The mirror is the record now; the warehouse is asked only
    to catch a table gone out of band, via one metadata call - never a
    full read of anything. Mutation: return the mirror unconditionally
    without the existence check, or check the WRONG table."""
    form = _form()
    mirror = _registry(form)
    calls = {"n": 0}

    def missing(f, t):
        raise AdapterError("Not found: Table")

    def exists(f, t):
        calls["n"] += 1
        return {"bytes": 1}

    monkeypatch.setattr(managed, "_stats", missing)
    out = managed.reconcile_registry(
        {**form, "managed_tables": {**mirror, "probes": {"plan": {"density": 0.01, "at": NOW.isoformat()}}}},
        _Run(),
    )
    assert "columns" not in out and out["probes"]["plan"]["density"] == 0.01

    monkeypatch.setattr(managed, "_stats", exists)
    out2 = managed.reconcile_registry({**form, "managed_tables": mirror}, _Run())
    assert out2["columns"]["plan"]["bookmark"] == mirror["columns"]["plan"]["bookmark"]
    assert calls["n"] == 1, "the existence check is the only warehouse read on a healthy mirror"


def test_reconcile_recovers_only_on_a_genuinely_empty_mirror(monkeypatch):
    """One trigger recovers, one resets clean. An empty mirror with a
    destination already populated (a fresh install, or `.factcat.json`
    lost) re-derives from `fc_column_index`'s own rows - the bug this item
    exists to fix. A destination that just changed under a POPULATED
    mirror does not: the mirror's columns describe a different physical
    table, and physical rows sitting at the new destination were not
    necessarily built under THIS entity/table mapping either - column-key
    matching alone cannot tell. Start
    clean there and let the ordinary build path re-index safely.
    Mutation: recover on a destination change too, or skip recovery on a
    genuinely empty mirror."""
    monkeypatch.setattr(managed, "_stats", lambda f, t: {"bytes": 1})
    form = _form()
    run = _Run(recovered=[{
        "fc_column": "plan",
        "fc_first_at": NOW - timedelta(days=40),
        "fc_bookmark": NOW - timedelta(days=1),
    }])
    # empty mirror, destination set and populated → recovers, with real
    # timestamps (not None) so it becomes refresh-eligible again
    out = managed.reconcile_registry(form, run)
    entry = out["columns"]["plan"]
    assert entry["expr"] == "plan"
    assert entry["pinned"] is False and entry["use_count"] == 0
    assert entry["built_at"] == now_iso(NOW - timedelta(days=40))
    assert entry["refreshed_at"] == entry["bookmark"] == now_iso(NOW - timedelta(days=1))
    # populated mirror, but the destination just changed → reset clean,
    # not recovered
    moved = _form(write_dataset="analytics_fc_v2")
    mirror = _registry(form)  # fp.dest is the OLD destination
    out2 = managed.reconcile_registry({**moved, "managed_tables": mirror}, run)
    assert "columns" not in out2


def test_a_recovered_column_with_old_history_refreshes_not_attaches_blind(monkeypatch):
    """A recovered entry's `refreshed_at` reflects the rows' REAL history
    (`MIN`/`MAX(fc_at)`), not the moment it was discovered - so build_plan
    sees genuinely stale data and proposes a refresh, not a silent
    forever-attach. Without real timestamps a recovered column would never
    become refresh-eligible again.
    Mutation: stamp `now` as built_at/refreshed_at instead of the derived
    timestamps."""
    monkeypatch.setattr(managed, "_stats", lambda f, t: {"bytes": 1})
    form = _form()
    run = _Run(recovered=[{
        "fc_column": "plan",
        "fc_first_at": NOW - timedelta(days=40),
        "fc_bookmark": NOW - timedelta(days=9),
    }])
    recovered, _stats_ = recover_registry_from_rows(form, run, now=NOW)
    plan = build_plan({**form, "managed_tables": recovered}, run, now=NOW)
    assert plan.columns[0].action == "refresh"
    assert "older than the staleness target" in plan.columns[0].reason


def test_the_sweep_does_not_evict_what_recovery_just_rescued(monkeypatch):
    """The whole point of recovery is saving an expensive backfill, and it
    fed that backfill straight to the reaper. `built_at` on a recovered
    entry is the OLDEST event in the index - routinely years back - and the
    sweep evicts on `last_used_at or built_at`. With `last_used_at` unset,
    the very next Run after a recovery dropped the column and then the
    table: 61M rows rescued and destroyed in two requests.

    A recovered column was found and used this second, which is what
    `last_used_at` means. Mutation: set it back to None.
    """
    monkeypatch.setattr(managed, "_stats", lambda f, t: {"bytes": 1})
    form = _form()
    run = _Run(recovered=[{
        "fc_column": "plan",
        "fc_first_at": NOW - timedelta(days=700),   # older than any TTL
        "fc_bookmark": NOW - timedelta(days=1),
    }])
    recovered, _stats_ = recover_registry_from_rows(form, run, now=NOW)
    assert recovered["columns"]["plan"]["built_at"] == now_iso(NOW - timedelta(days=700))

    swept, dropped, ran = sweep({**form, "managed_tables": recovered}, _Run(), now=NOW)
    assert ran and dropped == [], "the sweep evicted a column recovery had just rescued"
    assert "plan" in swept["columns"]


def test_recovery_never_guesses_an_unmatched_columns_expression(monkeypatch):
    """A row for a key the CURRENT mapping does not produce - an old
    computed expression, or a column no longer charted - must be left out
    of the recovered registry rather than attached with the raw key as a
    guessed expr: a wrong expr would silently write bad values into the
    next refresh. Unindexed-and-rebuilt is safe; that is not.
    Mutation: fall back to the raw key as expr/label for an unmatched row."""
    monkeypatch.setattr(managed, "_stats", lambda f, t: {"bytes": 1})
    form = _form()
    run = _Run(recovered=[
        {"fc_column": "plan", "fc_bookmark": NOW - timedelta(days=1), "fc_rows": 9},
        {"fc_column": "expr_deadbeef00", "fc_bookmark": NOW - timedelta(days=1), "fc_rows": 3},
    ])
    registry, stats = recover_registry_from_rows(form, run)
    assert set(registry["columns"]) == {"plan"}
    assert stats is not None


def test_recovery_reports_missing_when_the_table_does_not_exist(monkeypatch):
    form = _form()

    def missing(f, t):
        raise AdapterError("Not found: Table")

    monkeypatch.setattr(managed, "_stats", missing)
    registry, stats = recover_registry_from_rows(form, _Run())
    assert registry == {} and stats is None


def test_recovery_skips_the_scan_when_nothing_current_could_ever_match(monkeypatch):
    """The current mapping has no expensive columns at all (Value at reset
    to "each event", or no breakdown configured) - nothing a recovered row
    could ever match, so the WHERE-less scan of the whole table must not
    run for nothing on every render. Mutation: run the scan regardless."""
    monkeypatch.setattr(managed, "_stats", lambda f, t: {"bytes": 1})
    cheap = _form(breakdowns=[{"breakdown_column": "plan", "value_at": "event", "if_missing": "null"}])
    run = _Run(recovered=[{"fc_column": "plan", "fc_first_at": NOW, "fc_bookmark": NOW}])
    registry, stats = recover_registry_from_rows(cheap, run)
    assert registry == {} and stats == {"bytes": 1}
    assert run.calls == [], "the recovery query ran despite nothing that could match"


def test_planning_never_mutates_the_shared_config_defaults():
    """A probing plan built from a config that lacks managed_tables must not
    write its probe into config.DEFAULTS (shallow-copy leak: the next fresh
    config would inherit it). Mutation: return the caller's dict from
    registry_from_form, or shallow-copy DEFAULTS in config.load."""
    from factcat_app import config

    before = copy.deepcopy(config.DEFAULTS["managed_tables"])
    form = {**_form(), "managed_tables": config.DEFAULTS["managed_tables"]}
    plan = build_plan(form, _Run(density=0.01), now=NOW, allow_probe=True)
    assert plan.columns[0].action == "build"
    assert plan.registry["probes"]["plan"]["density"] == pytest.approx(0.01)
    assert config.DEFAULTS["managed_tables"] == before == {}


def test_mode_off_does_not_use_the_index_and_never_writes():
    """Off is a USE toggle, not only a maintenance one: it says whether
    Factcat may read the index at all, so charts read full history and scan
    more - the honest meaning of turning indexing off. It still never
    builds, refreshes, rebuilds or drops, and the rows are kept.
    Mutation: let an entry attach when the mode is closed."""
    form = {**_form(), "managed_mode": "off"}
    stale = _registry(form, refreshed_at=(NOW - timedelta(days=30)).isoformat())
    run = _Run()
    plan = build_plan({**form, "managed_tables": stale}, run, now=NOW, allow_probe=True)
    assert plan.columns[0].action == "live"
    assert plan.columns[0].reason == "indexing is off"
    assert run.calls == []
    # and a perfectly fresh index is not used either
    fresh = build_plan({**form, "managed_tables": _registry(form)}, run, now=NOW, allow_probe=True)
    assert fresh.columns[0].action == "live"
    moved = {**stale, "fp": {"moved": True}}
    plan2 = build_plan({**form, "managed_tables": moved}, run, now=NOW, allow_probe=True)
    assert plan2.columns[0].action == "live" and "off" in plan2.columns[0].reason
    nobm = _registry(form, bookmark=None)
    plan3 = build_plan({**form, "managed_tables": nobm}, run, now=NOW, allow_probe=True)
    assert plan3.columns[0].action == "live" and "off" in plan3.columns[0].reason
    assert run.calls == []
    unused = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    run2 = _Run()
    registry, dropped, ran = sweep({**form, "managed_tables": unused}, run2, now=NOW)
    assert (ran, dropped, run2.calls) == (False, [], [])
    assert "plan" in registry["columns"]


def test_non_text_columns_stay_live_with_a_cast_hint(monkeypatch):
    """fc_value is text: an INT64 or DATE column would fail the INSERT on
    every Run. The planner leaves it live with the CAST hint and spends no
    probe on it. Expressions and unknown types pass. Mutation:
    is_text_column -> True."""
    form = _form(columns=[{"name": "plan", "type": "INT64"}, {"name": "occurred_at", "type": "TIMESTAMP"}, {"name": "tier", "type": "STRING"}])
    run = _Run(density=0.01)
    plan = build_plan(form, run, now=NOW, allow_probe=True)
    assert plan.columns[0].action == "live"
    assert plan.columns[0].reason == managed.NON_TEXT
    assert not any("FC_PRESENT" in c.upper() for c in run.calls)
    assert managed.is_text_column(form, "tier")
    assert managed.is_text_column(_form(), "plan")  # unmapped: the caller knows
    assert managed.is_text_column(form, "CAST(plan AS STRING)")
    # Setup no longer offers a manual index: drop is the only action
    with pytest.raises(ValueError, match="action must be drop"):
        managed.apply_action(form, run, action="index", key="plan")


def test_builds_are_automatic_without_a_dry_run_too():
    """Mode: Automatic is the consent on every warehouse. Snowflake has no
    cost preview, but neither does the chart it runs; a build costs about
    what that chart scans. Mutation: gate builds on a dry-run capability."""
    plan = build_plan(_form("snowflake"), _Run(density=0.01), now=NOW, allow_probe=True)
    assert plan.columns[0].action == "build"
    assert "auto_ok" not in build_plan.__code__.co_varnames


def test_the_tail_starts_a_lookback_before_the_bookmark():
    """The refresh floors subtract the late-arrival lookback; the tail must
    subtract it too, or a row that lands after the scan with an event time
    just before the bookmark is in neither side. Mutation: drop the
    subtraction in watermark_sql."""
    form = _form(managed_lookback_days=3)
    bookmark = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)
    assert "2026-09-07" in managed.watermark_sql(form, bookmark, 3)
    # never zero cushion: the day floor is computed in the reporting timezone,
    # which can sit hours after the bookmark, so a caller who sets the
    # lookback to 0 still gets a day of overlap rather than a gap
    assert "2026-09-09" in managed.watermark_sql(form, bookmark, 0)
    # and the plan hands the setting through, not a zero
    reg = _registry(form, bookmark=bookmark.isoformat())
    plan = build_plan({**form, "managed_tables": reg}, None, now=NOW)
    att = plan.attachment(form, "plan", "plan", "any", None)
    assert att is not None and "2026-09-07" in att[1]  # the setting, not a zero


def test_a_mapping_change_drops_the_whole_index():
    """A chart rebuilds only the columns it uses. Resetting the registry
    without dropping the table would leave every other column's rows behind
    with no entry - unswept, unlistable, and unioned as a second generation
    of fc_at the next time that column is charted. Mutation: reset the
    registry without the drop."""
    form = _form()
    reg = _registry(form)
    reg["columns"]["utm"] = {**reg["columns"]["plan"], "label": "utm", "expr": "utm"}
    reg["fp"] = {"moved": True}
    run = _Run(density=0.01)
    plan = build_plan({**form, "managed_tables": reg}, run, now=NOW, allow_probe=True)
    assert plan.columns[0].action == "rebuild"
    registry = apply_plan(plan, {**form, "managed_tables": reg}, run, now=NOW)
    ups = [c.upper().strip() for c in run.calls]
    assert any(u.startswith("DROP TABLE") for u in ups), "stale generations were left in the table"
    assert ups.index(next(u for u in ups if u.startswith("DROP TABLE"))) < ups.index(
        next(u for u in ups if u.startswith("CREATE TABLE"))
    )
    # the rebuild became a build; a build clears its column first either way
    assert ups.index(next(u for u in ups if u.startswith("DROP TABLE"))) < ups.index(
        next(u for u in ups if u.startswith("INSERT"))
    )
    assert set(registry["columns"]) == {"plan"}


def test_a_stale_generation_drop_failure_is_not_reported_as_a_column(monkeypatch):
    """`plan.registry_failures` can only fire from one place now (the
    other went with the comment write): dropping the PREVIOUS index
    stale-fingerprint branch. This run's own columns are built and
    recorded regardless - only the old generation's cleanup failed - so
    "could not index `plan`" would be false. Driven through apply_plan so
    the routing is what is tested, not the field.
    Mutation: append registry failures to plan.failures."""
    form = _form()
    reg = _registry(form)
    reg["fp"] = {"moved": True}
    run = _Run(density=0.01, fail_on=("DROP TABLE",))
    monkeypatch.setattr(managed, "is_missing_relation", lambda exc: False)
    plan = build_plan({**form, "managed_tables": reg}, run, now=NOW, allow_probe=True)
    apply_plan(plan, {**form, "managed_tables": reg}, run, now=NOW)
    assert plan.registry_failures, "the drop failure was not recorded"
    assert not plan.failures, "a bookkeeping failure was filed as a column failure"
    note = failure_note(plan)
    assert note.startswith("Could not clear the previous index generation:")
    assert "This run's columns are correct and recorded" in note
    assert "index `plan`" not in note
    # this run's own column still built and is recorded, despite the old
    # generation's drop failing
    assert "plan" in plan.registry["columns"]
    # a real column failure still reads as one
    plan.failures = ["plan: boom"]
    assert failure_note(plan).startswith("Could not index `plan`: boom")


def test_the_fingerprint_carries_no_quoting():
    """The registry no longer round-trips through a table comment at all
    (see test_dialects.py for the still-live escaping guard on
    `set_relation_comment` itself, which this stopped calling). What
    remains true here: the fingerprint's `dest` is a plain dotted name,
    never a quoted ident, because it used to have to survive a comment."""
    assert '"' not in config_fingerprint(_form())["dest"]


def test_a_failed_probe_leaves_the_chart_alone():
    """The density probe is a billed query over the events table. A cap
    rejection there must turn the column live, not fail the run.
    Mutation: drop the try/except around _probe_density."""
    form = _form()
    run = _Run(density=0.01, fail_on=("fc_present",))
    plan = build_plan(form, run, now=NOW, allow_probe=True)
    assert plan.columns[0].action == "live"
    assert "could not measure" in plan.columns[0].reason


def test_an_expression_slot_is_never_labelled_none():
    """A breakdown written as SQL has no label; the key is the only name a
    person can be shown. Mutation: IndexColumn(..., label, ...)."""
    form = _form(breakdowns=[{"breakdown_expr": "JSON_VALUE(props, '$.plan')",
                              "value_at": "first_record", "if_missing": "fill",
                              "fill_from_event": "subscription_started"}])
    cols = expensive_columns(form)
    assert cols and cols[0].label
    assert cols[0].label == cols[0].key
    plan = Plan([], None, None, {}, settings(form))
    plan.built = [cols[0].label]
    assert "None" not in built_note(plan)


def test_half_configured_destinations_do_not_share_a_fingerprint():
    """Two destinations that are each missing a part must still differ: if
    they collapse to one value, old bookmarks attach to a new table.
    Mutation: drop the incomplete parts from _dest_name."""
    a = config_fingerprint(_form(write_project="proj-a", write_dataset=""))
    b = config_fingerprint(_form(write_project="proj-b", write_dataset=""))
    assert a["dest"] != b["dest"]
    c = config_fingerprint(_form("snowflake", write_database="DB1", write_schema=""))
    d = config_fingerprint(_form("snowflake", write_database="DB2", write_schema=""))
    assert c["dest"] != d["dest"]
    # and a complete destination still reads as a plain dotted name
    assert config_fingerprint(_form())["dest"] == "dest-proj.analytics_fc.fc_column_index"


def test_drop_on_a_stale_column_drops_the_table():
    """After a mapping change every column in the table is a stale
    generation, and Setup still lists them. The guides promise Drop as the
    immediate erasure remedy, so it must not refuse with "no such indexed
    column". Mutation: reset the registry without dropping the table.

    ``apply_action`` reads the mirror directly now - the
    warehouse-comment read in front of it is gone - no mock
    needed, and no live warehouse client is ever reached by this path.
    """
    form = _form()
    reg = _registry(form)
    reg["columns"]["utm"] = {**reg["columns"]["plan"], "label": "utm", "expr": "utm"}
    reg["fp"] = {"moved": True}
    run = _Run()
    out = managed.apply_action({**form, "managed_tables": reg}, run, action="drop", key="plan")
    ups = [c.upper().strip() for c in run.calls]
    assert any(u.startswith("DROP TABLE") for u in ups), "the stale rows were left behind"
    assert out["columns"] == {}
    assert out["fp"] == config_fingerprint(form)


def test_apply_action_persists_before_the_delete_runs():
    """Record first, rows second: the other order leaves the mirror
    claiming a bookmark for rows already gone when the DELETE fails, and a
    later run attaches to an empty column - silently wrong, no error.
    Mutation: call persist() after run(), not before."""
    form = _form()
    reg = _registry(form)
    reg["columns"]["utm"] = {**reg["columns"]["plan"], "label": "utm", "expr": "utm"}
    order: list[str] = []
    run = _Run(fail_on=("DELETE FROM",))
    with pytest.raises(AdapterError):
        managed.apply_action(
            {**form, "managed_tables": reg}, run, action="drop", key="plan",
            persist=lambda r: order.append("persist"),
        )
    # the DELETE raises before returning, so persist only shows up in
    # `order` at all if it ran BEFORE that call - the wrong order would
    # never reach it once run() has thrown
    assert order == ["persist"], "the mirror must be written before the DELETE, not after"


def test_a_refusal_the_caller_can_act_on_is_said_out_loud():
    """An expensive mode on a column Factcat will never index is a permanent
    silent slow path. The reason was write-only until an owner asked why the
    run row said nothing. Mutation: drop the refusal branch in pending_note."""
    form = _form(columns=[{"name": "plan", "type": "NUMERIC"}])
    plan = build_plan(form, _Run(density=0.01), now=NOW, allow_probe=True)
    assert plan.columns[0].action == "live" and plan.columns[0].reason == managed.NON_TEXT
    note = managed.pending_note(plan)
    assert note.startswith("Not indexing `plan`: the index stores text.")
    assert "CAST(plan AS STRING)" in note
    # a refusal with no remedy stays quiet: it is not the user's problem
    dense = build_plan(_form(), _Run(density=0.9), now=NOW, allow_probe=True)
    assert dense.columns[0].action == "live"
    assert managed.pending_note(dense) == ""


def test_the_sweep_does_not_evict_what_this_chart_is_asking_for():
    """The sweep runs BEFORE the plan, so it used to evict on "unused as of a
    moment ago" while the request in flight was the use: opening a chart you
    had not run for longer than the TTL dropped the column, dropped the
    table, and rebuilt it from full history in the same Run. Returning to a
    chart destroyed its index and charged a full backfill for coming back.

    Demand means servable demand: this chart asks for it, the mapping still
    matches, and there is a bookmark to attach to.

    Mutation: drop the `key in wanted` guard.
    """
    form = _form()
    reg = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    run = _Run()
    registry, dropped, ran = sweep({**form, "managed_tables": reg}, run, now=NOW)
    assert ran and dropped == [], "the sweep evicted the column the chart was asking for"
    assert "plan" in registry["columns"]
    assert run.calls == [], "nothing should have been deleted or dropped"
    # and the Run that follows uses the index it kept, rather than paying to
    # build one it had just destroyed
    plan = build_plan({**form, "managed_tables": registry}, _Run(), now=NOW)
    assert plan.columns[0].action == "attach"
    assert plan.columns[0].action != "build"


def test_the_sweep_still_evicts_a_column_no_chart_wants():
    """The demand guard must not become "never evict". A column this chart
    does not ask for, past the TTL, still goes."""
    form = _form()
    reg = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    reg["columns"] = {"utm": {**reg["columns"]["plan"], "label": "utm", "expr": "utm"}}
    _r, dropped, ran = sweep({**form, "managed_tables": reg}, _Run(), now=NOW)
    assert ran and dropped == ["utm"]


def test_a_stale_mapping_does_not_shelter_a_column_from_the_sweep():
    """Demand only counts when it is servable. If the mapping moved, the
    entry would be rebuilt rather than attached, so it earns no shelter.
    Mutation: drop the fingerprint check from `wanted`."""
    form = _form()
    reg = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    reg["fp"] = {**reg["fp"], "entity": "someone_else"}
    _r, dropped, ran = sweep({**form, "managed_tables": reg}, _Run(), now=NOW)
    assert ran and dropped == ["plan"]


def test_rows_deleted_behind_us_rebuild_instead_of_appending_a_tail():
    """A bookmark only moves when the NEWEST row changes, so comparing
    bookmarks cannot see rows removed from the middle or the old end, or one
    event name's rows removed while another's remain - the shapes that
    actually lose history. Counts can. `bookmarks_sql` has always computed
    COUNT(*) and `_read_bookmarks` always threw it away.

    A refresh here would append a tail onto a hole and answer from an index
    missing history, silently. Rebuild the column whole instead.

    Mutation: ignore row_counts, or compare bookmarks instead of counts.
    """
    form = _form()
    reg = _registry(form, refreshed_at=(NOW - timedelta(days=9)).isoformat())
    reg["columns"]["plan"]["row_counts"] = {"subscription_started": 500}
    plan = build_plan({**form, "managed_tables": reg}, _Run(), now=NOW)
    assert plan.columns[0].action == "refresh"
    # the index now reports far fewer rows than we recorded: something deleted them
    run = _Run(bookmark=NOW - timedelta(days=1), rows=9)
    registry = apply_plan(plan, {**form, "managed_tables": reg}, run, now=NOW)
    ups = [c.upper().strip() for c in run.calls]
    assert plan.repaired == ["plan"], "the column was not rebuilt"
    assert any(u.startswith("DELETE FROM") for u in ups), "a rebuild clears the column first"
    inserts = [u for u in ups if u.startswith("INSERT")]
    assert inserts and "FC_BOOKMARK" not in inserts[0], "it appended a tail instead of rebuilding whole"
    # and the fresh counts are stamped for next time
    assert registry["columns"]["plan"]["row_counts"] == {"subscription_started": 9}


def test_an_ordinary_refresh_does_not_read_as_tampering():
    """Counts GROW on a normal refresh, and Factcat's own census repair
    deletes names and re-inserts them. The stamp is always the post-write
    read, so neither can look like deletion. A false alarm here would cost a
    full-history rebuild - the exact expense this is meant to avoid.

    Mutation: stamp the counts BEFORE the writes instead of after.
    """
    form = _form()
    reg = _registry(form, refreshed_at=(NOW - timedelta(days=9)).isoformat())
    reg["columns"]["plan"]["row_counts"] = {"subscription_started": 5}
    plan = build_plan({**form, "managed_tables": reg}, _Run(), now=NOW)
    run = _Run(bookmark=NOW - timedelta(days=1), rows=5, rows_after=12)
    registry = apply_plan(plan, {**form, "managed_tables": reg}, run, now=NOW)
    assert plan.repaired == [], "an ordinary refresh was treated as tampering"
    ups = [c.upper().strip() for c in run.calls]
    assert not any(u.startswith("DELETE FROM") for u in ups), "it rebuilt a healthy column"
    assert registry["columns"]["plan"]["row_counts"] == {"subscription_started": 12}


def test_a_column_with_no_recorded_counts_is_not_treated_as_tampered():
    """Entries written before this shipped, and recovered entries, carry no
    counts. Absent is not evidence of deletion - stamp on the next write and
    say nothing. Mutation: treat a missing row_counts as zero."""
    form = _form()
    reg = _registry(form, refreshed_at=(NOW - timedelta(days=9)).isoformat())
    reg["columns"]["plan"].pop("row_counts", None)
    plan = build_plan({**form, "managed_tables": reg}, _Run(), now=NOW)
    run = _Run(bookmark=NOW - timedelta(days=1), rows=5)
    apply_plan(plan, {**form, "managed_tables": reg}, run, now=NOW)
    assert plan.repaired == []
