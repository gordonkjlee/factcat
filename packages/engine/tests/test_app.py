"""HTTP surface. Warehouse is mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from factcat.warehouses import BytesCapError, QueryResult
from factcat_app.catalog import column_fits
from factcat_app.config import mapping_ready
from factcat_app.main import APP_DIR, app


def _map_cfg(tmp_path, monkeypatch, **extra):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    data = {
        "project": "adc-project",
        "location": "EU",
        "table": "analytics.events",
        "dataset": "analytics",
        "table_name": "events",
        "entity": "account_id",
        "event_time": "occurred_at",
    }
    data.update(extra)
    (tmp_path / "cfg.json").write_text(json.dumps(data), encoding="utf-8")


def test_unmapped_root_redirects_to_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/setup"


def test_setup_renders(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    res = client.get("/setup")
    assert res.status_code == 200
    assert "Project setup" in res.text
    assert "Entity id" in res.text
    assert "This grain is called" not in res.text
    assert "Entity name (singular)" in res.text
    assert "Plural" in res.text
    assert "Event name column" in res.text
    assert "Week starts on" in res.text
    assert "Formatting" in res.text
    assert "Thousand separator" in res.text
    assert "Decimal separator" in res.text
    assert 'id="thousand_sep"' in res.text
    assert 'id="decimal_sep"' in res.text
    assert ">Other<" in res.text
    assert 'value="User"' in res.text
    assert "Volume" not in res.text
    assert 'value="user_id"' not in res.text
    assert "/path/to/key.json" not in res.text
    assert "Load datasets" not in res.text
    assert "form.dataset" not in res.text
    assert "Select a dataset" not in res.text
    assert "Select a table" not in res.text
    assert 'getElementById("dataset")' in res.text
    assert 'id="dataset-loading"' in res.text
    assert 'aria-label="Loading"' in res.text
    assert 'value="adc-project"' in res.text
    assert 'href="/setup"' in res.text
    assert "rail-setup" in res.text
    assert "if (catalogProject()) loadDatasets()" not in res.text
    assert 'addEventListener("mousedown"' in res.text
    assert ">Analysis<" not in res.text
    assert "Job scan cap" in res.text
    assert 'id="bytes_cap_gb"' in res.text
    assert "Result row limit" in res.text
    assert 'id="query_row_limit"' in res.text
    assert "Save and open Events" not in res.text
    assert 'id="save"' in res.text
    assert ">Save<" in res.text
    assert "window.location.href" not in res.text
    assert "Saved" in res.text
    assert "catalog: true" in res.text


def test_events_renders_when_mapped(monkeypatch, tmp_path):
    _map_cfg(tmp_path, monkeypatch)
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "Volume" in res.text
    assert "Unique Users" in res.text
    assert "Unique User<" not in res.text
    assert "Average per User" in res.text
    assert ">Uniques<" not in res.text
    assert "Only this event" not in res.text
    assert "Event name column" not in res.text
    assert "Date range" in res.text
    assert "Time grain" in res.text
    assert "<label>Bucket</label>" not in res.text
    assert "Last 30 days" in res.text
    assert "Last 8 weeks" in res.text
    assert "Last 6 months" in res.text
    assert "This week" in res.text
    assert "Last week" in res.text
    assert "Yesterday" in res.text
    assert 'label: "Custom"' in res.text
    assert "Specific dates" in res.text
    assert ">Relative<" in res.text
    assert "event_names: data.values" not in res.text
    assert "Include this week" in res.text
    assert "RANGE_PRESETS" in res.text
    assert 'id="range_choice"' in res.text
    assert "applyRangeChoice" in res.text
    assert "seedRangeChoice" in res.text
    assert 'value="this">This<' not in res.text
    assert 'value="previous">Previous<' not in res.text
    assert "Exclude current period" not in res.text
    html = res.text
    grain_at = html.find('id="grain"')
    range_at = html.find('id="range_choice"')
    assert grain_at != -1 and range_at != -1 and grain_at < range_at
    assert "fillSelect(eventValueEl, cachedEventNames" in html
    assert "syncChartTitle" not in html
    assert "Exact unique counts" in res.text
    assert "Exact (off = approx unique count)" not in res.text
    assert 'id="copy-sql"' in res.text
    assert 'id="copy-chart"' in res.text
    assert 'id="copy-table"' in res.text
    assert "grainHeader" in res.text
    assert "valueHeader" in res.text
    assert ">Bucket<" not in res.text
    assert 'id="export-png"' in res.text
    assert 'id="chart_type"' in res.text
    assert "Labels" in res.text
    assert 'id="chart-title"' in res.text
    assert 'id="reset-title"' in res.text
    assert "Reset title" in res.text
    assert 'value: "this:week"' in res.text
    assert "Export CSV" in res.text
    assert "All events" not in res.text
    assert "Pick an event." in res.text
    assert "Running…" in res.text
    assert 'id="run"' in res.text
    assert 'id="run-estimate"' in res.text
    assert "/api/estimate" in res.text
    assert ">Area<" in res.text
    assert "Format chart" in res.text
    assert "2 decimals" in res.text
    assert "Scientific" in res.text
    assert "Major and minor" in res.text
    assert "n <= 2 ? \"bar\" : \"line\"" in res.text
    assert 'id="thousand_sep"' in res.text
    assert "applySeps" in res.text
    assert "Show last" not in res.text
    assert "limit-note" in res.text
    assert "Load more" in res.text
    assert 'id="query_row_limit"' in res.text
    assert "Override cap" in res.text
    assert "icon-btn" in res.text
    assert "estimateKey" in res.text
    assert 'e.target.id === "exact"' in res.text
    assert "class=\"sort\"" in res.text or 'className = "sort"' in res.text
    assert html.find('id="exact-wrap"') < html.find('id="exact-hint"') < html.find('id="run"')
    assert "Refresh list" in res.text
    assert "/api/event_values" in res.text
    assert "<h1>Project setup</h1>" not in res.text
    assert "GCP project that runs" not in res.text
    assert "account_id" in res.text
    assert 'value="user_id"' not in res.text
    assert "pane-chart" in res.text
    assert "pane-table" in res.text
    assert "pane-sql" in res.text
    assert "Run to plot." in res.text
    assert "Run to see rows." in res.text
    assert "Run to see generated SQL." in res.text
    assert "<summary>SQL</summary>" not in res.text


def test_events_serves_cached_event_names(monkeypatch, tmp_path):
    _map_cfg(
        tmp_path,
        monkeypatch,
        event_column="event_name",
        event_names=["opened", "paid"],
        event_value="paid",
    )
    client = TestClient(app)
    html = client.get("/").text
    assert "cachedEventNames" in html
    assert '"opened"' in html
    assert '"paid"' in html
    assert 'id="event_value-wrap" hidden' not in html
    assert "fillSelect(eventValueEl, cachedEventNames" in html
    assert "All events" not in html


def test_run_builds_spec_and_calls_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[{"bucket": "2026-01-05", "value": 2}]
    )
    captured: dict = {}

    def fake_connect(kind, **kw):
        captured["kind"] = kind
        captured.update(kw)
        return warehouse

    monkeypatch.setattr("factcat_app.main.connect", fake_connect)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
            "exact": False,
            "credentials": "/tmp/sa.json",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["rows"] == [{"bucket": "2026-01-05", "value": 2, "incomplete": False}]
    assert body["truncated"] is False
    assert body["limit"] == 1_000_000
    warehouse.run.assert_called_once()
    sql = warehouse.run.call_args.args[0]
    compact = " ".join(sql.split()).upper()
    assert "LIMIT 1000000" in compact
    assert "ORDER BY BUCKET DESC" in compact
    assert "account_id" in sql
    assert "user_id" not in sql
    assert "TIMESTAMP_TRUNC" in sql.upper() or "DATE_TRUNC" in sql.upper()
    assert "DATE" in sql.upper()
    assert captured["kind"] == "bigquery"
    assert captured["project"] == "p"
    assert captured["location"] == "EU"
    assert captured["credentials"] == "/tmp/sa.json"
    assert (tmp_path / "cfg.json").is_file()


def test_estimate_is_dry_run(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    captured: dict = {}

    def fake_run(sql, *, dry_run=False):
        captured["sql"] = sql
        captured["dry_run"] = dry_run
        return QueryResult(rows=[{"bucket": "x", "value": 1}], bytes_processed=1500)

    warehouse.run.side_effect = fake_run
    monkeypatch.setattr(
        "factcat_app.main.connect", lambda kind, **kw: warehouse
    )
    client = TestClient(app)
    res = client.post(
        "/api/estimate",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["bytes"] == 1500
    assert body["over_cap"] is False
    assert captured["dry_run"] is True
    assert "account_id" in captured["sql"]


def test_estimate_over_cap_still_returns_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.side_effect = BytesCapError(
        "too big",
        bytes_processed=20 * 1024**3,
        maximum_bytes_billed=10 * 1024**3,
    )
    monkeypatch.setattr(
        "factcat_app.main.connect", lambda kind, **kw: warehouse
    )
    client = TestClient(app)
    res = client.post(
        "/api/estimate",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["over_cap"] is True
    assert body["bytes"] == 20 * 1024**3


def test_run_flags_truncated_when_row_count_hits_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[
            {"bucket": "2026-01-01", "value": 1},
            {"bucket": "2026-01-02", "value": 2},
            {"bucket": "2026-01-03", "value": 3},
        ]
    )
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "query_row_limit": 3,
        },
    )
    body = res.json()
    assert body["ok"] is True
    assert body["truncated"] is True
    assert body["limit"] == 3
    sql = warehouse.run.call_args.args[0]
    assert "LIMIT 3" in " ".join(sql.split()).upper()


def test_run_limit_override_does_not_persist_to_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=[{"bucket": "2026-01-01", "value": 1}])
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "query_row_limit": 5000,
            "query_row_limit_run": 10000,
        },
    )
    body = res.json()
    assert body["ok"] is True
    assert body["limit"] == 10000
    sql = warehouse.run.call_args.args[0]
    assert "LIMIT 10000" in " ".join(sql.split()).upper()
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert saved["query_row_limit"] == 5000
    assert "query_row_limit_run" not in saved


def test_run_rejects_empty_entity(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 400
    assert "entity" in res.json()["error"].lower()


def test_save_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/save",
        json={"project": "acme", "location": "EU", "entity": "account_id"},
    )
    assert res.status_code == 200
    html = client.get("/").text
    assert "acme" in html
    assert "account_id" in html


def test_blank_credentials_omitted_from_connect(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=[])
    captured: dict = {}

    def fake_connect(kind, **kw):
        captured.update(kw)
        captured["kind"] = kind
        return warehouse

    monkeypatch.setattr("factcat_app.main.connect", fake_connect)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
            "credentials": "",
        },
    )
    assert res.status_code == 200
    assert captured["kind"] == "bigquery"
    assert "credentials" not in captured


def test_run_rejects_lookback_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 0,
        },
    )
    assert res.status_code == 400
    assert "lookback" in res.json()["error"].lower()


def test_run_rejects_missing_project(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 400
    assert "project" in res.json()["error"].lower()


def test_save_drops_unknown_keys_and_nulls(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    client.post(
        "/api/save",
        json={
            "project": "acme",
            "location": "EU",
            "credentials": None,
            "extra": "drop-me",
        },
    )
    html = client.get("/").text
    assert "acme" in html
    assert "drop-me" not in html
    assert 'value="None"' not in html


def test_template_ships_with_package():
    templates = Path(APP_DIR) / "templates"
    assert (templates / "index.html").is_file()
    assert (templates / "setup.html").is_file()
    assert (templates / "base.html").is_file()


def test_mapping_ready_requires_table_entity_and_time():
    base = {
        "project": "p",
        "location": "EU",
        "table": "analytics.events",
        "entity": "account_id",
        "event_time": "occurred_at",
    }
    assert mapping_ready(base)
    for key in ("table", "entity", "event_time"):
        missing = dict(base)
        missing[key] = ""
        assert not mapping_ready(missing)


def test_save_overlays_without_resetting_lookback(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    client.post(
        "/api/save",
        json={"project": "acme", "lookback_days": 90, "measure": "total"},
    )
    client.post("/api/save", json={"location": "EU"})
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert saved["project"] == "acme"
    assert saved["location"] == "EU"
    assert saved["lookback_days"] == 90
    assert saved["measure"] == "total"


def test_datasets_lists_from_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.catalog.list_datasets",
        lambda **kw: [{"id": "3_entity"}, {"id": "analytics"}],
    )
    client = TestClient(app)
    res = client.post("/api/datasets", json={"project": "p"})
    assert res.status_code == 200
    assert res.json()["datasets"][0]["id"] == "3_entity"


def test_tables_return_location(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.catalog.list_tables",
        lambda **kw: {"location": "EU", "tables": ["customer_events"]},
    )
    client = TestClient(app)
    res = client.post(
        "/api/tables", json={"project": "p", "dataset": "3_entity"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["location"] == "EU"
    assert "customer_events" in body["tables"]


def test_columns_are_sorted_alphabetically(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.catalog.list_columns",
        lambda **kw: {
            "location": "EU",
            "columns": [
                {"name": "zeta", "type": "STRING"},
                {"name": "alpha", "type": "STRING"},
            ],
        },
    )
    client = TestClient(app)
    res = client.post(
        "/api/columns",
        json={"project": "p", "dataset": "analytics", "table_name": "events"},
    )
    names = [c["name"] for c in res.json()["columns"]]
    assert names == ["alpha", "zeta"]


def test_event_values_run_distinct_and_sort(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[
            {"fc_value": "paid"},
            {"fc_value": None},
            {"fc_value": "opened"},
            {"fc_value": "  "},
        ]
    )
    captured: dict = {}

    def fake_connect(kind, **kw):
        captured["kind"] = kind
        captured.update(kw)
        return warehouse

    monkeypatch.setattr("factcat_app.main.connect", fake_connect)
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "event_column": "event_name",
            "event_time": "occurred_at",
            "lookback_days": 7,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["values"] == ["opened", "paid"]
    sql = warehouse.run.call_args.args[0].upper()
    assert "DISTINCT" in sql
    assert "ORDER BY" in sql
    assert captured["kind"] == "bigquery"
    assert captured["project"] == "p"
    assert captured["location"] == "EU"


def test_catalog_event_values_writes_event_names(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(
        json.dumps({"event_names": ["stale"]}), encoding="utf-8"
    )
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[{"fc_value": "opened"}, {"fc_value": "paid"}]
    )
    monkeypatch.setattr(
        "factcat_app.main.connect", lambda kind, **kw: warehouse
    )
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "event_column": "event_name",
            "event_time": "occurred_at",
            "catalog": True,
        },
    )
    assert res.status_code == 200
    assert res.json()["values"] == ["opened", "paid"]
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert saved["event_names"] == ["opened", "paid"]


def test_non_catalog_event_values_do_not_write_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(
        json.dumps({"event_names": ["keep"]}), encoding="utf-8"
    )
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=[{"fc_value": "paid"}])
    monkeypatch.setattr(
        "factcat_app.main.connect", lambda kind, **kw: warehouse
    )
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "event_column": "event_name",
            "event_time": "occurred_at",
        },
    )
    assert res.status_code == 200
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert saved["event_names"] == ["keep"]


def test_columns_list_names(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.catalog.list_columns",
        lambda **kw: {
            "location": "EU",
            "columns": [
                {"name": "customer_care_id", "type": "STRING"},
                {"name": "event_datetime", "type": "DATETIME"},
            ],
        },
    )
    client = TestClient(app)
    res = client.post(
        "/api/columns",
        json={"project": "p", "dataset": "3_entity", "table_name": "customer_events"},
    )
    assert res.status_code == 200
    names = [c["name"] for c in res.json()["columns"]]
    assert names == ["customer_care_id", "event_datetime"]


def test_datasets_require_project(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post("/api/datasets", json={"project": ""})
    assert res.status_code == 400


def test_entity_column_types_are_ids_not_timestamps():
    assert column_fits("STRING", "entity")
    assert column_fits("integer", "entity")
    assert column_fits("INT64", "entity")
    assert column_fits("NUMERIC", "entity")
    assert not column_fits("TIMESTAMP", "entity")
    assert not column_fits("FLOAT", "entity")
    assert not column_fits("BOOL", "entity")
    assert not column_fits("RECORD", "entity")


def test_event_time_column_types_are_temporal():
    assert column_fits("TIMESTAMP", "event_time")
    assert column_fits("DATETIME", "event_time")
    assert not column_fits("DATE", "event_time")
    assert not column_fits("STRING", "event_time")
    assert not column_fits("TIME", "event_time")
    assert not column_fits("INT64", "event_time")


def test_event_name_column_is_string():
    assert column_fits("STRING", "event_column")
    assert not column_fits("INT64", "event_column")
    assert not column_fits("TIMESTAMP", "event_column")
