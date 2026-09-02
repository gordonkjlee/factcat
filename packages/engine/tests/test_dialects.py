"""Cross-dialect emission.

The prototype these tests came from failed exactly one dialect - Redshift, with
``Unsupported EXPLODE() function`` - because the period grid used DuckDB's
``GENERATE_SERIES``. sqlglot logs that as a warning rather than raising, so the
broken SQL was emitted silently. These tests turn that warning into a failure.
"""

from __future__ import annotations

import json
import logging

import re

import pytest

from factcat import (
    SUPPORTED,
    Breakdown,
    EventsSpec,
    FunnelSpec,
    RetentionSpec,
    events_sql,
    funnel_sql,
    retention_sql,
)
from factcat._emit import GRID_RELATION, transpile_with_grid
from factcat.dialects import (
    as_instant,
    create_or_replace_relation,
    set_relation_comment,
    hour_trunc,
    period_start_shifted,
    splice_placeholders,
    timestamp_at_date,
)

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

EVENTS_BREAKDOWN = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=("country",),
    breakdown_labels=("country",),
    top_n=8,
)

EVENTS_BREAKDOWN_APPROX = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=False,
    breakdowns=("country",),
    top_n=8,
)

EVENTS_BREAKDOWN_SUM_APPROX = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    on="property",
    measure="sum",
    of="amount",
    exact=False,
    breakdowns=("country",),
    top_n=8,
)

EVENTS_BREAKDOWN_PAIR = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=False,
    breakdowns=("country", "browser"),
    top_n=8,
)

EVENTS_BREAKDOWN_TRIPLE = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=("country", "browser", "plan"),
    breakdown_labels=("country", "browser", "plan"),
    top_n=8,
)

# Value-semantics specs. FIRST closes an old gap: the attr CTE was never
# in the walk. CARRIED and friends walk the counting-trick stream (there
# is no portable IGNORE NULLS; sqlglot silently strips it for Postgres).
EVENTS_BREAKDOWN_FIRST = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=("country",),
    breakdown_at="first",
    top_n=8,
)

EVENTS_BREAKDOWN_CARRIED = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        # own_value_first walks the COALESCE(fc_self, ...) shape too;
        # CARRIED_PAIR below keeps the plain shape in the walk.
        Breakdown(
            "plan",
            at="carried",
            fill_from="event_name = 'plan_set'",
            own_value_first=True,
        ),
    ),
    top_n=8,
)

EVENTS_BREAKDOWN_CARRIED_PAIR = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        Breakdown("plan", at="carried"),
        Breakdown("country", at="carried"),
    ),
    top_n=8,
)

EVENTS_BREAKDOWN_ASOF = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        Breakdown("plan", at="last", until="TIMESTAMP '2026-01-01'"),
    ),
    top_n=8,
)

EVENTS_BREAKDOWN_ASOF_BACKFILL = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        # before (strict) + backfill: the range-end shape the app emits.
        Breakdown(
            "plan", at="last", before="TIMESTAMP '2026-02-01'", backfill=True
        ),
    ),
    top_n=8,
)

EVENTS_BREAKDOWN_WINDOWED_FIRST = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        Breakdown(
            "plan",
            at="first",
            since="TIMESTAMP '2026-01-01'",
            until="TIMESTAMP '2026-02-01'",
        ),
    ),
    top_n=8,
)

EVENTS_BREAKDOWN_MIXED = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        Breakdown("plan", at="carried"),
        "country",
        Breakdown("browser", at="last"),
    ),
    top_n=8,
)

# The two property-measure composition specs below are compile-only in
# this walk; distinct+carried also executes on DuckDB in
# test_events_breakdowns, median+carried is transpile-covered only (the
# median splice is dialect-specific and has no portable execute fixture).
EVENTS_BREAKDOWN_CARRIED_MEDIAN = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    on="property",
    measure="median",
    of="amount",
    breakdowns=(Breakdown("plan", at="carried"),),
    top_n=8,
)

EVENTS_BREAKDOWN_CARRIED_DISTINCT = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    on="property",
    measure="distinct",
    of="country",
    breakdowns=(Breakdown("plan", at="carried"),),
    top_n=8,
)

# values_table specs (item 12): a column's recorded values read from a
# relation plus the live tail after the watermark; a complete relation
# (no watermark) skips the table for that column; event_time_column keeps
# the tail bound on the stored column so partitions prune.
EVENTS_BREAKDOWN_VALUES = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        Breakdown(
            "plan",
            at="carried",
            fill_from="event_name = 'plan_set'",
            own_value_first=True,
            values_table="plan_values",
            values_watermark="TIMESTAMP '2026-01-09'",
        ),
    ),
    top_n=8,
)

EVENTS_BREAKDOWN_VALUES_COMPLETE_PAIR = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        Breakdown("plan", at="carried", values_table="plan_values"),
        Breakdown("country", at="carried"),
    ),
    top_n=8,
)

EVENTS_BREAKDOWN_VALUES_ASOF = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        Breakdown(
            "plan",
            at="last",
            before="TIMESTAMP '2026-02-01'",
            backfill=True,
            values_table="plan_values",
            values_watermark="TIMESTAMP '2026-01-09'",
        ),
    ),
    top_n=8,
)

EVENTS_BREAKDOWN_VALUES_RAW_COLUMN = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="CAST(occurred_at AS TIMESTAMP)",
    event_time_column="occurred_at",
    measure="total",
    exact=True,
    breakdowns=(
        Breakdown(
            "plan",
            at="carried",
            values_table="plan_values",
            values_watermark="TIMESTAMP '2026-01-09'",
        ),
    ),
    top_n=8,
)

# Regression: rows-mode distinct + breakdown emitted a missing comma
# before per_entity; nothing walked the combination.
EVENTS_BREAKDOWN_DISTINCT = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    on="property",
    measure="distinct",
    of="country",
    breakdowns=("plan",),
    top_n=8,
)

EVENTS_WEEK = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="uniques",
    bucket=(
        "CAST(factcat_period_start_shifted("
        "occurred_at, 'week', 'monday', 0) AS DATE)"
    ),
    where=(
        "occurred_at >= factcat_period_start_shifted("
        "current_date, 'week', 'monday', 0)"
    ),
)

EVENTS_HOUR = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="uniques",
    bucket="factcat_hour_trunc(fc_event_ts, 'UTC', 'utc')",
)

EVENTS_DOW = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    bucket="factcat_dow(fc_event_ts, 'UTC', 'utc')",
)

EVENTS_HOD = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="total",
    bucket="factcat_hour_of_day(fc_event_ts, 'UTC', 'utc')",
)

EVENTS_TZ = EventsSpec(
    table="events",
    entity="entity_id",
    event_time="occurred_at",
    measure="uniques",
    bucket=(
        "CAST(factcat_period_start_shifted("
        "occurred_at, 'week', 'monday', 0, 'Europe/Berlin', 'utc') AS DATE)"
    ),
    where=(
        "occurred_at >= factcat_period_start_shifted("
        "current_date, 'week', 'monday', 0, 'Europe/Berlin', 'utc')"
    ),
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
    events_sql(EVENTS_WEEK, dialect=dialect)
    events_sql(EVENTS_TZ, dialect=dialect)
    events_sql(EVENTS_HOUR, dialect=dialect)
    events_sql(EVENTS_DOW, dialect=dialect)
    events_sql(EVENTS_HOD, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_APPROX, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_SUM_APPROX, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_PAIR, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_TRIPLE, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_FIRST, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_CARRIED, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_CARRIED_PAIR, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_ASOF, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_ASOF_BACKFILL, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_WINDOWED_FIRST, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_MIXED, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_CARRIED_MEDIAN, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_CARRIED_DISTINCT, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_DISTINCT, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_VALUES, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_VALUES_COMPLETE_PAIR, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_VALUES_ASOF, dialect=dialect)
    events_sql(EVENTS_BREAKDOWN_VALUES_RAW_COLUMN, dialect=dialect)

    assert sqlglot_warnings.messages == [], (
        f"sqlglot warned while emitting for {dialect}: {sqlglot_warnings.messages}"
    )


def test_carried_shares_one_value_scan():
    """Two carried columns must not scan the table twice for their values:
    the raw table appears exactly twice (src, fc_values)."""
    sql = events_sql(EVENTS_BREAKDOWN_CARRIED_PAIR, dialect="duckdb")
    assert sql.count("FROM events") == 2


def test_values_table_replaces_the_live_scan():
    """A complete relation (no watermark): the table is scanned once (src)
    and the column's values come from the relation. With a watermark the
    live tail is one more scan — shared with any live carried column, so a
    pair with one complete relation still scans the table twice, not three
    times."""
    complete = events_sql(
        EventsSpec(
            table="events",
            entity="entity_id",
            event_time="occurred_at",
            measure="total",
            exact=True,
            breakdowns=(Breakdown("plan", at="carried", values_table="plan_values"),),
        ),
        dialect="duckdb",
    )
    assert complete.count("FROM events") == 1
    assert "FROM plan_values" in complete
    pair = events_sql(EVENTS_BREAKDOWN_VALUES_COMPLETE_PAIR, dialect="duckdb")
    assert pair.count("FROM events") == 2
    tailed = events_sql(EVENTS_BREAKDOWN_VALUES, dialect="duckdb")
    assert tailed.count("FROM events") == 2
    asof = events_sql(EVENTS_BREAKDOWN_VALUES_ASOF, dialect="duckdb")
    # attr + its backfill twin each read the relation and the tail.
    assert asof.count("FROM plan_values") == 2
    assert asof.count("FROM events") == 3


def test_bigquery_values_tail_bound_is_on_the_stored_column():
    """event_time_column keeps the watermark comparison on the bare
    column: a function around the partition column defeats pruning, and
    the tail exists to be cheap."""
    sql = events_sql(EVENTS_BREAKDOWN_VALUES_RAW_COLUMN, dialect="bigquery")
    assert re.search(r"\boccurred_at\s*\)?\s*>\s*", sql)
    assert not re.search(r"CAST\(occurred_at AS \w+\)\s*\)?\s*>", sql)


def test_bigquery_carried_has_no_ignore_nulls_and_clean_windows():
    """The counting trick, not LAST_VALUE IGNORE NULLS (Postgres lacks it;
    sqlglot strips it silently). BigQuery forbids NULLS LAST inside
    aggregate windows; explicit NULLS FIRST in the source keeps it out."""
    sql = events_sql(EVENTS_BREAKDOWN_CARRIED, dialect="bigquery")
    assert "IGNORE NULLS" not in sql.upper()
    start = 0
    while True:
        pos = sql.find("OVER (", start)
        if pos == -1:
            break
        depth, j = 1, pos + len("OVER (")
        while depth and j < len(sql):
            depth += {"(": 1, ")": -1}.get(sql[j], 0)
            j += 1
        assert "NULLS LAST" not in sql[pos:j]
        start = j


def test_hour_trunc_rejects_non_iana_timezone():
    with pytest.raises(ValueError, match="IANA"):
        hour_trunc("occurred_at", "bigquery", "UTC'); DROP TABLE t; --", "utc")


def test_snowflake_hour_placeholders_are_spliced():
    hour = events_sql(EVENTS_HOUR, dialect="snowflake")
    assert "FACTCAT_" not in hour.upper()
    assert "DATE_TRUNC" in hour.upper()
    dow = events_sql(EVENTS_DOW, dialect="snowflake")
    assert "FACTCAT_" not in dow.upper()
    assert "DAYOFWEEKISO" in dow.upper()
    hod = events_sql(EVENTS_HOD, dialect="snowflake")
    assert "FACTCAT_" not in hod.upper()
    assert "HOUR(" in hod.upper().replace(" ", "")


def test_splice_uppercase_hour_trunc_on_snowflake():
    sql = splice_placeholders(
        "FACTCAT_HOUR_TRUNC(fc_event_ts, 'UTC', 'utc')",
        "snowflake",
    )
    assert "FACTCAT_" not in sql.upper()
    assert "DATE_TRUNC" in sql.upper()


def test_hours_ago_reporting_is_civil_on_bigquery():
    sql = splice_placeholders(
        "factcat_hours_ago(24, 'Europe/Berlin', 'reporting')",
        "bigquery",
    )
    assert "CURRENT_DATETIME('Europe/Berlin')" in sql
    assert "DATETIME_SUB" in sql
    bound = splice_placeholders(
        "factcat_current_hour_start('Europe/Berlin', 'utc')",
        "bigquery",
    )
    assert "TIMESTAMP(" in bound
    assert "CURRENT_DATETIME('Europe/Berlin')" in bound


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


def test_bigquery_week_start_monday_is_explicit():
    sql = events_sql(EVENTS_WEEK, dialect="bigquery")
    assert "WEEK(MONDAY)" in sql.upper().replace(" ", "")
    assert "factcat_period_start_shifted" not in sql
    assert "DATE(CAST(occurred_at AS TIMESTAMP), 'UTC')" in sql.replace("`", "")


def test_four_arg_placeholder_defaults_to_utc():
    sql = splice_placeholders(
        "CAST(factcat_period_start_shifted("
        "occurred_at, 'week', 'monday', 0) AS DATE)",
        "bigquery",
    )
    assert sql == (
        "CAST(DATE_TRUNC(DATE(CAST(occurred_at AS TIMESTAMP), 'UTC'), WEEK(MONDAY)) AS DATE)"
    )


def test_six_arg_placeholder_uses_reporting_zone():
    sql = splice_placeholders(
        "factcat_period_start_shifted("
        "occurred_at, 'day', 'monday', 0, 'Europe/Berlin', 'utc')",
        "bigquery",
    )
    assert sql == "DATE(CAST(occurred_at AS TIMESTAMP), 'Europe/Berlin')"


def test_as_instant_survives_transpile():
    sql = splice_placeholders(
        "factcat_as_instant(occurred_at) >= TIMESTAMP('2026-01-01')",
        "bigquery",
    )
    assert sql == "CAST(occurred_at AS TIMESTAMP) >= TIMESTAMP('2026-01-01')"


def test_utc_bound_is_datetime_so_column_stays_bare():
    sql = splice_placeholders(
        "occurred_at >= factcat_ts_at_date(DATE '2026-01-01', 'UTC', 'utc')",
        "bigquery",
    )
    assert sql == "occurred_at >= DATETIME(TIMESTAMP(DATE '2026-01-01', 'UTC'))"
    assert timestamp_at_date("DATE '2026-01-01'", "bigquery", "UTC", "utc") == (
        "DATETIME(TIMESTAMP(DATE '2026-01-01', 'UTC'))"
    )
    sf = timestamp_at_date("DATE '2026-01-01'", "snowflake", "UTC", "utc")
    assert "TIMESTAMP_NTZ" in sf
    instant = timestamp_at_date("DATE '2026-01-01'", "bigquery", "UTC", "instant")
    assert instant.startswith("TIMESTAMP(")
    sf_spliced = splice_placeholders(
        "occurred_at >= FACTCAT_TS_AT_DATE(DATE '2026-01-01', 'UTC', 'utc')",
        "snowflake",
    )
    assert "FACTCAT_" not in sf_spliced.upper()
    assert "TIMESTAMP_NTZ" in sf_spliced.upper()


def test_iana_storage_zone_bound_is_datetime_in_that_zone():
    sql = splice_placeholders(
        "occurred_at >= factcat_ts_at_date(DATE '2026-01-01', 'Europe/London', 'America/New_York')",
        "bigquery",
    )
    assert sql == (
        "occurred_at >= DATETIME(TIMESTAMP(DATE '2026-01-01', 'Europe/London'), "
        "'America/New_York')"
    )
    instant = splice_placeholders(
        "factcat_as_instant(occurred_at, 'America/New_York')",
        "bigquery",
    )
    assert instant == "TIMESTAMP(occurred_at, 'America/New_York')"
    sf = timestamp_at_date(
        "DATE '2026-01-01'", "snowflake", "Europe/London", "America/New_York"
    )
    assert "CONVERT_TIMEZONE('Europe/London', 'America/New_York'" in sf


def test_create_or_replace_relation_spelling():
    select = "SELECT event_name AS fc_value FROM t GROUP BY 1"
    mv = create_or_replace_relation("d.fc_event_names", select, "bigquery", materialized=True)
    table = create_or_replace_relation("d.fc_event_names", select, "snowflake", materialized=False)
    assert mv.startswith("CREATE OR REPLACE MATERIALIZED VIEW")
    assert table.startswith("CREATE OR REPLACE TABLE")
    assert "GROUP BY 1" in mv and "GROUP BY 1" in table
    noted = create_or_replace_relation(
        "d.fc_event_names",
        select,
        "bigquery",
        materialized=True,
        comment='{"event_column":"event_name","table":"analytics.events","v":1}',
    )
    assert "OPTIONS(description=" in noted
    sf_noted = create_or_replace_relation(
        "d.fc_event_names",
        select,
        "snowflake",
        materialized=False,
        comment='{"v":1}',
    )
    assert "COMMENT =" in sf_noted


def test_bigquery_civil_datetime_casts_to_date():
    sql = period_start_shifted(
        "occurred_at", "week", "monday", 0, "bigquery", "Europe/London", "reporting"
    )
    assert sql == "DATE_TRUNC(CAST(occurred_at AS DATE), WEEK(MONDAY))"


def test_bigquery_current_date_is_zoned():
    sql = period_start_shifted(
        "current_date", "week", "monday", -1, "bigquery", "Europe/Berlin", "utc"
    )
    assert sql == (
        "DATE_SUB(DATE_TRUNC(CURRENT_DATE('Europe/Berlin'), WEEK(MONDAY)), "
        "INTERVAL 1 WEEK)"
    )


def test_unix_seconds_is_timestamp_seconds_on_bigquery():
    sql = period_start_shifted(
        "occurred_at", "day", "monday", 0, "bigquery", "Europe/Berlin", "unix_s"
    )
    assert sql == "DATE(TIMESTAMP_SECONDS(occurred_at), 'Europe/Berlin')"
    assert as_instant("occurred_at", "bigquery", "unix_s") == "TIMESTAMP_SECONDS(occurred_at)"


def test_unix_millis_is_to_timestamp_on_snowflake():
    sql = period_start_shifted(
        "occurred_at", "day", "monday", 0, "snowflake", "UTC", "unix_ms"
    )
    assert "TO_TIMESTAMP_TZ(occurred_at, 3)" in sql
    assert "CONVERT_TIMEZONE('UTC'" in sql


def test_snowflake_ntz_utc_uses_three_arg_convert():
    sql = period_start_shifted(
        "occurred_at", "day", "monday", 0, "snowflake", "Europe/Berlin", "utc"
    )
    assert sql == "CAST(CONVERT_TIMEZONE('UTC', 'Europe/Berlin', occurred_at) AS DATE)"


def test_snowflake_instant_uses_two_arg_convert():
    sql = period_start_shifted(
        "occurred_at", "day", "monday", 0, "snowflake", "Europe/Berlin", "instant"
    )
    assert sql == "CAST(CONVERT_TIMEZONE('Europe/Berlin', occurred_at) AS DATE)"
    assert "CONVERT_TIMEZONE('UTC', 'Europe/Berlin'" not in sql


def test_snowflake_ntz_reporting_is_plain_date():
    sql = period_start_shifted(
        "occurred_at", "day", "monday", 0, "snowflake", "Europe/Berlin", "reporting"
    )
    assert sql == "CAST(occurred_at AS DATE)"


def test_snowflake_week_start_is_explicit_not_session():
    sql = events_sql(EVENTS_TZ, dialect="snowflake")
    compact = sql.upper().replace(" ", "")
    assert "CONVERT_TIMEZONE('EUROPE/BERLIN'" in compact
    assert "DAYOFWEEKISO" in compact
    assert "WEEK_START" not in compact
    assert "FACTCAT_PERIOD_START_SHIFTED" not in compact
    assert "ASDATE" in compact


def test_timezone_rejects_quotes():
    with pytest.raises(ValueError, match="IANA"):
        period_start_shifted(
            "occurred_at", "day", "monday", 0, "bigquery", "UTC' --", "utc"
        )


def test_bigquery_approx_breakdown_uses_approx_top_count():
    sql = events_sql(EVENTS_BREAKDOWN_APPROX, dialect="bigquery")
    assert "APPROX_TOP_COUNT" in sql.upper()
    assert "IS NOT NULL" in sql.upper()
    sql_exact = events_sql(EVENTS_BREAKDOWN, dialect="bigquery")
    assert "APPROX_TOP_COUNT" not in sql_exact.upper()
    assert "LIMIT" in sql_exact.upper()


def test_bigquery_approx_sum_breakdown_uses_approx_top_sum():
    sql = events_sql(EVENTS_BREAKDOWN_SUM_APPROX, dialect="bigquery")
    assert "APPROX_TOP_SUM" in sql.upper()
    assert "APPROX_TOP_COUNT" not in sql.upper()
    sql_exact = events_sql(
        EventsSpec(
            table="events",
            entity="entity_id",
            event_time="occurred_at",
            on="property",
            measure="sum",
            of="amount",
            exact=True,
            breakdowns=("country",),
            top_n=8,
        ),
        dialect="bigquery",
    )
    assert "APPROX_TOP_SUM" not in sql_exact.upper()
    assert "LIMIT" in sql_exact.upper()


def test_snowflake_approx_uniques_uses_approx_count_distinct():
    sql = events_sql(EVENTS, dialect="snowflake")
    assert "APPROX_COUNT_DISTINCT" in sql.upper()
    assert "COUNT(DISTINCT" not in sql.upper().replace("APPROX_COUNT_DISTINCT", "")


def test_snowflake_approx_median_uses_approx_percentile():
    sql = events_sql(EVENTS_MEDIAN, dialect="snowflake")
    assert "approx_percentile" in sql.lower()
    sql_exact = events_sql(EVENTS_MEDIAN_EXACT, dialect="snowflake")
    assert "approx_percentile" not in sql_exact.lower()
    assert "median(" in sql_exact.lower()


def test_snowflake_approx_breakdown_uses_approx_top_k():
    sql = events_sql(EVENTS_BREAKDOWN_APPROX, dialect="snowflake")
    assert "APPROX_TOP_K" in sql.upper()
    assert "FLATTEN" in sql.upper()
    assert "IS NOT NULL" in sql.upper()
    sql_exact = events_sql(EVENTS_BREAKDOWN, dialect="snowflake")
    assert "APPROX_TOP_K" not in sql_exact.upper()
    assert "LIMIT" in sql_exact.upper()


def test_duckdb_approx_breakdown_uses_approx_top_k():
    sql = events_sql(EVENTS_BREAKDOWN_APPROX, dialect="duckdb")
    assert "approx_top_k" in sql.lower()
    sql_exact = events_sql(EVENTS_BREAKDOWN, dialect="duckdb")
    assert "approx_top_k" not in sql_exact.lower()
    assert "LIMIT" in sql_exact.upper()


def test_databricks_approx_breakdown_nests_explode_outside_aggregate():
    for dialect in ("databricks", "spark"):
        sql = events_sql(EVENTS_BREAKDOWN_APPROX, dialect=dialect)
        compact = sql.lower().replace(" ", "")
        assert "approx_top_k" in compact
        assert "explode(fc_tops)" in compact
        assert "explode(approx_top_k" not in compact


def test_clickhouse_approx_breakdown_uses_topk():
    sql = events_sql(EVENTS_BREAKDOWN_APPROX, dialect="clickhouse")
    assert "topk(" in sql.lower().replace(" ", "")


def test_pair_breakdown_stays_exact_limit():
    for dialect in ("bigquery", "snowflake", "duckdb"):
        sql = events_sql(EVENTS_BREAKDOWN_PAIR, dialect=dialect).upper()
        assert "APPROX_TOP" not in sql
        assert "LIMIT" in sql


def test_snowflake_sum_breakdown_has_no_weighted_sketch():
    sql = events_sql(EVENTS_BREAKDOWN_SUM_APPROX, dialect="snowflake")
    assert "APPROX_TOP_K" not in sql.upper()
    assert "LIMIT" in sql.upper()


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


def _as_the_warehouse_stores_it(body: str) -> str:
    """A single-quoted literal as BigQuery and Snowflake hand it back: both
    process backslash escapes inside the quotes."""
    unescape = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"', "`": "`"}
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            out.append(unescape.get(body[i + 1], "\\" + body[i + 1]))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


@pytest.mark.parametrize("dialect", sorted(SUPPORTED))
@pytest.mark.parametrize(
    "note",
    [
        json.dumps({"a": 'say "hi"'}),                       # JSON escapes an embedded quote
        json.dumps({"e": "REGEXP_EXTRACT(x, r'\\d+')"}),     # a backslash and a quote together
        json.dumps({"q": "it's"}),                           # a bare apostrophe
    ],
)
def test_a_relation_comment_survives_the_warehouses_own_unescaping(dialect, note):
    """The registry travels as JSON inside a table comment. Escaping only the
    quote loses every backslash the JSON needed: the document stops parsing,
    the registry reads back empty, and every run rebuilds an index that is
    already there. Mutation: escape only the quote in _comment_literal."""
    stmt = set_relation_comment('"d"."s"."t"', note, dialect)
    body = stmt[stmt.index("'") + 1 : stmt.rindex("'")]
    stored = _as_the_warehouse_stores_it(body)
    assert stored == note
    assert json.loads(stored)
