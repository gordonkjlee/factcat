"""HTTP surface. Warehouse is mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from factcat.warehouses import QueryResult
from factcat_app.main import app


def test_index_renders(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "Uniques" in res.text
    assert 'value="user_id"' not in res.text


def test_run_builds_spec_and_calls_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[{"bucket": "2026-01-05", "value": 2}]
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
            "lookback_days": 30,
            "exact": False,
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
