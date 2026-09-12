"""Live Snowflake: the one job the mocked suite cannot do.

Skipped unless ``FACTCAT_SF_LIVE_CONFIG`` names a Factcat config file
whose mapping points at an EMPTY events table in a throwaway schema. The
file must not be a working ``.factcat.json``: a real mapping would run the
seven chart shapes against real data and bill a real warehouse for it.

Run with ``-s`` to see the driver's raw column casing on the console.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from factcat.warehouses.snowflake import SnowflakeAdapter, _open_connection
from factcat_app.query import connection_from_form, events_sql_from_form

CONFIG_ENV = "FACTCAT_SF_LIVE_CONFIG"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get(CONFIG_ENV), reason=f"{CONFIG_ENV} not set"
    ),
]


@pytest.fixture(scope="module")
def live_config() -> dict:
    path = Path(os.environ[CONFIG_ENV]).expanduser()
    if path.name == ".factcat.json":
        pytest.fail(
            f"{CONFIG_ENV} must not be a working .factcat.json: point it at a "
            "copy whose table is empty"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if str(data.get("kind") or "").lower() != "snowflake":
        pytest.fail(f"{CONFIG_ENV} must have kind = snowflake")
    for key in ("table", "entity", "event_time", "event_column"):
        if not str(data.get(key) or "").strip():
            pytest.fail(f"{CONFIG_ENV} has no {key} mapped")
    return data


@pytest.fixture(scope="module")
def adapter(live_config) -> SnowflakeAdapter:
    return SnowflakeAdapter(**connection_from_form(live_config))


def test_select_one_lowers_the_column(adapter):
    """The adapter lowers result columns so every consumer reads the key it
    generated. This is the first place a real driver, not a mock, reports
    what it actually hands back."""
    result = adapter.run("SELECT 1 AS fc_rows")
    assert result.rows == [{"fc_rows": 1}]
    assert result.job_id


def test_record_raw_description_casing(adapter):
    """Print what the driver reports for an unquoted alias, before the
    adapter lowers it. Paste the observed value into the casing docstring
    on ``test_run_returns_dicts`` in ``tests/test_snowflake.py`` so the
    mocked suite states a fact rather than a reading of the docs."""
    ctx = _open_connection(**adapter._connect_kwargs())
    try:
        cur = ctx.cursor()
        try:
            cur.execute("SELECT 1 AS fc_rows")
            raw = [col[0] for col in (cur.description or [])]
        finally:
            cur.close()
    finally:
        ctx.close()
    print(f"\nraw cursor.description names for 'SELECT 1 AS fc_rows': {raw!r}")
    assert len(raw) == 1
    assert raw[0].lower() == "fc_rows"


def test_session_statement_timeout_is_in_force(adapter):
    """The adapter sends STATEMENT_TIMEOUT_IN_SECONDS as a session parameter;
    the warehouse must report it in force, or a stuck statement keeps billing
    after the client has given up waiting. Read back from the session, never
    from our own connect kwargs."""
    result = adapter.run("SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN SESSION")
    assert result.rows, "the session reports no STATEMENT_TIMEOUT_IN_SECONDS"
    assert int(result.rows[0]["value"]) == int(adapter.timeout)


def _shapes(cfg: dict) -> list[dict]:
    """The seven shapes ``tests.test_cross_adapter`` compiles, on the
    file's own mapping. Breakdown columns are the mapped event and entity
    columns, the only two the file promises exist."""
    base = {
        "kind": "snowflake",
        "table": cfg["table"],
        "entity": cfg["entity"],
        "event_time": cfg["event_time"],
        "measure": "uniques",
        "grain": "day",
        "lookback_days": 30,
        "exact": False,
        "event_column": cfg["event_column"],
        "event_value": str(cfg.get("event_value") or "paid"),
        "reporting_timezone": "Europe/Berlin",
    }
    extras = [
        {},
        {"grain": "week", "range_mode": "last", "range_n": 8, "range_unit": "week"},
        {"grain": "hour", "range_mode": "last", "range_n": 24, "range_unit": "hour"},
        {"grain": "day_of_week"},
        {"grain": "hour_of_day"},
        {"breakdown_column": cfg["event_column"]},
        {
            "breakdowns": [
                {"breakdown_column": cfg["event_column"]},
                {"breakdown_column": cfg["entity"]},
            ]
        },
    ]
    return [{**base, **extra} for extra in extras]


@pytest.mark.parametrize("index", range(7))
def test_cross_adapter_shapes_run_on_an_empty_table(adapter, live_config, index):
    """Snowflake has no dry run, so the shapes execute for real. The table is
    empty and the events SQL has no period grid, so every shape must come
    back with no rows; anything else is a real read of someone's data."""
    form = _shapes(live_config)[index]
    result = adapter.run(events_sql_from_form(form))
    assert result.rows == []
