"""HTTP surface. Warehouse is mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from factcat.warehouses import QueryResult
from factcat_app.main import APP_DIR, app


def test_index_renders(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "Uniques" in res.text
    assert 'value="user_id"' not in res.text
    assert "/path/to/key.json" not in res.text
    assert "dataset.events" not in res.text
    assert "subscription_id" not in res.text
    assert "Load datasets" in res.text
    assert 'value="adc-project"' in res.text


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
    assert body["rows"] == [{"bucket": "2026-01-05", "value": 2}]
    warehouse.run.assert_called_once()
    sql = warehouse.run.call_args.args[0]
    assert "account_id" in sql
    assert "user_id" not in sql
    assert "TIMESTAMP_TRUNC" in sql.upper()
    assert captured["kind"] == "bigquery"
    assert captured["project"] == "p"
    assert captured["location"] == "EU"
    assert captured["credentials"] == "/tmp/sa.json"
    assert (tmp_path / "cfg.json").is_file()


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
    assert (Path(APP_DIR) / "templates" / "index.html").is_file()


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
