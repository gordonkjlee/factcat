"""Rolling active users, executed by a real warehouse.

The hermetic suite runs the SQL on DuckDB and walks the other dialects as text.
It cannot show that BigQuery or Snowflake accept the period grid join, the
day addition, or the approximate distinct, nor that they count the same days.
This runs the hand-counted fixture from ``tests/test_events_active.py``
through the warehouse itself and compares every day with the Python
reference. The fixture is an inline subquery, so no table is read and no
bytes are billed.

BigQuery runs under ``FACTCAT_LIVE_CONFIG`` (``tests/live/conftest.py``);
Snowflake under ``FACTCAT_SF_LIVE_CONFIG`` and skips without it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from factcat import EventsSpec, events_sql
from tests.test_events_active import ROWS, _reference, d

pytestmark = pytest.mark.live

WINDOWS = (1, 7, 30)
FIRST, LAST = 0, 45


def _fixture_table() -> str:
    """The fixture as a derived table both warehouses read the same way."""
    selects = [
        f"SELECT '{u}' AS user_id, CAST('{d(n)} 12:00:00' AS TIMESTAMP) AS occurred_at, "
        f"'{platform}' AS platform"
        for u, n, platform in ROWS
        if u is not None
    ]
    return "(" + " UNION ALL ".join(selects) + ") AS fc_active_fixture"


def _spec(window: int, **overrides) -> EventsSpec:
    base = dict(
        table=_fixture_table(),
        entity="user_id",
        event_time="occurred_at",
        measure="active",
        window_days=window,
        exact=True,
        report_from=f"DATE '{d(FIRST)}'",
        report_until=f"DATE '{d(LAST + 1)}'",
    )
    base.update(overrides)
    return EventsSpec(**base)


def _series(rows: list[dict]) -> dict[str, int]:
    return {str(r["bucket"])[:10]: int(r["value"]) for r in rows}


def _check(adapter, dialect: str) -> None:
    for window in WINDOWS:
        sql = events_sql(_spec(window), dialect)
        got = _series(adapter.run(sql).rows)
        assert got == _reference(window, FIRST, LAST), f"window {window}"
    # Approximate distinct runs and, at this size, agrees.
    approx = _series(adapter.run(events_sql(_spec(7, exact=False), dialect)).rows)
    assert approx == _reference(7, FIRST, LAST)
    # A breakdown: u1 is active on ios (day 0) and web (day 3), so both
    # groups count it on day 3 of a 7-day window.
    rows = adapter.run(
        events_sql(
            _spec(
                7,
                breakdowns=("platform",),
                report_from=f"DATE '{d(3)}'",
                report_until=f"DATE '{d(4)}'",
            ),
            dialect,
        )
    ).rows
    keys = [k for k in rows[0] if k not in ("bucket", "value")]
    assert len(keys) == 1
    assert {r[keys[0]]: int(r["value"]) for r in rows} == {"ios": 2, "web": 2}


def test_bigquery_counts_every_day_like_the_reference(live):
    if live.kind != "bigquery":
        pytest.skip(f"the mapping is {live.kind}")
    _check(live.adapter, "bigquery")


def test_snowflake_counts_every_day_like_the_reference():
    raw = os.environ.get("FACTCAT_SF_LIVE_CONFIG", "").strip()
    if not raw:
        pytest.skip("FACTCAT_SF_LIVE_CONFIG not set")
    from factcat.warehouses.snowflake import SnowflakeAdapter
    from factcat_app.query import connection_from_form

    path = Path(raw).expanduser()
    if path.name == ".factcat.json":
        pytest.fail("FACTCAT_SF_LIVE_CONFIG must not be a working .factcat.json")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if str(cfg.get("kind") or "").lower() != "snowflake":
        pytest.fail("FACTCAT_SF_LIVE_CONFIG must have kind = snowflake")
    _check(SnowflakeAdapter(**connection_from_form(cfg)), "snowflake")
