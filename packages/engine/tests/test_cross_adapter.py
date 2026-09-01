"""Shipped adapters, not the one you happen to be looking at.

SQL generation already walks ``SUPPORTED`` in ``test_dialects.py``. This file
walks ``ADAPTERS`` for the app compile path and chrome registry. Mocked
clients only — no live warehouse in CI.
"""

from __future__ import annotations

import logging

import pytest

from fastapi.testclient import TestClient

from factcat.warehouses import (
    ADAPTERS,
    CAP_DRY_RUN,
    CAP_SCAN_CAP,
    capabilities,
    extra_installed,
    extra_requirement,
)
from factcat.warehouses.bigquery import DEFAULT_MAXIMUM_BYTES_BILLED
from factcat_app.catalog import catalog_steps, catalog_steps_by_kind, type_sets
from factcat_app.main import SETUP_DOCS
from factcat_app.main import app as main_app
from factcat_app.query import (
    connection_from_form,
    event_name_cache_rebuild_sql,
    event_values_sql,
    events_sql_from_form,
)


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


def _form(kind: str, **extra):
    base = {
        "kind": kind,
        "table": "analytics.events",
        "entity": "subscription_id",
        "event_time": "occurred_at",
        "measure": "uniques",
        "grain": "day",
        "lookback_days": 30,
        "exact": False,
        "event_column": "event_name",
        "event_value": "paid",
        "reporting_timezone": "Europe/Berlin",
    }
    if kind == "snowflake":
        base["table"] = "ANALYTICS.MARTS.EVENTS"
    base.update(extra)
    return base


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_events_sql_from_form_emits_without_warnings(kind, sqlglot_warnings):
    events_sql_from_form(_form(kind))
    events_sql_from_form(_form(kind, grain="week", range_mode="last", range_n=8, range_unit="week"))
    events_sql_from_form(_form(kind, grain="hour", range_mode="last", range_n=24, range_unit="hour"))
    events_sql_from_form(_form(kind, grain="day_of_week"))
    events_sql_from_form(_form(kind, grain="hour_of_day"))
    events_sql_from_form(_form(kind, breakdown_column="country"))
    events_sql_from_form(
        _form(
            kind,
            breakdowns=[
                {"breakdown_column": "country"},
                {"breakdown_column": "browser"},
            ],
        )
    )
    assert sqlglot_warnings.messages == [], (
        f"sqlglot warned for {kind}: {sqlglot_warnings.messages}"
    )


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_breakdown_value_semantics_compile(kind, sqlglot_warnings):
    """Value at × If missing × Fill from compiles per shipped adapter:
    carried stream, range anchor, and no placeholder residue."""
    sql = events_sql_from_form(
        _form(
            kind,
            range_mode="custom",
            custom_kind="absolute",
            start_date="2026-01-01",
            end_date="2026-01-31",
            breakdowns=[
                {
                    "breakdown_column": "country",
                    "value_at": "event",
                    "if_missing": "fill",
                    "fill_from_event": "signup",
                },
                {"breakdown_column": "browser", "value_at": "range_start"},
                {
                    "breakdown_column": "plan",
                    "value_at": "range_end",
                    "if_missing": "fill",
                },
            ],
        )
    )
    assert "fc_stamps" in sql
    assert "FACTCAT_" not in sql.upper()
    assert "IGNORE NULLS" not in sql.upper()
    assert sqlglot_warnings.messages == [], (
        f"sqlglot warned for {kind}: {sqlglot_warnings.messages}"
    )


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_hour_grains_leave_no_placeholders(kind):
    for grain, extra in (
        ("hour", {"range_mode": "last", "range_n": 24, "range_unit": "hour"}),
        ("day_of_week", {}),
        ("hour_of_day", {}),
    ):
        sql = events_sql_from_form(_form(kind, grain=grain, **extra))
        assert "FACTCAT_" not in sql.upper(), sql


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_event_values_sql_emits_without_warnings(kind, sqlglot_warnings):
    event_values_sql(_form(kind, catalog=True))
    assert sqlglot_warnings.messages == [], (
        f"sqlglot warned for {kind}: {sqlglot_warnings.messages}"
    )


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_event_values_window_does_not_cast_time_column(kind):
    sql = event_values_sql(_form(kind, catalog=True)).upper().replace(" ", "")
    assert "CAST(OCCURRED_AT" not in sql
    assert "OCCURRED_AT>=" in sql
    assert "FACTCAT_" not in sql
    events = events_sql_from_form(_form(kind)).upper().replace(" ", "")
    assert "CAST(OCCURRED_AT ASTIMESTAMP)>=" not in events
    assert "CAST(OCCURRED_AT ASDATETIME)>=" not in events
    assert "OCCURRED_AT>=" in events
    assert "FACTCAT_" not in events
    if kind == "snowflake":
        assert "TIMESTAMP_NTZ" in sql or "CONVERT_TIMEZONE" in sql
        assert "TIMESTAMP_NTZ" in events or "CONVERT_TIMEZONE" in events
    if kind == "bigquery":
        assert "DATETIME(TIMESTAMP(" in sql.replace("`", "")


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_event_name_cache_rebuild_walks_adapters(kind, sqlglot_warnings):
    extra = {"event_column": "event_name"}
    if kind == "snowflake":
        extra.update(write_database="ANALYTICS", write_schema="MARTS")
    else:
        extra.update(write_project="dest-proj", write_dataset="analytics")
    sql = event_name_cache_rebuild_sql(_form(kind, **extra), materialized=True)
    assert "CREATE OR REPLACE MATERIALIZED VIEW" in sql.upper()
    assert "GROUP BY" in sql.upper()
    assert sqlglot_warnings.messages == [], (
        f"sqlglot warned for {kind}: {sqlglot_warnings.messages}"
    )


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_capabilities_declared(kind):
    caps = capabilities(kind)
    assert isinstance(caps, frozenset)


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_scan_cap_rides_the_capability_not_the_kind(kind):
    """A kind without CAP_SCAN_CAP is never handed a cap it cannot honour."""
    conn = connection_from_form(_form(kind, project="p", location="EU"))
    if CAP_SCAN_CAP in capabilities(kind):
        assert conn["maximum_bytes_billed"] == DEFAULT_MAXIMUM_BYTES_BILLED
        assert connection_from_form(
            _form(kind, project="p", location="EU", override_cap=True)
        )["maximum_bytes_billed"] is None
    else:
        assert "maximum_bytes_billed" not in conn


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_estimate_gated_on_dry_run_capability(kind, monkeypatch, tmp_path):
    """A kind that cannot estimate says so instead of opening a connection."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / f"{kind}.json"))

    def _boom(*a, **kw):
        raise AssertionError(f"{kind} must not connect to estimate")

    if CAP_DRY_RUN not in capabilities(kind):
        monkeypatch.setattr("factcat_app.main.connect", _boom)
        res = TestClient(main_app).post(
            "/api/estimate", json=_form(kind, project="p", location="EU")
        )
        assert res.json()["supported"] is False


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_extra_probe_declared(kind):
    assert extra_requirement(kind) == f"factcat[{kind}]"
    assert extra_installed(kind) in (True, False)


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_type_sets_declared(kind):
    sets = type_sets(kind)
    for role in (
        "entity",
        "event_time",
        "event_time_instant",
        "event_time_wallclock",
        "event_time_unix",
        "event_column",
        "of",
        "json",
    ):
        assert role in sets
        assert isinstance(sets[role], frozenset)


def test_catalog_steps_cover_every_adapter():
    assert set(catalog_steps_by_kind()) == set(ADAPTERS)
    for kind in ADAPTERS:
        steps = catalog_steps(kind)
        assert steps, f"{kind} has no catalog steps"
        ids = []
        for step in steps:
            assert "needs" in step
            assert "endpoint" in step
            ids.extend(
                [fill["id"] for fill in step["fill"]] if step.get("fill") else [step["id"]]
            )
        assert "table_name" in ids, f"{kind} catalog must pick a table"
        if kind == "bigquery":
            assert "write_dataset" in ids
            write_ds = next(s for s in steps if s["id"] == "write_dataset")
            assert "write_project" in write_ds["needs"]
        if kind == "snowflake":
            assert "write_database" in ids
            assert "write_schema" in ids


def test_setup_docs_cover_every_adapter():
    assert set(SETUP_DOCS) == set(ADAPTERS)
    for kind, path in SETUP_DOCS.items():
        assert path.is_file(), f"missing Setup guide for {kind}: {path}"
        text = path.read_text(encoding="utf-8")
        assert f"pip install factcat[{kind}]" not in text, (
            f"Setup guide must not duplicate the extra banner: {path}"
        )
