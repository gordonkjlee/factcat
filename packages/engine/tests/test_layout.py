"""Setup layout diagnostic: prune SQL, clustering notes, relation facts."""

from __future__ import annotations

from factcat.warehouses.snowflake import _cluster_fields, _object_kind
from factcat_app.layout import (
    _cluster_note,
    _metadata_timestamp_verdict,
    assemble_layout,
    cluster_fingerprint,
    date_fingerprint,
    layout_from_form,
    prune_count_sql,
    relation_fingerprint,
)



def _form(**extra):
    base = {
        "kind": "bigquery",
        "project": "p",
        "location": "EU",
        "dataset": "analytics",
        "table_name": "events",
        "table": "p.analytics.events",
        "entity": "user_id",
        "event_time": "occurred_at",
        "event_column": "event_name",
        "event_time_tz": "utc",
        "reporting_timezone": "UTC",
        "week_start": "monday",
    }
    base.update(extra)
    return base


def test_prune_sql_isolates_timestamp_column():
    sql = prune_count_sql(_form(), window=True)
    assert "COUNT(*)" in sql.upper()
    assert "occurred_at >=" in sql.replace("`", "")
    assert "CAST(occurred_at" not in sql.replace("`", "")
    full = prune_count_sql(_form(), window=False)
    assert "WHERE" not in full.upper().split("FROM", 1)[-1]
    named = prune_count_sql(_form(), window=True, event_name="opened")
    assert "event_name = 'opened'" in named.replace("`", "")


def test_metadata_match_and_mismatch():
    rel = {
        "kind": "table",
        "partition": {"field": "occurred_at", "type": "MONTH", "ingestion": False},
        "clustering": ["event_name"],
    }
    assert _metadata_timestamp_verdict(rel, "occurred_at") == "match"
    assert _metadata_timestamp_verdict(rel, "created_at") == "mismatch"
    view = {"kind": "view", "partition": None, "bases": []}
    assert _metadata_timestamp_verdict(view, "occurred_at") is None
    sf = {"kind": "table", "partition": None, "clustering": [], "bases": []}
    assert _metadata_timestamp_verdict(sf, "occurred_at", kind="snowflake") is None
    assert _metadata_timestamp_verdict(sf, "occurred_at", kind="bigquery") == "none"


def test_cluster_note_raises_unless_spoke_view():
    table = {
        "kind": "table",
        "clustering": ["customer_id", "event_name"],
        "bases": [],
    }
    note = _cluster_note(table, "event_name", "customer_id", "bigquery")
    assert note["status"] == "not_leading"
    assert note["entity_pos"] == 1
    spokes = {
        "kind": "view",
        "clustering": [],
        "bases": [
            {"name": "purchase", "clustering": ["user_id"]},
            {"name": "login", "clustering": ["user_id"]},
            {"name": "signup", "clustering": ["user_id"]},
        ],
    }
    spoke = _cluster_note(spokes, "event_name", "user_id", "bigquery")
    assert spoke["status"] == "spokes"
    assert spoke["entity_pos"] == 1
    hub = {
        "kind": "view",
        "clustering": [],
        "bases": [
            {
                "name": "int_events",
                "clustering": ["event_name", "user_id"],
            },
            {"name": "identity", "clustering": []},
        ],
    }
    hub_note = _cluster_note(hub, "event_name", "user_id", "bigquery")
    assert hub_note["status"] == "ok"
    assert hub_note["entity_pos"] == 2


def test_snowflake_no_key_is_not_a_failure():
    rel = {
        "kind": "table",
        "clustering": [],
        "automatic_clustering": False,
        "bases": [],
    }
    note = _cluster_note(rel, "event_name", "user_id", "snowflake")
    assert note["status"] == "no_key"


def test_cluster_note_flags_missing_entity_on_bigquery():
    rel = {
        "kind": "table",
        "clustering": ["event_name", "country"],
        "bases": [],
    }
    note = _cluster_note(rel, "event_name", "user_id", "bigquery")
    assert note["status"] == "ok"
    assert note["entity_pos"] == 0
    sf = _cluster_note(rel, "event_name", "user_id", "snowflake")
    assert sf["status"] == "ok"
    assert sf["entity_pos"] == 0


def test_layout_skips_partition_avg_until_asked(monkeypatch):
    called = []
    monkeypatch.setattr(
        "factcat_app.layout._partition_avg",
        lambda *a, **k: called.append(True) or 99,
    )
    monkeypatch.setattr(
        "factcat_app.layout.columns_from_form",
        lambda form: {
            "location": "EU",
            "columns": [],
            "relation": {
                "name": "events",
                "kind": "table",
                "partition": {
                    "field": "occurred_at",
                    "type": "MONTH",
                    "ingestion": False,
                },
                "clustering": ["event_name", "user_id"],
            },
        },
    )
    out = layout_from_form(_form())
    assert called == []
    assert out["partition_avg_bytes"] is None
    assert out["cluster"]["entity_pos"] == 2
    out_avg = layout_from_form(_form(include_partition_avg=True))
    assert called == [True]
    assert out_avg["partition_avg_bytes"] == 99


def test_layout_uses_columns_location_when_form_omits_it(monkeypatch):
    seen = {}

    def fake_enrich(form, relation):
        seen["location"] = form.get("location")
        return {**relation, "bases": []}

    monkeypatch.setattr("factcat_app.layout._enrich_bases", fake_enrich)
    monkeypatch.setattr(
        "factcat_app.layout._probe_bq",
        lambda form, rel: {"status": "ok", "verdict": "unknown"},
    )
    monkeypatch.setattr(
        "factcat_app.layout._probe_cluster_bq",
        lambda *a, **k: {"status": "skipped", "verdict": None},
    )
    monkeypatch.setattr(
        "factcat_app.layout.columns_from_form",
        lambda form: {
            "location": "EU",
            "columns": [],
            "relation": {
                "name": "events",
                "kind": "view",
                "partition": None,
                "clustering": [],
            },
        },
    )
    layout_from_form(_form(location=""))
    assert seen["location"] == "EU"


def test_cluster_probe_when_view_metadata_is_silent(monkeypatch):
    def fake_scan(*, sql, **kw):
        compact = sql.replace("`", "")
        named = "event_name = 'opened'" in compact
        return {
            "bytes_processed": (5 if named else 200) * 1024**2,
            "referenced_tables": [],
        }

    monkeypatch.setattr("factcat_app.layout.dry_run_scan", fake_scan)
    monkeypatch.setattr(
        "factcat_app.layout._enrich_bases",
        lambda form, rel: {**rel, "bases": []},
    )
    monkeypatch.setattr(
        "factcat_app.layout.columns_from_form",
        lambda form: {
            "location": "EU",
            "columns": [],
            "relation": {
                "name": "events",
                "kind": "view",
                "partition": None,
                "clustering": [],
            },
        },
    )
    out = layout_from_form(_form(event_names=["opened"]))
    assert out["cluster"]["status"] == "prunes"


def test_cluster_probe_skipped_when_metadata_has_keys(monkeypatch):
    called = []
    monkeypatch.setattr(
        "factcat_app.layout.dry_run_scan",
        lambda **kw: called.append(True) or {"bytes_processed": 1, "referenced_tables": []},
    )
    monkeypatch.setattr(
        "factcat_app.layout.columns_from_form",
        lambda form: {
            "location": "EU",
            "columns": [],
            "relation": {
                "name": "events",
                "kind": "table",
                "partition": {
                    "field": "occurred_at",
                    "type": "MONTH",
                    "ingestion": False,
                },
                "clustering": ["event_name", "user_id"],
            },
        },
    )
    out = layout_from_form(_form(event_names=["opened"]))
    assert called == []
    assert out["cluster"]["status"] == "ok"


def test_layout_fingerprints_split_by_check():
    form = _form()
    assert relation_fingerprint(form)["table"] == "p.analytics.events"
    assert "event_time" not in relation_fingerprint(form)
    assert date_fingerprint(form)["event_time"] == "occurred_at"
    assert "entity" not in date_fingerprint(form)
    assert cluster_fingerprint(form)["entity"] == "user_id"
    assert cluster_fingerprint(_form(entity="other"))["entity"] == "other"
    assert relation_fingerprint(form) == relation_fingerprint(_form(entity="other"))
    assert date_fingerprint(form) == date_fingerprint(_form(entity="other"))
    assert date_fingerprint(form) != date_fingerprint(_form(event_time="other"))


def test_snowflake_table_is_not_called_unpartitioned(monkeypatch):
    monkeypatch.setattr(
        "factcat_app.layout.columns_from_form",
        lambda form: {
            "columns": [],
            "relation": {
                "name": "EVENTS",
                "kind": "table",
                "partition": None,
                "clustering": [],
                "automatic_clustering": False,
            },
        },
    )
    payload, _store = assemble_layout(
        _form(
            kind="snowflake",
            database="ANALYTICS",
            schema="MARTS",
            table="ANALYTICS.MARTS.EVENTS",
            table_name="EVENTS",
        )
    )
    assert payload["metadata_verdict"] is None
    assert (payload["probe"] or {}).get("verdict") is None


def test_assemble_layout_reuses_relation_when_mapping_changes(monkeypatch):
    calls = {"n": 0}

    def columns(form):
        calls["n"] += 1
        return {
            "location": "EU",
            "columns": [],
            "relation": {
                "name": "events",
                "kind": "table",
                "partition": {
                    "field": "occurred_at",
                    "type": "MONTH",
                    "ingestion": False,
                },
                "clustering": ["event_name", "user_id"],
            },
        }

    monkeypatch.setattr("factcat_app.layout.columns_from_form", columns)
    payload, store = assemble_layout(_form())
    assert calls["n"] == 1
    assert payload["cluster"]["entity_pos"] == 2
    payload, store = assemble_layout(_form(entity="customer_care_id"), stored=store)
    assert calls["n"] == 1
    assert payload["cluster"]["entity_pos"] == 0
    payload, store = assemble_layout(_form(event_time="created_at"), stored=store)
    assert calls["n"] == 1
    payload, store = assemble_layout(
        _form(table="p.analytics.other", table_name="other"), stored=store
    )
    assert calls["n"] == 2


def test_cluster_fields_parse_linear():
    assert _cluster_fields("LINEAR(EVENT_NAME, TO_DATE(EVENT_TIME))")[0] == "EVENT_NAME"
    assert _object_kind("MATERIALIZED VIEW") == "materialized_view"
    assert _object_kind("VIEW") == "view"



