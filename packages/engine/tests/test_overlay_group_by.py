"""Two event series with one chart-wide group-by. Ground truth is counted by
hand from the fixture below.

Mutation: give each arm its own top-N pick (drop ``top_labels_sql`` in the
overlay wrapper) and the shared-label test goes red: started keeps FR and
completed keeps DE, the countries each ranked alone.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from factcat_app.query import WAREHOUSE_KINDS, events_sql_from_form, fill_cyclic_buckets

# country counts per event, one day. Combined: US 6, UK 5, FR 4, DE 3, so the
# shared top 2 is US and UK. Alone, started would pick US, FR and completed
# UK, DE. Each event also has one row with no country.
COUNTS = {
    "started": {"US": 5, "FR": 4, "UK": 1, None: 1},
    "completed": {"UK": 4, "DE": 3, "US": 1, None: 1},
}


@pytest.fixture()
def con(monkeypatch) -> duckdb.DuckDBPyConnection:
    # DuckDB is the reference dialect, not an adapter: admitted for this file only.
    monkeypatch.setattr("factcat_app.query.WAREHOUSE_KINDS", (*WAREHOUSE_KINDS, "duckdb"))
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE main.events ("
        "  subscription_id VARCHAR, event_name VARCHAR,"
        "  occurred_at TIMESTAMP, country VARCHAR)"
    )
    # Relative to the run: the form's lookback is measured against today.
    day = date.today() - timedelta(days=1)
    rows = []
    for event, by_country in COUNTS.items():
        for country, n in by_country.items():
            for i in range(n):
                rows.append((f"{event}-{country}-{i}", event, f"{day} 12:00:00", country))
    connection.executemany("INSERT INTO main.events VALUES (?, ?, ?, ?)", rows)
    return connection


def _form(**extra) -> dict:
    base = {
        "kind": "duckdb",
        "table": "memory.main.events",
        "entity": "subscription_id",
        "event_time": "occurred_at",
        "event_time_tz": "reporting",
        "event_column": "event_name",
        "measure": "total",
        "grain": "day",
        "lookback_days": 7,
        "top_n": 2,
        # The approximate sketch can miss a close count on rows this few.
        "exact": True,
        "breakdown_column": "country",
        "series": [{"event": "started"}, {"event": "completed"}],
    }
    base.update(extra)
    return base


def _run(con, form) -> list[dict]:
    cur = con.execute(events_sql_from_form(form))
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def test_both_series_split_by_one_shared_country_list(con):
    rows = _run(con, _form())
    got = {(r["series"], r["country"]): r["value"] for r in rows}
    assert got == {
        ("started", "US"): 5,
        ("started", "UK"): 1,
        ("started", "(other)"): 4,
        ("started", None): 1,
        ("completed", "US"): 1,
        ("completed", "UK"): 4,
        ("completed", "(other)"): 3,
        ("completed", None): 1,
    }


def test_rows_keep_series_and_group_as_separate_columns(con):
    rows = _run(con, _form())
    assert [k for k in rows[0] if k not in ("bucket", "value")] == ["series", "country"]
    assert {r["series"] for r in rows} == {"started", "completed"}


def test_without_other_only_the_shared_countries_remain(con):
    rows = _run(con, _form(include_other=False))
    assert {(r["series"], r["country"]) for r in rows} == {
        ("started", "US"), ("started", "UK"), ("started", None),
        ("completed", "US"), ("completed", "UK"), ("completed", None),
    }


def test_per_series_mode_still_ranks_each_card_alone(con):
    rows = _run(
        con,
        _form(
            breakdown_column="",
            breakdown_by_series=True,
            series=[
                {"event": "started", "breakdown_column": "country"},
                {"event": "completed", "breakdown_column": "country"},
            ],
        ),
    )
    got = {(r["series"], r["country"]) for r in rows}
    assert ("started", "FR") in got
    assert ("completed", "DE") in got


def test_weekday_fill_keeps_one_line_per_series_and_group():
    rows = [
        {"bucket": "0", "series": "started", "country": "US", "value": 5},
        {"bucket": "0", "series": "started", "country": "UK", "value": 1},
    ]
    filled = fill_cyclic_buckets(rows, {"grain": "day_of_week"})
    assert len(filled) == 14
    assert {(r["series"], r["country"]) for r in filled} == {
        ("started", "US"), ("started", "UK"),
    }
