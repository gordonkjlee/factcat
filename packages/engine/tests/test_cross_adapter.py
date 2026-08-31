"""Shipped adapters, not the one you happen to be looking at.

SQL generation already walks ``SUPPORTED`` in ``test_dialects.py``. This file
walks ``ADAPTERS`` for the app compile path and chrome registry. Mocked
clients only — no live warehouse in CI.
"""

from __future__ import annotations

import logging

import pytest

from factcat.warehouses import ADAPTERS, capabilities, extra_installed, extra_requirement
from factcat_app.catalog import catalog_steps, catalog_steps_by_kind, type_sets
from factcat_app.main import SETUP_DOCS
from factcat_app.query import event_values_sql, events_sql_from_form


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
def test_event_values_sql_emits_without_warnings(kind, sqlglot_warnings):
    event_values_sql(_form(kind, catalog=True))
    assert sqlglot_warnings.messages == [], (
        f"sqlglot warned for {kind}: {sqlglot_warnings.messages}"
    )


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_capabilities_declared(kind):
    caps = capabilities(kind)
    assert isinstance(caps, frozenset)


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


def test_setup_docs_cover_every_adapter():
    assert set(SETUP_DOCS) == set(ADAPTERS)
    for kind, path in SETUP_DOCS.items():
        assert path.is_file(), f"missing Setup guide for {kind}: {path}"
        text = path.read_text(encoding="utf-8")
        assert f"pip install factcat[{kind}]" not in text, (
            f"Setup guide must not duplicate the extra banner: {path}"
        )
