"""Factcat-managed tables: the column index chassis (item 12).

Mutations that must go red: skip the ensure statement before a backfill
(the build test counts statements in order); attach an index whose config
fingerprint no longer matches (the stale-config test); run a density
probe from the estimate path (the estimate-plan test forbids ``run``);
drop a pinned column on sweep; let ``notes_for`` say "index" in the
Events register.
"""

from __future__ import annotations

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
    failure_note,
    index_table,
    notes_for,
    parse_registry,
    refresh_sql,
    registry_comment,
    registry_comment_sql,
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

    def __init__(self, *, bookmark=None, density=0.02, fail_on=None):
        self.calls: list[str] = []
        self.bookmark = bookmark
        self.density = density
        self.fail_on = fail_on or ()

    def __call__(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        self.calls.append(sql)
        upper = sql.upper()
        for needle in self.fail_on:
            if needle.upper() in upper:
                raise AdapterError(f"boom on {needle}")
        if "FC_BOOKMARK" in upper:
            if self.bookmark is None:
                return QueryResult(rows=[])
            return QueryResult(
                rows=[{"fc_event_name": "subscription_started", "fc_bookmark": self.bookmark, "fc_rows": 5}]
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
    registry = {"v": 1, "fp": config_fingerprint(form), "columns": {}}
    stmts = [
        ensure_table_sql(form, registry),
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
        registry_comment_sql(form, registry),
        density_probe_sql(form, "plan", today=NOW.date()),
    ]
    assert sqlglot_warnings.messages == [], sqlglot_warnings.messages
    up = [s.upper() for s in stmts]
    assert up[0].startswith("CREATE TABLE IF NOT EXISTS")
    assert "FC_COLUMN_INDEX" in up[0]
    if kind == "bigquery":
        assert "PARTITION BY DATE(FC_AT)" in up[0]
        assert "CLUSTER BY FC_COLUMN, FC_ENTITY" in up[0]
        assert up[8].startswith("ALTER TABLE") and "DESCRIPTION" in up[8]
    else:
        assert "CLUSTER BY (FC_COLUMN, FC_ENTITY)" in up[0]
        assert up[8].startswith("COMMENT ON TABLE")
    assert up[1].startswith("INSERT INTO") and "IS NULL" in up[1]  # sqlglot spells NOT (x) IS NULL
    assert "SUBSCRIPTION_STARTED" in up[2]
    assert "MAX(FC_AT)" in up[3]
    assert "NOT EXISTS" in up[4]
    assert up[5].startswith("DELETE FROM")
    assert up[7].startswith("DROP TABLE IF EXISTS")


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
    wm = watermark_sql(_form(), datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc))
    assert "'2026-09-01'" in wm and "factcat_ts_at_date" in wm


# ---------------------------------------------------------------- registry


def test_registry_comment_trims_census_first():
    big = {
        "v": 1, "fp": {}, "columns": {
            "plan": {"expr": "plan", "names": {f"event_{i}": {"rows": i, "first": "2026-01-01T00:00:00+00:00", "last": "2026-09-01T00:00:00+00:00"} for i in range(400)}}
        }
    }
    text = registry_comment(big)
    assert len(text) <= managed.COMMENT_BUDGET + 200
    assert "names" not in json.loads(text)["columns"]["plan"]
    small = {"v": 1, "fp": {}, "columns": {"plan": {"expr": "plan", "names": {"a": {"rows": 1}}}}}
    assert "names" in json.loads(registry_comment(small))["columns"]["plan"]


def test_parse_registry_ignores_foreign_comments():
    assert parse_registry("") == {}
    assert parse_registry("a human wrote this") == {}
    assert parse_registry('{"v":1}') == {}  # no columns → not ours
    assert parse_registry('{"v":1,"columns":{}}') == {"v": 1, "columns": {}}


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


def test_apply_build_runs_ensure_backfill_bookmarks_comment_in_order():
    form = _form()
    run = _Run(bookmark=NOW - timedelta(hours=3))
    plan = build_plan(form, _Run(density=0.01), now=NOW, allow_probe=True)
    registry = apply_plan(plan, form, run, now=NOW)
    ups = [c.upper() for c in run.calls]
    assert ups[0].startswith("CREATE TABLE IF NOT EXISTS")
    assert ups[1].startswith("INSERT INTO")
    assert "MAX(FC_AT)" in ups[2]
    assert ups[3].startswith("ALTER TABLE") or ups[3].startswith("COMMENT ON")
    entry = registry["columns"]["plan"]
    assert entry["bookmark"] and entry["built_at"] and entry["expr"] == "plan"
    assert plan.columns[0].action == "attach"
    assert plan.failures == []


def test_apply_failure_turns_the_column_live_and_reports():
    form = _form()
    plan = build_plan(form, _Run(density=0.01), now=NOW, allow_probe=True)
    run = _Run(fail_on=["INSERT INTO"])
    apply_plan(plan, form, run, now=NOW)
    assert plan.columns[0].action == "live"
    assert plan.failures and "plan" in plan.failures[0]
    note = failure_note(plan)
    assert note.startswith("Could not prepare `plan`")
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
    form = _form()
    reg = _registry(form, last_used_at=(NOW - timedelta(minutes=10)).isoformat())
    plan = build_plan({**form, "managed_tables": reg}, None, now=NOW)
    run = _Run()
    bump_usage(plan, form, run, now=NOW)
    assert run.calls == []  # within the hour: count only
    assert plan.registry["columns"]["plan"]["use_count"] == 4
    reg2 = _registry(form, last_used_at=(NOW - timedelta(hours=3)).isoformat())
    plan2 = build_plan({**form, "managed_tables": reg2}, None, now=NOW)
    run2 = _Run()
    bump_usage(plan2, form, run2, now=NOW)
    assert len(run2.calls) == 1


# ---------------------------------------------------------------- sweep


def test_sweep_drops_unused_unpinned_and_respects_the_daily_clock():
    form = _form()
    reg = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    reg["columns"]["utm"] = {**reg["columns"]["plan"], "label": "utm", "pinned": True}
    run = _Run()
    registry, dropped, ran = sweep({**form, "managed_tables": reg}, run, now=NOW)
    assert ran and dropped == ["plan"]
    assert "utm" in registry["columns"] and "plan" not in registry["columns"]
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
    reg = _registry(form, last_used_at=(NOW - timedelta(days=61)).isoformat())
    run = _Run()
    sweep({**form, "managed_tables": reg}, run, now=NOW)
    assert any(c.upper().startswith("DROP TABLE IF EXISTS") for c in run.calls)


# ---------------------------------------------------------------- copy register


def test_events_notes_never_say_index():
    form = _form()
    plan = build_plan(form, _Run(density=0.01), now=NOW, allow_probe=True)
    note = notes_for(plan, bytes_build=12 * 1024**3, bytes_after=int(1.9 * 1024**3))
    assert note == "Also prepares `plan` for faster breakdowns (one-time ≈ 12 GB). Later runs ≈ 1.9 GB."
    assert "index" not in note.lower()
    assert notes_for(Plan([], None, None, {}, settings(form))) == ""
