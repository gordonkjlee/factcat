"""The Unix-epoch unit is never guessed on the compile path.

A chart whose mapped timestamp is a Unix integer with no recorded unit used to
sample the warehouse (scan cap off) from every compile-shaped endpoint, and on
any failure it wrote ``seconds`` into the mapping file. Now the endpoints refuse
with a Setup-pointing message, make no warehouse call, and persist nothing;
the unit comes from the form or the stored mapping, and is inferred only by
``/api/infer_epoch``.

Mutations that must fail: in ``ensure_epoch`` replace the ``raise`` with
``unit = "seconds"`` and a ``save`` (every no-call test goes red); in
``api_infer_epoch`` save a unit on the error path
(test_infer_epoch_failure_persists_nothing goes red).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from factcat.warehouses import AdapterError
from factcat_app.main import app

COLUMNS = [
    {"name": "occurred_at", "type": "INT64"},
    {"name": "account_id", "type": "INT64"},
    {"name": "event_name", "type": "STRING"},
]


def _cfg(tmp_path, monkeypatch, **extra):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    data = {
        "kind": "bigquery",
        "project": "adc-project",
        "location": "EU",
        "table": "analytics.events",
        "dataset": "analytics",
        "table_name": "events",
        "entity": "account_id",
        "event_time": "occurred_at",
        "event_column": "event_name",
        "columns": COLUMNS,
    }
    data.update(extra)
    (tmp_path / "cfg.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path / "cfg.json"


def _form(**extra):
    base = {
        "kind": "bigquery",
        "project": "adc-project",
        "location": "EU",
        "table": "analytics.events",
        "entity": "account_id",
        "event_time": "occurred_at",
        "event_column": "event_name",
        "columns": COLUMNS,
        "series": [{"event": "signup"}],
        "grain": "day",
        "measure": "total",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
    }
    base.update(extra)
    return base


@pytest.fixture()
def no_warehouse(monkeypatch):
    """Every path to a warehouse records the attempt; none must be taken."""
    calls: list[str] = []

    def refuse(kind, **kwargs):
        calls.append(kind)
        raise AssertionError("the compile path reached the warehouse")

    monkeypatch.setattr("factcat.warehouses.connect", refuse)
    monkeypatch.setattr("factcat_app.main.connect", refuse)
    return calls


@pytest.mark.parametrize("endpoint", ["/api/sql", "/api/estimate", "/api/run"])
def test_an_unset_epoch_unit_refuses_without_touching_the_warehouse(
    tmp_path, monkeypatch, no_warehouse, endpoint
):
    cfg = _cfg(tmp_path, monkeypatch)
    before = cfg.read_bytes()
    res = TestClient(app).post(endpoint, json=_form())
    assert res.status_code == 400, res.text
    body = res.json()
    assert body["ok"] is False
    assert "unit is not set" in body["error"] and "Setup" in body["error"]
    assert no_warehouse == []
    assert cfg.read_bytes() == before


def test_a_stored_unit_is_used_when_the_form_omits_it(tmp_path, monkeypatch, no_warehouse):
    _cfg(tmp_path, monkeypatch, event_time_epoch="milliseconds")
    res = TestClient(app).post("/api/sql", json=_form())
    assert res.status_code == 200, res.text
    assert "TIMESTAMP_MILLIS" in res.json()["sql"]
    assert no_warehouse == []


def test_a_unit_on_the_form_wins_and_still_makes_no_call(tmp_path, monkeypatch, no_warehouse):
    _cfg(tmp_path, monkeypatch)
    res = TestClient(app).post("/api/sql", json=_form(event_time_epoch="microseconds"))
    assert res.status_code == 200, res.text
    assert "TIMESTAMP_MICROS" in res.json()["sql"]
    assert no_warehouse == []


def test_infer_epoch_failure_persists_nothing(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    before = cfg.read_bytes()

    def broken(kind, **kwargs):
        raise AdapterError("no jobs permission")

    monkeypatch.setattr("factcat.warehouses.connect", broken)
    res = TestClient(app).post("/api/infer_epoch", json=_form())
    assert res.json()["ok"] is False
    assert cfg.read_bytes() == before
    assert "event_time_epoch" not in json.loads(cfg.read_text(encoding="utf-8"))
