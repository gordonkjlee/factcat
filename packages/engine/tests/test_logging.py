"""The rotating log beside the mapping: the first artefact that survives a
failure, so a bug report can carry the SQL and the warehouse's own words.

Mutations that must go red: drop the ``logger.exception`` call in ``_fail``
(``test_run_failure_lands_in_log`` loses "boom" and "fc_event_ts"); drop the
one in ``_catalog_error`` (``test_catalog_failure_lands_in_log``).
"""

from __future__ import annotations

from logging.handlers import RotatingFileHandler
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from factcat.warehouses import AdapterError
from factcat_app.main import app, logger

FORM = {
    "project": "p",
    "location": "EU",
    "table": "analytics.events",
    "entity": "account_id",
    "event_time": "occurred_at",
    "measure": "uniques",
    "grain": "day",
    "lookback_days": 30,
}


def _file_handlers() -> list[RotatingFileHandler]:
    return [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]


@pytest.fixture()
def clean_logger():
    """The logger is process-wide; a handler left over from another test
    would keep writing into that test's temp directory."""

    def strip() -> None:
        for handler in _file_handlers():
            logger.removeHandler(handler)
            handler.close()

    strip()
    yield
    strip()


def test_run_failure_lands_in_log(monkeypatch, tmp_path, clean_logger):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.side_effect = AdapterError("boom")
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    log = tmp_path / "factcat.log"

    with TestClient(app) as client:
        # A form the app refuses is a line, not an incident.
        res = client.post("/api/run", json={**FORM, "lookback_days": 0})
        assert res.status_code == 400
        text = log.read_text(encoding="utf-8")
        assert "run rejected" in text
        assert "Traceback" not in text

        res = client.post("/api/run", json=FORM)
        assert res.status_code == 400
        assert res.json()["error"] == "boom"
        text = log.read_text(encoding="utf-8")
        assert "boom" in text
        assert "fc_event_ts" in text
        assert "Traceback" in text
        assert len(_file_handlers()) == 1

    # A second startup in the same process must not double every line.
    with TestClient(app):
        assert len(_file_handlers()) == 1
    assert _file_handlers()[0].baseFilename == str(log)


def test_catalog_failure_lands_in_log(monkeypatch, tmp_path, clean_logger):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))

    def boom(form):
        raise AdapterError("catalog boom")

    monkeypatch.setattr("factcat_app.main.roles_from_form", boom)
    with TestClient(app) as client:
        res = client.post("/api/roles", json={"kind": "snowflake"})
    assert res.status_code == 400
    assert res.json() == {"ok": False, "error": "catalog boom"}
    text = (tmp_path / "factcat.log").read_text(encoding="utf-8")
    assert "catalog call failed" in text
    assert "catalog boom" in text
    assert "Traceback" in text
