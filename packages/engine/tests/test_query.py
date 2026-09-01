"""Form → EventsSpec. No warehouse."""

from __future__ import annotations

import pytest

from datetime import date

from factcat import events_sql
from factcat_app.filters import FILTER_FAMILY_OPS, FILTER_OP_META
from factcat_app.query import (
    EVENT_VALUE_LIMIT,
    annotate_incomplete,
    catalog_lookback_days,
    event_name_cache_rebuild_sql,
    event_values_sql,
    events_sql_from_form,
    fill_cyclic_buckets,
    job_bytes_cap,
    query_row_limit,
    spec_from_form,
    write_cache_table,
)


def _form(**overrides):
    base = dict(
        table="analytics.events",
        entity="subscription_id",
        event_time="occurred_at",
        measure="uniques",
        grain="day",
        lookback_days=30,
        exact=False,
    )
    base.update(overrides)
    return base


def _shifted(expr, unit, n=0, week_start="monday", tz="UTC", kind="utc"):
    return (
        f"factcat_period_start_shifted({expr}, '{unit}', "
        f"'{week_start}', {n}, '{tz}', '{kind}')"
    )


def _ts(date_sql, tz="UTC", kind="utc"):
    return f"factcat_ts_at_date({date_sql}, '{tz}', '{kind}')"


def test_empty_entity_is_rejected():
    with pytest.raises(ValueError, match="entity is required"):
        spec_from_form(_form(entity=""))


def test_does_not_default_entity_to_user_id():
    spec = spec_from_form(_form())
    assert spec.entity == "subscription_id"
    omitted = _form()
    del omitted["entity"]
    with pytest.raises(ValueError, match="entity is required"):
        spec_from_form(omitted)
    with pytest.raises(ValueError, match="entity is required"):
        spec_from_form(_form(entity="   "))


def test_week_bucket_uses_explicit_week_start():
    spec = spec_from_form(_form(grain="week"))
    assert _shifted("fc_event_ts", "week") in spec.bucket
    sql = events_sql(spec, dialect="bigquery")
    assert "WEEK(MONDAY)" in sql.upper().replace(" ", "")
    assert "factcat_period_start_shifted" not in sql
    sun = spec_from_form(_form(grain="week", week_start="sunday"))
    assert "WEEK(SUNDAY)" in events_sql(sun, dialect="bigquery").upper().replace(" ", "")


def test_range_preset_7_is_last_n_days():
    spec = spec_from_form(_form(range_preset="7"))
    assert (
        f"occurred_at >= {_ts(_shifted('current_date', 'day', -7))}"
        in spec.where
    )


def test_this_month_is_anchored_date_trunc():
    spec = spec_from_form(_form(range_mode="this", range_unit="month", grain="month"))
    assert _shifted("current_date", "month") in spec.where
    sql = events_sql(spec, dialect="bigquery").upper()
    assert "MONTH" in sql


def test_last_week_is_previous_complete_week():
    spec = spec_from_form(
        _form(range_mode="previous", range_unit="week", grain="week")
    )
    assert _shifted("current_date", "week", -1) in spec.where
    assert _shifted("current_date", "week", 0) in spec.where


def test_today_is_current_calendar_day():
    spec = spec_from_form(_form(range_mode="this", range_unit="day", grain="day"))
    assert _shifted("current_date", "day") in spec.where


def test_this_week_by_day_keeps_week_window():
    spec = spec_from_form(_form(grain="day", range_mode="this", range_unit="week"))
    assert _shifted("current_date", "week") in spec.where
    assert spec.bucket == f"CAST({_shifted('fc_event_ts', 'day')} AS DATE)"


def test_week_grain_this_day_bumps_to_this_week():
    spec = spec_from_form(_form(grain="week", range_mode="this", range_unit="day"))
    assert _shifted("current_date", "week") in spec.where


def test_week_grain_rejects_day_window():
    spec = spec_from_form(
        _form(grain="week", range_mode="last", range_n=30, range_unit="day")
    )
    assert _shifted("current_date", "week", -8) in spec.where
    assert _shifted("current_date", "week", 0) in spec.where
    assert "current_date - 30" not in spec.where


def test_last_eight_weeks_are_complete_by_default():
    spec = spec_from_form(
        _form(grain="week", range_mode="last", range_n=8, range_unit="week")
    )
    assert _shifted("current_date", "week", -8) in spec.where
    assert _shifted("current_date", "week", 0) in spec.where


def test_last_days_include_today_by_default():
    spec = spec_from_form(_form(grain="day", range_mode="last", range_n=30, range_unit="day"))
    assert _shifted("current_date", "day", -30) in spec.where
    assert f"{spec.event_time} < " not in spec.where


def test_last_days_can_exclude_today():
    spec = spec_from_form(
        _form(
            grain="day",
            range_mode="last",
            range_n=30,
            range_unit="day",
            include_current=False,
        )
    )
    assert _shifted("current_date", "day", -30) in spec.where
    assert _shifted("current_date", "day", 0) in spec.where
    assert "<" in spec.where


def test_include_current_week_keeps_partial_trailing():
    spec = spec_from_form(
        _form(
            grain="week",
            range_mode="last",
            range_n=8,
            range_unit="week",
            include_current=True,
        )
    )
    assert _shifted("current_date", "week", -7) in spec.where
    assert _shifted("current_date", "week", 0) not in spec.where


def test_relative_custom_weeks_are_inclusive_endpoints():
    spec = spec_from_form(
        _form(
            grain="week",
            range_mode="custom",
            custom_kind="relative",
            rel_start_n=12,
            rel_end_n=3,
        )
    )
    assert _shifted("current_date", "week", -12) in spec.where
    assert _shifted("current_date", "week", -2) in spec.where


def test_relative_custom_from_must_not_be_after_to():
    with pytest.raises(ValueError, match="at least as far back"):
        spec_from_form(
            _form(
                grain="day",
                range_mode="custom",
                custom_kind="relative",
                rel_start_n=3,
                rel_end_n=12,
            )
        )


def test_custom_week_snaps_to_week_starts():
    spec = spec_from_form(
        _form(
            grain="week",
            range_mode="custom",
            start_date="2026-01-07",
            end_date="2026-01-20",
        )
    )
    # Wed 7 Jan → week of Mon 5 Jan; Tue 20 Jan → week ending Sun 25 Jan.
    assert "DATE '2026-01-05'" in spec.where
    assert "DATE '2026-01-26'" in spec.where


def test_custom_week_snaps_sunday_start():
    spec = spec_from_form(
        _form(
            grain="week",
            week_start="sunday",
            range_mode="custom",
            start_date="2026-01-07",
            end_date="2026-01-20",
        )
    )
    assert "DATE '2026-01-04'" in spec.where
    assert "DATE '2026-01-25'" in spec.where


def test_annotate_incomplete_marks_current_grain():
    rows = annotate_incomplete(
        [
            {"bucket": "2026-08-17", "value": 3},
            {"bucket": "2026-08-24", "value": 1},
        ],
        _form(grain="week", range_mode="this", range_unit="week"),
        today=date(2026, 8, 28),
    )
    assert rows[0]["incomplete"] is False
    assert rows[1]["incomplete"] is True


def test_annotate_incomplete_skips_complete_last_weeks():
    rows = annotate_incomplete(
        [{"bucket": "2026-08-17", "value": 3}],
        _form(grain="week", range_mode="last", range_n=8, range_unit="week"),
        today=date(2026, 8, 28),
    )
    assert rows[0]["incomplete"] is False


def test_last_weeks_exclude_current():
    spec = spec_from_form(
        _form(
            grain="week",
            range_mode="last",
            range_n=5,
            range_unit="week",
            exclude_current=True,
        )
    )
    assert _shifted("current_date", "week", -5) in spec.where
    assert _shifted("current_date", "week", 0) in spec.where


def test_events_sql_from_form_limits_most_recent_buckets():
    sql = events_sql_from_form(_form(query_row_limit=12))
    compact = " ".join(sql.split()).upper()
    assert "LIMIT 12" in compact
    assert "ORDER BY CAST(BUCKET AS DATE) DESC" in compact
    assert compact.index("LIMIT 12") < compact.rindex("ORDER BY CAST(BUCKET AS DATE)")
    assert sql.startswith("SELECT * FROM (")
    assert "\n    WITH src AS (" in sql
    assert "AS _fc_inner" in sql
    assert "AS _fc_recent" in sql
    assert "ORDER BY CAST(bucket AS DATE) DESC" in sql
    assert sql.strip().endswith("ORDER BY CAST(bucket AS DATE)")


def test_query_row_limit_run_overrides_setup():
    assert query_row_limit(_form(query_row_limit=5000, query_row_limit_run=10000)) == 10000
    assert query_row_limit(_form(query_row_limit=5000)) == 5000


def test_query_row_limit_default_has_no_max():
    assert query_row_limit(_form()) == 1_000_000
    assert query_row_limit(_form(query_row_limit_run=10**9)) == 10**9
    with pytest.raises(ValueError, match="at least 1"):
        query_row_limit(_form(query_row_limit=0))


def test_job_bytes_cap_default_is_ten_gib():
    assert job_bytes_cap(_form()) == 10 * 1024**3


def test_job_bytes_cap_override_uses_report_gb():
    assert job_bytes_cap(_form(override_cap=True, bytes_cap_override_gb=20)) == 20 * 1024**3


def test_catalog_event_values_uses_recent_window():
    sql = event_values_sql(_form(event_column="event_name", catalog=True))
    compact = " ".join(sql.split()).upper()
    assert "DISTINCT" in compact
    assert "CURRENT_DATE('UTC')" in sql
    assert "DATE_SUB" in compact
    assert "90" in compact
    assert "OCCURRED_AT" in compact
    assert "CAST(OCCURRED_AT" not in compact
    assert "DATETIME(TIMESTAMP(" in sql.replace(" ", "").replace("`", "")


def test_catalog_lookback_zero_is_all_time():
    sql = event_values_sql(
        _form(event_column="event_name", catalog=True, catalog_lookback_days=0)
    )
    upper = sql.upper()
    assert "DATE_SUB" not in upper
    assert "OCCURRED_AT" not in upper
    assert catalog_lookback_days(_form(catalog_lookback_days=0)) is None


def test_catalog_lookback_override():
    sql = event_values_sql(
        _form(event_column="event_name", catalog=True, catalog_lookback_days=365)
    )
    assert "365" in sql
    assert catalog_lookback_days(_form(catalog_lookback_days=365)) == 365


def test_snowflake_window_isolates_ntz_column():
    sql = events_sql_from_form(
        _form(
            kind="snowflake",
            table="ANALYTICS.MARTS.EVENTS",
            event_time_tz="utc",
        )
    )
    compact = sql.replace(" ", "").upper()
    assert "CAST(OCCURRED_AT ASTIMESTAMP)>=" not in compact
    assert "OCCURRED_AT>=" in compact
    assert "FACTCAT_" not in compact
    assert "TIMESTAMP_NTZ" in compact or "CONVERT_TIMEZONE" in compact


def test_write_cache_table_none_when_unset():
    assert write_cache_table(_form()) is None
    assert write_cache_table(_form(write_dataset="analytics")) is None
    assert write_cache_table(_form(write_project="p")) is None
    assert write_cache_table(_form(kind="snowflake", table="ANALYTICS.MARTS.EVENTS")) is None
    assert write_cache_table(
        _form(kind="snowflake", table="ANALYTICS.MARTS.EVENTS", write_schema="MARTS")
    ) is None


def test_write_cache_table_does_not_use_billing_project():
    assert write_cache_table(_form(project="billing", write_dataset="analytics")) is None


def test_write_cache_table_bigquery_and_snowflake():
    bq = write_cache_table(
        _form(project="billing", write_project="dest-proj", write_dataset="analytics")
    )
    assert "fc_event_names" in bq
    assert "analytics" in bq
    assert "dest-proj" in bq
    assert "billing" not in bq
    sf = write_cache_table(
        _form(
            kind="snowflake",
            table="ANALYTICS.MARTS.EVENTS",
            database="EVENTS_DB",
            write_database="ANALYTICS",
            write_schema="MARTS",
        )
    )
    assert "fc_event_names" in sf
    assert "ANALYTICS" in sf or "analytics" in sf.lower()
    assert "MARTS" in sf or "marts" in sf.lower()


def test_event_name_cache_rebuild_is_group_by():
    sql = event_name_cache_rebuild_sql(
        _form(
            project="p",
            write_project="dest-proj",
            write_dataset="analytics",
            event_column="event_name",
        ),
        materialized=True,
    )
    upper = sql.upper()
    assert "CREATE OR REPLACE MATERIALIZED VIEW" in upper
    assert "GROUP BY" in upper
    assert "DISTINCT" not in upper
    assert "DATE_SUB" not in upper
    assert "FACTCAT_" not in upper
    hyphen = event_name_cache_rebuild_sql(
        _form(
            project="p",
            write_project="my-gcp-proj",
            write_dataset="analytics",
            event_column="event_name",
        ),
        materialized=False,
    )
    assert "`my-gcp-proj`" in hyphen or '"my-gcp-proj"' in hyphen
    table_sql = event_name_cache_rebuild_sql(
        _form(
            project="p",
            write_project="dest-proj",
            write_dataset="analytics",
            event_column="event_name",
        ),
        materialized=False,
    )
    assert "CREATE OR REPLACE TABLE" in table_sql.upper()
    assert "MATERIALIZED VIEW" not in table_sql.upper()
    sf = event_name_cache_rebuild_sql(
        _form(
            kind="snowflake",
            table="ANALYTICS.MARTS.EVENTS",
            write_database="ANALYTICS",
            write_schema="MARTS",
            event_column="event_name",
        ),
        materialized=True,
    )
    assert "CREATE OR REPLACE MATERIALIZED VIEW" in sf.upper()


def test_custom_range_is_inclusive_dates():
    spec = spec_from_form(
        _form(range_preset="custom", start_date="2026-01-01", end_date="2026-01-31")
    )
    assert "DATE '2026-01-01'" in spec.where
    assert "DATE '2026-02-01'" in spec.where
    sql = events_sql(spec, dialect="bigquery")
    assert "INTERVAL" not in sql.upper()


def test_custom_range_rejects_bad_dates():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        spec_from_form(_form(range_preset="custom", start_date="nope", end_date="2026-01-01"))
    with pytest.raises(ValueError, match="on or before"):
        spec_from_form(
            _form(range_preset="custom", start_date="2026-02-01", end_date="2026-01-01")
        )


def test_event_filter_is_and_lookback():
    spec = spec_from_form(
        _form(event_column="event_name", event_value="paid")
    )
    assert "event_name = 'paid'" in spec.where
    assert (
        f"occurred_at >= {_ts(_shifted('current_date', 'day', -30))}"
        in spec.where
    )


def test_hyphenated_project_is_quoted_for_sqlglot():
    spec = spec_from_form(_form(table="my-gcp.analytics.events"))
    assert spec.table == '"my-gcp"."analytics"."events"'


def test_quote_in_event_value_is_escaped():
    spec = spec_from_form(
        _form(event_column="event_name", event_value="o'paid")
    )
    assert "o''paid" in spec.where


def test_sql_injection_in_table_is_rejected():
    with pytest.raises(ValueError, match="table"):
        spec_from_form(_form(table="events; drop table x"))


def test_lookback_and_hyphen_table_transpile_to_bigquery():
    spec = spec_from_form(_form(table="my-gcp.analytics.events", lookback_days=7))
    sql = events_sql(spec, dialect="bigquery")
    assert "`my-gcp`.`analytics`.`events`" in sql
    assert "DATE_SUB" in sql.upper()
    assert "CURRENT_DATE('UTC')" in sql
    assert "INTERVAL 7 DAY" in sql.upper()
    assert "APPROX_COUNT_DISTINCT" in sql.upper()


def test_exact_toggle():
    assert spec_from_form(_form(exact=True)).exact is True
    assert spec_from_form(_form(exact="on")).exact is True
    assert spec_from_form(_form()).exact is False


def test_lookback_zero_is_rejected():
    with pytest.raises(ValueError, match="lookback"):
        spec_from_form(_form(lookback_days=0))


def test_lookback_too_large_is_rejected():
    with pytest.raises(ValueError, match="lookback"):
        spec_from_form(_form(lookback_days=3651))


def test_event_column_without_value_is_rejected():
    with pytest.raises(ValueError, match="event name is required"):
        spec_from_form(_form(event_column="event_name", event_value=""))


def test_event_value_without_column_is_rejected():
    with pytest.raises(ValueError, match="event column"):
        spec_from_form(_form(event_column="", event_value="paid"))


def test_month_bucket_is_date_trunc_sugar():
    spec = spec_from_form(_form(grain="month"))
    assert spec.bucket == f"CAST({_shifted('fc_event_ts', 'month')} AS DATE)"


def test_day_bucket_casts_to_date():
    spec = spec_from_form(_form(grain="day"))
    assert spec.bucket == f"CAST({_shifted('fc_event_ts', 'day')} AS DATE)"
    sql = events_sql(spec, dialect="bigquery")
    assert "DATE(CAST(fc_event_ts AS TIMESTAMP), 'UTC')" in sql.replace("`", "")
    assert "DATE_TRUNC" not in sql.upper()


def test_plain_table_transpiles():
    spec = spec_from_form(_form(table="analytics.events"))
    sql = events_sql(spec, dialect="bigquery")
    assert "analytics" in sql
    assert "events" in sql
    assert "CURRENT_DATE('UTC')" in sql


def test_event_values_sql_is_distinct_ordered_lookback():
    sql = event_values_sql(
        _form(
            table="my-gcp.analytics.events",
            event_column="event_name",
            event_time="occurred_at",
            lookback_days=7,
        )
    )
    compact = " ".join(sql.split())
    upper = compact.upper()
    assert "DISTINCT" in upper
    assert "ORDER BY" in upper
    assert f"LIMIT {EVENT_VALUE_LIMIT}" in upper
    assert "DATE_SUB" in upper
    assert "CURRENT_DATE('UTC')" in sql
    assert "INTERVAL 7 DAY" in upper
    assert "`my-gcp`" in sql or "`MY-GCP`" in sql.upper()
    assert "fc_value" in sql.lower()
    assert "event_name" in sql


def test_event_values_sql_rejects_injection():
    with pytest.raises(ValueError, match="table"):
        event_values_sql(_form(table="events; drop table x", event_column="event_name"))
    with pytest.raises(ValueError, match="column"):
        event_values_sql(
            _form(event_column="event_name; drop", event_time="occurred_at")
        )


def test_event_values_sql_transpiles_without_sqlglot_warning(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="sqlglot")
    event_values_sql(
        _form(event_column="event_name", event_time="occurred_at")
    )
    warned = [r for r in caplog.records if r.name.startswith("sqlglot")]
    assert warned == []


def test_exact_uniques_is_count_distinct():
    spec = spec_from_form(_form(exact=True))
    sql = events_sql(spec, dialect="bigquery").upper()
    assert "COUNT" in sql and "DISTINCT" in sql
    assert "APPROX_COUNT_DISTINCT" not in sql


def test_average_is_total_over_uniques():
    spec = spec_from_form(_form(measure="average"))
    assert spec.on == "events"
    sql = events_sql(spec, dialect="bigquery").upper()
    assert "COUNT(*)" in sql.replace(" ", "") or "COUNT(*)" in sql
    assert "APPROX_COUNT_DISTINCT" in sql


def test_property_sum_requires_of():
    with pytest.raises(ValueError, match="of is required"):
        spec_from_form(_form(measure="sum"))


def test_property_sum_of_column():
    spec = spec_from_form(_form(measure="sum", of_column="revenue"))
    assert spec.on == "property"
    assert spec.measure == "sum"
    assert spec.of == "revenue"
    sql = events_sql(spec, dialect="bigquery").upper()
    assert "SUM(" in sql.replace(" ", "") or "SUM(" in sql


def test_property_average_is_avg_of_column():
    spec = spec_from_form(_form(measure="property_average", of_column="revenue"))
    assert spec.on == "property"
    assert spec.measure == "average"
    assert spec.of == "revenue"
    sql = events_sql(spec, dialect="bigquery").upper()
    assert "AVG(" in sql or "AVG (" in sql
    assert "APPROX_COUNT_DISTINCT" not in sql


def test_saved_on_property_with_average_measure():
    spec = spec_from_form(
        _form(measure="average", on="property", of_column="amount")
    )
    assert spec.on == "property"
    assert spec.measure == "average"
    assert spec.of == "amount"


def test_of_expr_wins_over_column():
    spec = spec_from_form(
        _form(measure="sum", of_column="ignored", of_expr="revenue / 100")
    )
    assert spec.of == "revenue / 100"


def test_of_expr_rejects_statements():
    with pytest.raises(ValueError, match="single SQL expression"):
        spec_from_form(_form(measure="sum", of_expr="revenue; drop table x"))


def test_distinct_is_per_entity_not_global_count():
    spec = spec_from_form(_form(measure="distinct", of_column="country"))
    assert spec.on == "property"
    assert spec.measure == "distinct"
    sql = events_sql(spec, dialect="bigquery").upper()
    assert "AVG(" in sql
    assert "APPROX_COUNT_DISTINCT" in sql or "COUNT(DISTINCT" in sql


def test_event_measure_ignores_leftover_of():
    spec = spec_from_form(_form(measure="uniques", of_column="revenue"))
    assert spec.on == "events"
    assert spec.of is None


def test_breakdown_column_fills_expression():
    spec = spec_from_form(_form(breakdown_column="country", top_n=5, include_other=True))
    assert spec.breakdowns == ("country",)
    assert spec.breakdown_labels == ("country",)
    assert spec.top_n == 5
    assert spec.include_other is True
    sql = events_sql(spec, dialect="bigquery")
    assert "country" in sql
    assert "fc_fold_0" in sql
    assert "APPROX_TOP_COUNT" in sql.upper()


def test_two_breakdown_slots_fill_tuple():
    spec = spec_from_form(
        _form(
            breakdowns=[
                {"breakdown_column": "country"},
                {"breakdown_column": "browser"},
            ],
            top_n=5,
        )
    )
    assert spec.breakdowns == ("country", "browser")
    assert spec.breakdown_labels == ("country", "browser")
    sql = events_sql(spec, dialect="bigquery")
    assert "fc_bd_1" in sql
    assert "APPROX_TOP_COUNT" not in sql.upper()
    assert "LIMIT" in sql.upper()


def test_three_breakdown_slots_compile():
    spec = spec_from_form(
        _form(
            breakdowns=[
                {"column": "country"},
                {"column": "browser"},
                {"column": "plan"},
            ]
        )
    )
    assert spec.breakdowns == ("country", "browser", "plan")
    sql = events_sql_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            breakdowns=[
                {"column": "country"},
                {"column": "browser"},
                {"column": "plan"},
            ],
        )
    )
    assert "plan" in sql


def test_empty_second_slot_is_one_breakdown():
    spec = spec_from_form(
        _form(
            breakdowns=[
                {"breakdown_column": "country"},
                {"breakdown_column": ""},
            ]
        )
    )
    assert spec.breakdowns == ("country",)


def test_snowflake_breakdown_uses_approx_top_k():
    spec = spec_from_form(
        _form(
            kind="snowflake",
            table="ANALYTICS.MARTS.EVENTS",
            measure="total",
            breakdown_column="country",
        )
    )
    sql = events_sql(spec, dialect="snowflake")
    assert "APPROX_TOP_K" in sql.upper()
    assert "FLATTEN" in sql.upper()


def test_json_of_sum_extracts_key():
    spec = spec_from_form(
        _form(measure="sum", of_column="properties", of_json_key="revenue")
    )
    assert spec.of == "SAFE_CAST(JSON_VALUE(properties, '$.revenue') AS FLOAT64)"


def test_json_of_distinct_is_untyped_json_value():
    spec = spec_from_form(
        _form(measure="distinct", of_column="properties", of_json_key="plan")
    )
    assert spec.of == "JSON_VALUE(properties, '$.plan')"
    assert "SAFE_CAST" not in spec.of


def test_json_key_rejected_for_snowflake():
    with pytest.raises(ValueError, match="JSON key extract"):
        spec_from_form(
            _form(
                kind="snowflake",
                table="ANALYTICS.MARTS.EVENTS",
                breakdown_column="properties",
                breakdown_json_key="plan",
            )
        )


def test_two_event_names_compile_to_in():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_values=["started", "completed"],
        )
    )
    assert "event_name IN ('started', 'completed')" in spec.where
    assert "event_name = 'started'" not in spec.where


def test_one_event_name_stays_equals():
    spec = spec_from_form(
        _form(event_column="event_name", event_values=["paid"])
    )
    assert "event_name = 'paid'" in spec.where
    assert " IN (" not in spec.where


def test_event_values_win_over_event_value():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="ignored",
            event_values=["started", "completed"],
        )
    )
    assert "ignored" not in spec.where
    assert "event_name IN ('started', 'completed')" in spec.where


def test_filter_is_and_is_not():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {"column": "country", "op": "is", "value": "UK"},
                {"join": "AND", "column": "plan", "op": "is_not", "value": "free"},
            ],
        )
    )
    assert "event_name = 'paid'" in spec.where
    assert "country = 'UK'" in spec.where
    assert "plan <> 'free'" in spec.where
    assert spec.where.count(" AND ") >= 2


def test_filter_in_and_null():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {"column": "country", "op": "is_any_of", "value": "UK, IE"},
                {"join": "AND", "column": "plan", "op": "is_null"},
            ],
        )
    )
    assert "country IN ('UK', 'IE')" in spec.where
    assert "plan IS NULL" in spec.where


def test_filter_none_of_and_not_null():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {"column": "country", "op": "is_none_of", "value": "US\nUK"},
                {"join": "AND", "column": "plan", "op": "is_not_null"},
            ],
        )
    )
    assert "country NOT IN ('US', 'UK')" in spec.where
    assert "plan IS NOT NULL" in spec.where


def test_filter_numeric_literal_unquoted():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "amount",
                    "op": "is",
                    "value": "10",
                    "type": "INT64",
                }
            ],
        )
    )
    assert "amount = 10" in spec.where
    assert "amount = '10'" not in spec.where


def test_filter_numeric_local_separators(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    from factcat_app.prefs import save

    save({"thousand_sep": "period", "decimal_sep": "comma"})
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "amount",
                    "op": "gt",
                    "value": "1.234,56",
                    "type": "FLOAT64",
                }
            ],
        )
    )
    assert "amount > 1234.56" in spec.where
    assert "1.234,56" not in spec.where


def test_filter_numeric_canonical_still_accepted_with_comma_prefs(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    from factcat_app.prefs import save

    save({"thousand_sep": "period", "decimal_sep": "comma"})
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "amount",
                    "op": "is",
                    "value": "1234.56",
                    "type": "FLOAT64",
                }
            ],
        )
    )
    assert "amount = 1234.56" in spec.where


def test_filter_json_key_is_json_value():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "properties",
                    "json_key": "plan",
                    "op": "is",
                    "value": "pro",
                }
            ],
        )
    )
    assert "JSON_VALUE(properties, '$.plan') = 'pro'" in spec.where


def test_filter_sql_expression_interpolated():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[{"expr": "amount > 0"}],
        )
    )
    assert "amount > 0" in spec.where


def test_filter_sql_expression_rejects_statements():
    with pytest.raises(ValueError, match="single SQL expression"):
        spec_from_form(
            _form(
                event_column="event_name",
                event_value="paid",
                filters=[{"expr": "amount > 0; drop table x"}],
            )
        )


def test_snowflake_events_sql_uses_convert_timezone():
    sql = events_sql_from_form(
        _form(
            kind="snowflake",
            table="ANALYTICS.MARTS.EVENTS",
            reporting_timezone="Europe/Berlin",
        )
    )
    assert "CONVERT_TIMEZONE" in sql.upper()
    assert "JSON_VALUE" not in sql.upper()
    assert "MAXIMUMBYTESBILLED" not in sql.upper()


def test_filter_mixed_and_or_is_left_folded():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {"column": "country", "op": "is", "value": "UK"},
                {"join": "OR", "column": "country", "op": "is", "value": "IE"},
                {"join": "AND", "column": "plan", "op": "is", "value": "pro"},
            ],
        )
    )
    assert (
        "(country = 'UK' OR country = 'IE') AND plan = 'pro'" in spec.where
        or "((country = 'UK' OR country = 'IE') AND plan = 'pro')" in spec.where
    )


def test_filter_matrix_ops_are_defined():
    for ops in FILTER_FAMILY_OPS.values():
        for op in ops:
            assert op in FILTER_OP_META
            assert FILTER_OP_META[op]["value"] in {"none", "one", "two", "list"}


def test_filter_boolean_is_true():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[{"column": "active", "op": "is_true", "type": "BOOL"}],
        )
    )
    assert "active IS TRUE" in spec.where


def test_filter_integer_rejects_decimal():
    with pytest.raises(ValueError, match="whole number"):
        spec_from_form(
            _form(
                event_column="event_name",
                event_value="paid",
                filters=[
                    {
                        "column": "n",
                        "op": "is",
                        "value": "10.5",
                        "type": "INT64",
                    }
                ],
            )
        )


def test_filter_float_allows_decimal():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "amount",
                    "op": "gt",
                    "value": "10.5",
                    "type": "FLOAT64",
                }
            ],
        )
    )
    assert "amount > 10.5" in spec.where


def test_filter_week_of_year_rejects_out_of_range():
    with pytest.raises(ValueError, match="at most 53"):
        spec_from_form(
            _form(
                event_column="event_name",
                event_value="paid",
                filters=[
                    {
                        "column": "dt",
                        "op": "is",
                        "value": "54",
                        "type": "DATE",
                        "date_part": "week_of_year",
                    }
                ],
            )
        )


def test_filter_numeric_comparisons_and_between():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {"column": "amount", "op": "gte", "value": "10", "type": "INT64"},
                {"join": "AND", "column": "amount", "op": "between", "value": "1", "value_to": "9", "type": "FLOAT64"},
            ],
        )
    )
    assert "amount >= 10" in spec.where
    assert "amount >= 1 AND amount <= 9" in spec.where


def test_filter_trunc_month_on_date():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "dt",
                    "op": "is",
                    "value": "2026-05-18",
                    "type": "DATE",
                    "date_part": "month",
                }
            ],
        )
    )
    assert "DATE_TRUNC(dt, MONTH) = DATE_TRUNC(DATE '2026-05-18', MONTH)" in spec.where
    ym = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "dt",
                    "op": "is",
                    "value": "2026-05",
                    "type": "DATE",
                    "date_part": "month",
                }
            ],
        )
    )
    assert "DATE_TRUNC(DATE '2026-05-01', MONTH)" in ym.where


def test_filter_trunc_month_on_event_time():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "occurred_at",
                    "op": "is",
                    "value": "2026-05-18",
                    "type": "TIMESTAMP",
                    "date_part": "month",
                }
            ],
        )
    )
    assert (
        "DATE_TRUNC(DATE(factcat_as_instant(occurred_at), 'UTC'), MONTH)"
        in spec.where
    )
    assert "DATE_TRUNC(DATE '2026-05-18', MONTH)" in spec.where


def test_filter_day_of_week():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "occurred_at",
                    "op": "is_any_of",
                    "values": ["Saturday", "sunday"],
                    "type": "TIMESTAMP",
                    "date_part": "day_of_week",
                }
            ],
        )
    )
    assert "FORMAT_DATE('%A', DATE(factcat_as_instant(occurred_at), 'UTC'))" in spec.where
    assert "IN ('Saturday', 'Sunday')" in spec.where


def test_filter_day_of_week_accepts_short_names():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "occurred_at",
                    "op": "is",
                    "value": "Mon",
                    "type": "TIMESTAMP",
                    "date_part": "day_of_week",
                }
            ],
        )
    )
    assert "FORMAT_DATE('%A', DATE(factcat_as_instant(occurred_at), 'UTC')) = 'Monday'" in spec.where


def test_filter_month_accepts_short_name():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "dt",
                    "op": "is",
                    "value": "Jan",
                    "type": "DATE",
                    "date_part": "month_of_year",
                }
            ],
        )
    )
    assert "FORMAT_DATE('%B', dt) = 'January'" in spec.where


def test_filter_month_of_year():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "dt",
                    "op": "is",
                    "value": "May",
                    "type": "DATE",
                    "date_part": "month_of_year",
                }
            ],
        )
    )
    assert "FORMAT_DATE('%B', dt) = 'May'" in spec.where


def test_filter_day_of_month_numeric():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "dt",
                    "op": "gte",
                    "value": "15",
                    "type": "DATE",
                    "date_part": "day_of_month",
                }
            ],
        )
    )
    assert "EXTRACT(DAY FROM dt) >= 15" in spec.where


def test_filter_hour_on_timestamp():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "occurred_at",
                    "op": "between",
                    "value": "9",
                    "value_to": "17",
                    "type": "TIMESTAMP",
                    "date_part": "hour_of_day",
                }
            ],
        )
    )
    assert (
        "EXTRACT(HOUR FROM DATETIME(factcat_as_instant(occurred_at), 'UTC')) >= 9"
        in spec.where
    )
    assert (
        "EXTRACT(HOUR FROM DATETIME(factcat_as_instant(occurred_at), 'UTC')) <= 17"
        in spec.where
    )


def test_filter_year_is_four_digits():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "dt",
                    "op": "is",
                    "value": "2026",
                    "type": "DATE",
                    "date_part": "year",
                }
            ],
        )
    )
    assert "EXTRACT(YEAR FROM dt) = 2026" in spec.where
    with pytest.raises(ValueError, match="year must be four digits"):
        spec_from_form(
            _form(
                event_column="event_name",
                event_value="paid",
                filters=[
                    {
                        "column": "dt",
                        "op": "is",
                        "value": "26",
                        "type": "DATE",
                        "date_part": "year",
                    }
                ],
            )
        )


def test_filter_day_of_year():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "dt",
                    "op": "lte",
                    "value": "31",
                    "type": "DATE",
                    "date_part": "day_of_year",
                }
            ],
        )
    )
    assert "EXTRACT(DAYOFYEAR FROM dt) <= 31" in spec.where


def test_filter_same_hour():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "occurred_at",
                    "op": "is",
                    "value": "2026-05-18",
                    "value_time": "14:30",
                    "type": "TIMESTAMP",
                    "date_part": "hour",
                }
            ],
        )
    )
    assert (
        "DATETIME_TRUNC(DATETIME(factcat_as_instant(occurred_at), 'UTC'), HOUR)"
        in spec.where
    )
    assert "DATETIME_TRUNC(DATETIME '2026-05-18 14:30:00', HOUR)" in spec.where


def test_filter_hour_on_date_is_rejected():
    with pytest.raises(ValueError, match="hour is not a date part"):
        spec_from_form(
            _form(
                event_column="event_name",
                event_value="paid",
                filters=[
                    {
                        "column": "dt",
                        "op": "is",
                        "value": "9",
                        "type": "DATE",
                        "date_part": "hour_of_day",
                    }
                ],
            )
        )


def test_filter_date_before():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[{"column": "dt", "op": "before", "value": "2026-08-01", "type": "DATE"}],
        )
    )
    assert "dt < DATE '2026-08-01'" in spec.where


def test_filter_time_between():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "t",
                    "op": "between",
                    "value": "09:00",
                    "value_to": "17:30:00",
                    "type": "TIME",
                }
            ],
        )
    )
    assert "t >= TIME '09:00:00'" in spec.where
    assert "t <= TIME '17:30:00'" in spec.where


def test_event_time_filter_is_that_day():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "occurred_at",
                    "op": "is",
                    "value": "2026-08-01",
                    "type": "TIMESTAMP",
                }
            ],
        )
    )
    assert "factcat_as_instant(occurred_at) >= factcat_ts_at_date(DATE '2026-08-01', 'UTC', 'utc')" in spec.where
    assert "factcat_as_instant(occurred_at) < factcat_ts_at_date(DATE '2026-08-02', 'UTC', 'utc')" in spec.where


def test_other_timestamp_is_stored_clock():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "trial_ends_at",
                    "op": "on_or_after",
                    "value": "2026-08-01",
                    "type": "TIMESTAMP",
                }
            ],
        )
    )
    assert "trial_ends_at >= TIMESTAMP '2026-08-01 00:00:00'" in spec.where
    assert "factcat_as_instant(trial_ends_at)" not in spec.where


def test_filter_contains_is_like_case_insensitive():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[{"column": "course_code", "op": "contains", "value": "xyz"}],
        )
    )
    assert "LOWER(course_code) LIKE LOWER('%xyz%') ESCAPE '#'" in spec.where


def test_filter_values_list_is_tokens():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "course_code",
                    "op": "starts_with",
                    "values": ["AB", "CD"],
                    "case_sensitive": True,
                }
            ],
        )
    )
    assert (
        "(course_code LIKE 'AB%' ESCAPE '#' OR course_code LIKE 'CD%' ESCAPE '#')"
        in spec.where
    )


def test_filter_starts_with_several_patterns():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "course_code",
                    "op": "starts_with",
                    "value": "AB, CD",
                    "case_sensitive": True,
                }
            ],
        )
    )
    assert "(course_code LIKE 'AB%' ESCAPE '#' OR course_code LIKE 'CD%' ESCAPE '#')" in spec.where


def test_filter_like_escapes_wildcards():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[{"column": "name", "op": "contains", "value": "100%", "case_sensitive": True}],
        )
    )
    assert "name LIKE '%100#%%' ESCAPE '#'" in spec.where


def test_filter_does_not_contain_ands_negations():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[{"column": "name", "op": "not_contains", "value": "foo, bar"}],
        )
    )
    assert "NOT (LOWER(name) LIKE LOWER('%foo%') ESCAPE '#')" in spec.where
    assert "NOT (LOWER(name) LIKE LOWER('%bar%') ESCAPE '#')" in spec.where


def test_json_contains_is_string_like():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "properties",
                    "json_key": "plan",
                    "op": "contains",
                    "value": "pro",
                    "type": "JSON",
                }
            ],
        )
    )
    assert (
        "LOWER(JSON_VALUE(properties, '$.plan')) LIKE LOWER('%pro%') ESCAPE '#'"
        in spec.where
    )


def test_series_event_time_filter_uses_form_clock():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            series=[
                {
                    "event": "started",
                    "filters": [
                        {
                            "column": "occurred_at",
                            "op": "before",
                            "value": "2026-08-15",
                            "type": "TIMESTAMP",
                        }
                    ],
                }
            ],
        )
    )
    assert "event_name = 'started'" in spec.where
    assert "factcat_as_instant(occurred_at) < factcat_ts_at_date(DATE '2026-08-15', 'UTC', 'utc')" in spec.where


def test_day_of_week_filter_transpiles_without_sqlglot_warning(caplog):
    import logging

    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "occurred_at",
                    "op": "is",
                    "value": "Monday",
                    "type": "TIMESTAMP",
                    "date_part": "day_of_week",
                }
            ],
        )
    )
    with caplog.at_level(logging.WARNING, logger="sqlglot"):
        sql = events_sql(spec, dialect="bigquery")
    assert not [r.message for r in caplog.records if r.name.startswith("sqlglot")]
    assert "FORMAT_DATE" in sql.upper() or "DAYOFWEEK" in sql.upper() or "%A" in sql


def test_contains_filter_transpiles_without_sqlglot_warning(caplog):
    import logging

    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[{"column": "name", "op": "contains", "value": "xyz"}],
        )
    )
    with caplog.at_level(logging.WARNING, logger="sqlglot"):
        sql = events_sql(spec, dialect="bigquery")
    assert not [r.message for r in caplog.records if r.name.startswith("sqlglot")]
    assert "LIKE" in sql.upper()


def test_filter_wrong_op_for_boolean_is_rejected():
    with pytest.raises(ValueError, match="filter operator is not supported"):
        spec_from_form(
            _form(
                event_column="event_name",
                event_value="paid",
                filters=[{"column": "active", "op": "contains", "value": "x", "type": "BOOL"}],
            )
        )


def test_filter_empty_value_is_rejected():
    with pytest.raises(ValueError, match="filter value is required"):
        spec_from_form(
            _form(
                event_column="event_name",
                event_value="paid",
                filters=[{"column": "country", "op": "is", "value": ""}],
            )
        )


def test_series_card_owns_filters():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            series=[
                {
                    "event": "started",
                    "filters": [{"column": "course_code", "op": "is", "value": "XYZ"}],
                }
            ],
        )
    )
    assert "event_name = 'started'" in spec.where
    assert "course_code = 'XYZ'" in spec.where


def test_any_of_is_or_with_per_member_filters():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            series=[
                {
                    "kind": "any_of",
                    "members": [
                        {
                            "event": "started",
                            "filters": [
                                {"column": "course_code", "op": "is", "value": "XYZ"}
                            ],
                        },
                        {"event": "completed", "filters": []},
                    ],
                }
            ],
        )
    )
    assert (
        "(event_name = 'started' AND course_code = 'XYZ') OR event_name = 'completed'"
        in spec.where
        or "(event_name = 'started' AND course_code = 'XYZ') OR (event_name = 'completed')"
        in spec.where
    )


def test_overlay_per_series_breakdown():
    sql = events_sql_from_form(
        _form(
            event_column="event_name",
            breakdown_by_series=True,
            series=[
                {"event": "started", "breakdown_column": "country"},
                {"event": "completed"},
            ],
        )
    )
    assert "UNION ALL" in sql
    assert "CONCAT('started', ' · ', CAST(country AS STRING)) AS series" in sql
    assert "'completed' AS series" in sql


def test_overlay_concat_all_breakdown_labels():
    sql = events_sql_from_form(
        _form(
            event_column="event_name",
            breakdowns=[
                {"breakdown_column": "country"},
                {"breakdown_column": "browser"},
            ],
            series=[{"event": "paid"}, {"event": "signup"}],
        )
    )
    assert "UNION ALL" in sql
    assert "CAST(country AS STRING)" in sql
    assert "CAST(browser AS STRING)" in sql
    assert ", country, browser," in sql.replace("\n", " ")


def test_snowflake_overlay_cast_is_varchar():
    sql = events_sql_from_form(
        _form(
            kind="snowflake",
            table="ANALYTICS.MARTS.EVENTS",
            event_column="event_name",
            series=[{"event": "started"}, {"event": "completed"}],
            breakdown_column="country",
        )
    )
    assert "CAST(country AS VARCHAR)" in sql
    assert "CAST(country AS STRING)" not in sql


def test_series_measure_overrides_chart_measure():
    spec = spec_from_form(
        _form(
            measure="uniques",
            event_column="event_name",
            series=[{"event": "paid", "measure": "total"}],
        )
    )
    assert spec.measure == "total"
    assert spec.on == "events"


def test_series_property_measure_uses_of():
    spec = spec_from_form(
        _form(
            measure="uniques",
            event_column="event_name",
            series=[{"event": "paid", "measure": "sum", "of_column": "revenue"}],
        )
    )
    assert spec.measure == "sum"
    assert spec.on == "property"
    assert spec.of == "revenue"


def test_combined_series_measure_is_on_the_nest():
    spec = spec_from_form(
        _form(
            measure="uniques",
            event_column="event_name",
            series=[
                {
                    "kind": "any_of",
                    "measure": "total",
                    "members": [{"event": "started"}, {"event": "completed"}],
                }
            ],
        )
    )
    assert spec.measure == "total"
    assert "event_name = 'started'" in spec.where
    assert "event_name = 'completed'" in spec.where


def test_overlay_per_series_measure():
    sql = events_sql_from_form(
        _form(
            event_column="event_name",
            series=[
                {"event": "started", "measure": "total"},
                {
                    "event": "completed",
                    "measure": "sum",
                    "of_column": "revenue",
                },
            ],
        )
    )
    assert "UNION ALL" in sql
    upper = sql.upper()
    assert "COUNT(*)" in upper.replace(" ", "") or "COUNT(*)" in sql.upper()
    assert "SUM(" in sql.upper()
    assert "revenue" in sql


def test_overlay_union_labels_series():
    sql = events_sql_from_form(
        _form(
            event_column="event_name",
            series=[{"event": "started"}, {"event": "completed"}],
        )
    )
    assert "UNION ALL" in sql
    assert "'started' AS series" in sql
    assert "'completed' AS series" in sql


def test_empty_filter_row_is_skipped():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[{}, {"column": "country", "op": "is", "value": "UK"}],
        )
    )
    assert "country = 'UK'" in spec.where


def test_json_breakdown_extracts_key():
    spec = spec_from_form(
        _form(breakdown_column="properties", breakdown_json_key="plan")
    )
    assert spec.breakdowns == ("JSON_VALUE(properties, '$.plan')",)
    assert spec.breakdown_labels == ("plan",)


def test_json_key_rejects_injection():
    with pytest.raises(ValueError, match="JSON key"):
        spec_from_form(
            _form(measure="sum", of_column="properties", of_json_key="x'); DROP TABLE t; --")
        )


def test_json_key_accepts_dotted_path():
    spec = spec_from_form(
        _form(measure="sum", of_column="properties", of_json_key="user.plan")
    )
    assert "$.user.plan" in spec.of


def test_breakdown_expr_wins_over_column():
    spec = spec_from_form(
        _form(breakdown_column="ignored", breakdown_expr="lower(country)")
    )
    assert spec.breakdowns == ("lower(country)",)
    assert spec.breakdown_labels is None


def test_breakdown_expr_rejects_statements():
    with pytest.raises(ValueError, match="single SQL expression"):
        spec_from_form(_form(breakdown_expr="country; drop table x"))


def test_include_other_false_from_form():
    spec = spec_from_form(_form(breakdown_column="country", include_other=False))
    assert spec.include_other is False


def test_no_breakdown_when_column_blank():
    spec = spec_from_form(_form())
    assert spec.breakdowns == ()


def test_berlin_timestamp_uses_date_in_that_zone():
    spec = spec_from_form(_form(reporting_timezone="Europe/Berlin"))
    assert _shifted("fc_event_ts", "day", tz="Europe/Berlin") in spec.bucket
    sql = events_sql(spec, dialect="bigquery")
    assert "DATE(CAST(fc_event_ts AS TIMESTAMP), 'Europe/Berlin')" in sql.replace("`", "")
    assert "CURRENT_DATE('Europe/Berlin')" in sql
    assert "DATE(CAST(fc_event_ts AS TIMESTAMP), 'UTC')" not in sql.replace("`", "")


def test_civil_datetime_casts_instead_of_date_tz():
    spec = spec_from_form(
        _form(grain="week", event_time_tz="reporting", reporting_timezone="Europe/London")
    )
    assert _shifted("fc_event_ts", "week", tz="Europe/London", kind="reporting") in spec.bucket
    assert "factcat_ts_at_date" in spec.where
    sql = events_sql(spec, dialect="bigquery")
    assert "CAST(fc_event_ts AS DATE)" in sql.replace("`", "")
    assert "DATE(occurred_at," not in sql.replace("`", "")
    assert "DATE(fc_event_ts," not in sql.replace("`", "")
    assert "CURRENT_DATE('Europe/London')" in sql


def test_utc_kind_isolates_column_in_window():
    spec = spec_from_form(_form())
    assert spec.event_time == "factcat_as_instant(occurred_at)"
    assert "occurred_at >=" in spec.where
    assert "factcat_as_instant(occurred_at) >=" not in spec.where
    sql = events_sql(spec, dialect="bigquery").replace("`", "")
    assert "factcat_as_instant" not in sql
    assert "CAST(occurred_at AS TIMESTAMP) AS fc_event_ts" in sql
    assert "DATE(CAST(fc_event_ts AS TIMESTAMP), 'UTC')" in sql
    assert "CAST(occurred_at AS TIMESTAMP) >=" not in sql
    assert "DATETIME(TIMESTAMP(" in sql
    assert "fc_bucket AS bucket" in sql
    assert "CAST(fc_bucket AS DATE) AS bucket" not in sql


def test_unknown_timezone_is_rejected():
    with pytest.raises(ValueError, match="reporting_timezone"):
        spec_from_form(_form(reporting_timezone="Not/A_Zone"))
    with pytest.raises(ValueError, match="event_time_tz"):
        spec_from_form(_form(event_time_tz="local"))


def test_unix_epoch_seconds_uses_timestamp_seconds():
    spec = spec_from_form(_form(event_time_epoch="seconds"))
    assert spec.event_time == "factcat_as_instant(occurred_at, 'unix_s')"
    sql = events_sql(spec, dialect="bigquery")
    assert "TIMESTAMP_SECONDS(occurred_at)" in sql.replace(" ", "") or (
        "TIMESTAMP_SECONDS(occurred_at)" in sql
    )
    assert "TIMESTAMP_MILLIS" not in sql.upper()


def test_unix_epoch_millis_snowflake():
    sql = events_sql_from_form(
        _form(
            kind="snowflake",
            table="ANALYTICS.MARTS.EVENTS",
            event_time_epoch="milliseconds",
        )
    )
    assert "TO_TIMESTAMP_TZ(occurred_at, 3)" in sql.replace(" ", "") or (
        "TO_TIMESTAMP_TZ(occurred_at, 3)" in sql
    )


def test_snowflake_instant_kind_emits_two_arg_convert():
    sql = events_sql_from_form(
        _form(
            kind="snowflake",
            table="ANALYTICS.MARTS.EVENTS",
            reporting_timezone="Europe/Berlin",
            event_time_tz="instant",
        )
    )
    compact = sql.replace(" ", "")
    assert "CONVERT_TIMEZONE('Europe/Berlin',fc_event_ts)" in compact or (
        "CONVERT_TIMEZONE('Europe/Berlin', fc_event_ts)" in sql.replace("\n", "")
    )
    assert "CONVERT_TIMEZONE('UTC', 'Europe/Berlin'" not in sql


def test_annotate_incomplete_uses_iana_today():
    rows = annotate_incomplete(
        [{"bucket": "2099-01-01", "value": 1}],
        _form(grain="day", reporting_timezone="Europe/Berlin"),
    )
    assert rows[0]["incomplete"] is False


def test_hour_bucket_is_not_cast_as_date():
    spec = spec_from_form(_form(grain="hour"))
    assert "factcat_hour_trunc(fc_event_ts" in spec.bucket
    assert "CAST(" not in spec.bucket
    sql = events_sql(spec, dialect="bigquery")
    assert "DATETIME_TRUNC" in sql.upper()
    assert "CAST(fc_bucket AS DATE)" not in sql
    wrap = events_sql_from_form(_form(grain="hour"))
    assert "CAST(bucket AS DATE)" not in wrap
    assert "ORDER BY bucket" in wrap
    assert "LIMIT" in wrap


def test_hour_last_24_hours_is_rolling():
    spec = spec_from_form(
        _form(grain="hour", range_mode="last", range_n=24, range_unit="hour")
    )
    assert "factcat_hours_ago(24, 'UTC', 'utc')" in spec.where


def test_hour_exclude_current_uses_reporting_zone():
    spec = spec_from_form(
        _form(
            grain="hour",
            range_mode="last",
            range_n=24,
            range_unit="hour",
            include_current=False,
            reporting_timezone="Europe/Berlin",
        )
    )
    assert "factcat_hours_ago(24, 'Europe/Berlin', 'utc')" in spec.where
    assert "factcat_current_hour_start('Europe/Berlin', 'utc')" in spec.where
    assert "factcat_hour_trunc(CURRENT_TIMESTAMP()" not in spec.where


def test_hour_last_30_days_keeps_day_window():
    spec = spec_from_form(
        _form(grain="hour", range_mode="last", range_n=30, range_unit="day")
    )
    assert _shifted("current_date", "day", -30) in spec.where


def test_day_of_week_bucket_is_integer_extract():
    spec = spec_from_form(_form(grain="day_of_week", week_start="monday"))
    assert "factcat_dow(fc_event_ts" in spec.bucket
    sql = events_sql(spec, dialect="bigquery")
    assert "DAYOFWEEK" in sql.upper() or "MOD(" in sql.upper()
    wrap = events_sql_from_form(_form(grain="day_of_week", query_row_limit=12))
    assert "CAST(bucket AS DATE)" not in wrap
    assert "LIMIT 12" in wrap
    assert "ORDER BY bucket" in wrap
    compact = " ".join(wrap.split()).upper()
    assert compact.index("LIMIT 12") < compact.rindex("ORDER BY BUCKET")


def test_hour_of_day_range_is_not_locked_to_hour():
    spec = spec_from_form(
        _form(
            grain="hour_of_day",
            range_mode="last",
            range_n=14,
            range_unit="day",
        )
    )
    assert "factcat_hour_of_day(fc_event_ts" in spec.bucket
    assert _shifted("current_date", "day", -14) in spec.where


def test_day_of_week_last_six_months_excludes_current():
    spec = spec_from_form(
        _form(
            grain="day_of_week",
            range_mode="last",
            range_n=6,
            range_unit="month",
            include_current=False,
        )
    )
    assert "factcat_dow(fc_event_ts" in spec.bucket
    assert _shifted("current_date", "month", -6) in spec.where
    assert _shifted("current_date", "month", 0) in spec.where


def test_hour_of_day_last_three_quarters_include_current():
    spec = spec_from_form(
        _form(
            grain="hour_of_day",
            range_mode="last",
            range_n=3,
            range_unit="quarter",
            include_current=True,
        )
    )
    assert _shifted("current_date", "quarter", -2) in spec.where
    assert _shifted("current_date", "quarter", 0) not in spec.where


def test_cyclic_last_months_exclude_current_by_default():
    spec = spec_from_form(
        _form(
            grain="hour_of_day",
            range_mode="last",
            range_n=6,
            range_unit="month",
        )
    )
    assert _shifted("current_date", "month", -6) in spec.where
    assert _shifted("current_date", "month", 0) in spec.where


def test_day_of_week_this_quarter_is_a_filter_window():
    spec = spec_from_form(
        _form(
            grain="day_of_week",
            range_mode="this",
            range_unit="quarter",
        )
    )
    assert _shifted("current_date", "quarter") in spec.where
    assert "factcat_dow(fc_event_ts" in spec.bucket


def test_cyclic_relative_custom_uses_range_unit():
    spec = spec_from_form(
        _form(
            grain="day_of_week",
            range_mode="custom",
            custom_kind="relative",
            range_unit="month",
            rel_start_n=6,
            rel_end_n=0,
        )
    )
    assert _shifted("current_date", "month", -6) in spec.where
    assert _shifted("current_date", "week", -6) not in spec.where


def test_cyclic_fill_keeps_breakdown_groups_apart():
    rows = fill_cyclic_buckets(
        [
            {"bucket": "0", "value": 1, "country": "UK"},
            {"bucket": "0", "value": 2, "country": "IE"},
        ],
        _form(grain="day_of_week", week_start="monday"),
    )
    mondays = [r for r in rows if r["bucket"] == "0"]
    assert sorted(r["country"] for r in mondays) == ["IE", "UK"]
    assert {r["value"] for r in mondays} == {1, 2}
    uk_sun = next(
        r for r in rows if r["bucket"] == "6" and r["country"] == "UK"
    )
    ie_sun = next(
        r for r in rows if r["bucket"] == "6" and r["country"] == "IE"
    )
    assert uk_sun["value"] == 0
    assert ie_sun["value"] == 0


def test_cyclic_fill_keeps_extra_columns_on_zeros():
    rows = fill_cyclic_buckets(
        [{"bucket": "0", "value": 4, "series": "paid", "plan": "pro"}],
        _form(grain="day_of_week", week_start="monday"),
    )
    monday = next(r for r in rows if r["bucket"] == "0")
    sunday = next(r for r in rows if r["bucket"] == "6")
    assert monday["plan"] == "pro"
    assert sunday["value"] == 0
    assert sunday["plan"] == "pro"
    assert sunday["series"] == "paid"


def test_cyclic_fill_includes_missing_sunday():
    rows = fill_cyclic_buckets(
        [{"bucket": "0", "value": 4}],
        _form(grain="day_of_week", week_start="monday"),
    )
    assert [r["bucket"] for r in rows] == [str(i) for i in range(7)]
    assert rows[6]["value"] == 0
    sun = fill_cyclic_buckets(
        [{"bucket": "0", "value": 1}],
        _form(grain="day_of_week", week_start="sunday"),
    )
    assert [r["bucket"] for r in sun] == ["6", "0", "1", "2", "3", "4", "5"]


def test_hour_as_date_trunc_day_is_not_the_bucket():
    spec = spec_from_form(_form(grain="hour"))
    assert "date_trunc('day'" not in spec.bucket.lower()
    assert "CAST(" not in spec.bucket


def test_dow_is_not_week_trunc():
    spec = spec_from_form(_form(grain="day_of_week"))
    assert "date_trunc('week'" not in spec.bucket.lower()
    assert "WEEK(" not in spec.bucket


def test_hour_filter_parses_12_hour_clock():
    spec = spec_from_form(
        _form(
            event_column="event_name",
            event_value="paid",
            filters=[
                {
                    "column": "occurred_at",
                    "op": "is",
                    "date_part": "hour_of_day",
                    "value": "3 pm",
                    "type": "TIMESTAMP",
                }
            ],
        )
    )
    assert "EXTRACT(HOUR" in spec.where
    assert "= 15" in spec.where or "=15" in spec.where.replace(" ", "")
