"""Factcat-managed tables: the column index chassis (item 12).

Mutations that must go red: skip the ensure statement before a backfill
(the build test counts statements in order); attach an index whose config
fingerprint no longer matches (the stale-config test); run a density
probe from the estimate path (the estimate-plan test forbids ``run``);
drop a pinned column on sweep; let ``notes_for`` say "index" in the
Events register.
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
        # No CLUSTER BY on Snowflake: a clustering key switches on Automatic
        # Clustering, a standing credit charge the caller never asked for.
        assert "CLUSTER BY" not in up[0]
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
    # the default call carries no lookback, and the tail still keeps its
    # one-day cushion (see test_the_tail_starts_a_lookback_before_the_bookmark)
    wm = watermark_sql(_form(), datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc))
    assert "'2026-08-31'" in wm and "factcat_ts_at_date" in wm


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


def test_apply_build_runs_ensure_clear_backfill_bookmarks_comment_in_order():
    """A build clears its column before backfilling: rows can outlive their
    registry entry, and appending onto them doubles the table."""
    form = _form()
    run = _Run(bookmark=NOW - timedelta(hours=3))
    plan = build_plan(form, _Run(density=0.01), now=NOW, allow_probe=True)
    registry = apply_plan(plan, form, run, now=NOW)
    ups = [c.upper() for c in run.calls]
    assert ups[0].startswith("CREATE TABLE IF NOT EXISTS")
    assert ups[1].startswith("DELETE FROM")
    assert ups[2].startswith("INSERT INTO")
    assert "MAX(FC_AT)" in ups[3]
    assert ups[4].startswith("ALTER TABLE") or ups[4].startswith("COMMENT ON")
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
    reg["columns"]["utm"] = {**reg["columns"]["plan"], "label": "utm", "last_used_at": (NOW - timedelta(hours=3)).isoformat(), "pinned": True}
    run = _Run()
    registry, dropped, ran = sweep({**form, "managed_tables": reg}, run, now=NOW)
    assert ran and dropped == ["plan"]
    # utm stays because it was used, not because an old mirror says pinned (pins are withdrawn)
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


def test_reconcile_prefers_the_table_description_and_forgets_a_dropped_table(monkeypatch):
    form = _form()
    mirror = _registry(form)
    # table gone: only the probe cache survives
    monkeypatch.setattr(managed, "authoritative_registry", lambda f: ({}, None))
    out = managed.reconcile_registry({**form, "managed_tables": {**mirror, "probes": {"plan": {"density": 0.01, "at": NOW.isoformat()}}}})
    assert "columns" not in out and out["probes"]["plan"]["density"] == 0.01
    # table present with its own registry: the authority wins over the mirror
    authority = _registry(form, bookmark=(NOW - timedelta(days=3)).isoformat())
    monkeypatch.setattr(managed, "authoritative_registry", lambda f: (authority, {"bytes": 1}))
    out2 = managed.reconcile_registry({**form, "managed_tables": mirror})
    assert out2["columns"]["plan"]["bookmark"] == (NOW - timedelta(days=3)).isoformat()


def test_registry_comment_flags_a_dropped_census():
    big = {"v": 1, "fp": {}, "columns": {"plan": {"expr": "plan", "names": {f"e{i}": {"rows": i, "first": "2026-01-01T00:00:00+00:00", "last": "2026-09-01T00:00:00+00:00"} for i in range(400)}}}}
    assert json.loads(registry_comment(big)).get("names_dropped") is True
    small = {"v": 1, "fp": {}, "columns": {"plan": {"expr": "plan"}}}
    assert "names_dropped" not in json.loads(registry_comment(small))


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


def test_mode_off_reads_an_existing_index_as_is_and_never_writes():
    """Off is the kill-switch for every automatic write: a stale index is
    read as-is (the live tail keeps results exact), a moved mapping or a
    missing bookmark means live rather than a rebuild, and the sweep neither
    drops nor advances its clock. Mutation: gate only the build branch."""
    form = {**_form(), "managed_mode": "off"}
    stale = _registry(form, refreshed_at=(NOW - timedelta(days=30)).isoformat())
    run = _Run()
    plan = build_plan({**form, "managed_tables": stale}, run, now=NOW, allow_probe=True)
    assert plan.columns[0].action == "attach"
    assert run.calls == []
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
    monkeypatch.setattr(managed, "authoritative_registry", lambda f: ({}, None))
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


def test_a_registry_write_failure_is_not_reported_as_a_column():
    """The index IS in place when only the description write fails, so
    "could not index `registry`; this run read the full history" is false
    twice over. Driven through apply_plan so the routing is what is tested,
    not the field. Mutation: append registry failures to plan.failures."""
    form = _form()
    run = _Run(density=0.01, fail_on=("SET OPTIONS",))
    plan = build_plan(form, run, now=NOW, allow_probe=True)
    assert plan.columns[0].action == "build"
    apply_plan(plan, form, run, now=NOW)
    assert plan.registry_failures, "the comment write did not fail"
    assert not plan.failures, "a bookkeeping failure was filed as a column failure"
    note = failure_note(plan)
    assert note.startswith("Saved no record of the prepared columns:")
    assert "The chart is correct" in note
    assert "index `registry`" not in note and "read the full history" not in note
    # a real column failure still reads as one
    plan.failures = ["plan: boom"]
    assert failure_note(plan).startswith("Could not index `plan`: boom")


def test_the_registry_round_trips_through_a_table_comment():
    """End to end on the real fingerprint: the document we write is the
    document we read back. Mutation: put the quoted ident back in the
    fingerprint, or escape only the quote in _comment_literal."""
    for kind in ("bigquery", "snowflake"):
        form = _form(kind)
        registry = {"v": 1, "fp": config_fingerprint(form),
                    "columns": {"k": {"expr": "JSON_VALUE(props, '$.plan')", "label": "plan"}}}
        note = registry_comment(registry)
        stmt = managed.registry_comment_sql(form, registry)
        body = stmt[stmt.index("'") + 1 : stmt.rindex("'")]
        stored = []
        i = 0
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                stored.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"', "`": "`"}.get(body[i + 1], "\\" + body[i + 1]))
                i += 2
                continue
            stored.append(body[i])
            i += 1
        text = "".join(stored)
        assert text == note
        assert parse_registry(text).get("columns", {}).get("k", {}).get("expr") == "JSON_VALUE(props, '$.plan')"
    # and the fingerprint carries no quoting to escape in the first place
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


def test_drop_on_a_stale_column_drops_the_table(monkeypatch):
    """After a mapping change every column in the table is a stale
    generation, and Setup still lists them. The guides promise Drop as the
    immediate erasure remedy, so it must not refuse with "no such indexed
    column". Mutation: reset the registry without dropping the table.

    ``apply_action`` reads the AUTHORITATIVE registry off the table's own
    description before comparing fingerprints - real metadata, not the form's
    mirror - so this has to mock ``authoritative_registry`` like its siblings
    above, or it reaches for a live BigQuery client. It did, once: this test
    passed locally on a machine with ambient `gcloud` credentials and failed
    every CI run, which has none.
    """
    form = _form()
    reg = _registry(form)
    reg["columns"]["utm"] = {**reg["columns"]["plan"], "label": "utm", "expr": "utm"}
    reg["fp"] = {"moved": True}
    monkeypatch.setattr(managed, "authoritative_registry", lambda f: (reg, {"bytes": 1}))
    run = _Run()
    out = managed.apply_action({**form, "managed_tables": reg}, run, action="drop", key="plan")
    ups = [c.upper().strip() for c in run.calls]
    assert any(u.startswith("DROP TABLE") for u in ups), "the stale rows were left behind"
    assert out["columns"] == {}
    assert out["fp"] == config_fingerprint(form)


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
