"""Cross-dialect emission.

The prototype these tests came from failed exactly one dialect - Redshift, with
``Unsupported EXPLODE() function`` - because the period grid used DuckDB's
``GENERATE_SERIES``. sqlglot logs that as a warning rather than raising, so the
broken SQL was emitted silently. These tests turn that warning into a failure.
"""

from __future__ import annotations

import logging

import pytest

from factcat import (
    SUPPORTED,
    EventsSpec,
    FunnelSpec,
    RetentionSpec,
    events_sql,
    funnel_sql,
    retention_sql,
)
from factcat._emit import GRID_RELATION, transpile_with_grid

RETENTION = RetentionSpec(
    table="payments",
    entity="subscription_id",
    entity_time="sub_start",
    event_time="paid_at",
    period_days=35,
    n_periods=2,
    retained="status = 'collected' AND within_period_offset <= 5",
)

FUNNEL = FunnelSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    steps=("event_name = 'view'", "event_name = 'cart'"),
    within_days=7,
)

EVENTS = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="uniques",
    where="event_name = 'view'",
)

EVENTS_AVG = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    on="property",
    measure="average",
    of="amount",
)

EVENTS_MEDIAN = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    on="property",
    measure="median",
    of="amount",
)

EVENTS_MEDIAN_EXACT = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    on="property",
    measure="median",
    of="amount",
    exact=True,
)

EVENTS_UNIQUES_EXACT = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="uniques",
    exact=True,
)

EVENTS_DISTINCT = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    on="property",
    measure="distinct",
    of="country",
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


@pytest.mark.parametrize("dialect", SUPPORTED)
def test_retention_emits_without_warnings(dialect, sqlglot_warnings):
    sql = retention_sql(RETENTION, dialect=dialect)

    assert sqlglot_warnings.messages == [], (
        f"sqlglot warned while emitting for {dialect}: {sqlglot_warnings.messages}"
    )
    assert GRID_RELATION in sql, "the period grid CTE was not attached"
    assert "GENERATE_SERIES" not in sql.upper() or dialect in ("duckdb", "postgres")


@pytest.mark.parametrize("dialect", SUPPORTED)
def test_funnel_emits_without_warnings(dialect, sqlglot_warnings):
    funnel_sql(FUNNEL, dialect=dialect)

    assert sqlglot_warnings.messages == [], (
        f"sqlglot warned while emitting for {dialect}: {sqlglot_warnings.messages}"
    )


@pytest.mark.parametrize("dialect", SUPPORTED)
def test_events_emits_without_warnings(dialect, sqlglot_warnings):
    events_sql(EVENTS, dialect=dialect)
    events_sql(EVENTS_AVG, dialect=dialect)
    events_sql(EVENTS_MEDIAN, dialect=dialect)
    events_sql(EVENTS_MEDIAN_EXACT, dialect=dialect)
    events_sql(EVENTS_DISTINCT, dialect=dialect)
    events_sql(EVENTS_UNIQUES_EXACT, dialect=dialect)

    assert sqlglot_warnings.messages == [], (
        f"sqlglot warned while emitting for {dialect}: {sqlglot_warnings.messages}"
    )


def test_bigquery_exact_median_uses_percentile_cont():
    sql = events_sql(EVENTS_MEDIAN_EXACT, dialect="bigquery")
    assert "PERCENTILE_CONT" in sql.upper()
    assert "OVER" in sql.upper()
    assert "APPROX_QUANTILES" not in sql.upper()


def test_bigquery_approx_median_uses_approx_quantiles():
    sql = events_sql(EVENTS_MEDIAN, dialect="bigquery")
    assert "APPROX_QUANTILES" in sql.upper()
    assert "PERCENTILE_CONT" not in sql.upper()


def test_bigquery_approx_uniques_uses_approx_count_distinct():
    sql = events_sql(EVENTS, dialect="bigquery")
    assert "APPROX_COUNT_DISTINCT" in sql.upper()
    assert "COUNT(DISTINCT" not in sql.upper().replace("APPROX_COUNT_DISTINCT", "")


def test_bigquery_exact_uniques_uses_count_distinct():
    sql = events_sql(EVENTS_UNIQUES_EXACT, dialect="bigquery")
    assert "COUNT(DISTINCT" in sql.upper()
    assert "APPROX_COUNT_DISTINCT" not in sql.upper()


def test_postgres_approx_uniques_falls_back_to_count_distinct():
    sql = events_sql(EVENTS, dialect="postgres")
    assert "COUNT(DISTINCT" in sql.upper()


def test_duckdb_exact_median_uses_median():
    sql = events_sql(EVENTS_MEDIAN_EXACT, dialect="duckdb")
    assert "median(fc_of)" in sql.lower()


@pytest.mark.parametrize("dialect", SUPPORTED)
def test_grid_defines_the_relation_the_query_joins(dialect):
    """The grid CTE must be named exactly what the query references."""
    sql = retention_sql(RETENTION, dialect=dialect)
    assert sql.lstrip().upper().startswith("WITH " + GRID_RELATION.upper())


def test_period_grid_row_counts_are_correct():
    """The grid must be inclusive of both ends: 0..n is n+1 rows."""
    import duckdb

    from factcat import period_grid

    for n in (0, 1, 5, 20):
        rows = duckdb.connect().execute(period_grid(n, "duckdb")).fetchall()
        assert [r[0] for r in rows] == list(range(n + 1))


def test_redshift_falls_back_to_a_union_grid():
    """Redshift has no reachable row generator, so it gets an explicit union."""
    from factcat import period_grid

    grid = period_grid(2, "redshift")
    assert grid.count("UNION ALL") == 2
    assert "GENERATE_SERIES" not in grid.upper()


def test_missing_with_clause_is_loud_not_silent():
    """A query the grid cannot attach to must raise, never emit broken SQL."""
    with pytest.raises(RuntimeError, match="WITH clause"):
        transpile_with_grid("SELECT 1 AS x", "duckdb", 2)
