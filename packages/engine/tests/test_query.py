"""Form → EventsSpec. No warehouse."""

from __future__ import annotations

import pytest

from datetime import date

from factcat import events_sql
from factcat_app.query import (
    EVENT_VALUE_LIMIT,
    annotate_incomplete,
    event_values_sql,
    events_sql_from_form,
    job_bytes_cap,
    query_row_limit,
    spec_from_form,
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
    assert "factcat_period_start_shifted(occurred_at, 'week', 'monday', 0)" in spec.bucket
    sql = events_sql(spec, dialect="bigquery")
    assert "WEEK(MONDAY)" in sql.upper().replace(" ", "")
    assert "factcat_period_start_shifted" not in sql
    sun = spec_from_form(_form(grain="week", week_start="sunday"))
    assert "WEEK(SUNDAY)" in events_sql(sun, dialect="bigquery").upper().replace(" ", "")


def test_range_preset_7_is_last_n_days():
    spec = spec_from_form(_form(range_preset="7"))
    assert "occurred_at >= current_date - 7" in spec.where


def test_this_month_is_anchored_date_trunc():
    spec = spec_from_form(_form(range_mode="this", range_unit="month", grain="month"))
    assert "factcat_period_start_shifted(current_date, 'month'" in spec.where
    sql = events_sql(spec, dialect="bigquery").upper()
    assert "MONTH" in sql


def test_last_week_is_previous_complete_week():
    spec = spec_from_form(
        _form(range_mode="previous", range_unit="week", grain="week")
    )
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', -1)" in spec.where
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', 0)" in spec.where


def test_today_is_current_calendar_day():
    spec = spec_from_form(_form(range_mode="this", range_unit="day", grain="day"))
    assert "factcat_period_start_shifted(current_date, 'day'" in spec.where


def test_this_week_by_day_keeps_week_window():
    spec = spec_from_form(_form(grain="day", range_mode="this", range_unit="week"))
    assert "factcat_period_start_shifted(current_date, 'week'" in spec.where
    assert spec.bucket == "CAST(date_trunc('day', occurred_at) AS DATE)"


def test_week_grain_this_day_bumps_to_this_week():
    spec = spec_from_form(_form(grain="week", range_mode="this", range_unit="day"))
    assert "factcat_period_start_shifted(current_date, 'week'" in spec.where


def test_week_grain_rejects_day_window():
    spec = spec_from_form(
        _form(grain="week", range_mode="last", range_n=30, range_unit="day")
    )
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', -8)" in spec.where
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', 0)" in spec.where
    assert "current_date - 30" not in spec.where


def test_last_eight_weeks_are_complete_by_default():
    spec = spec_from_form(
        _form(grain="week", range_mode="last", range_n=8, range_unit="week")
    )
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', -8)" in spec.where
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', 0)" in spec.where


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
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', -7)" in spec.where
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', 0)" not in spec.where


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
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', -12)" in spec.where
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', -2)" in spec.where


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
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', -5)" in spec.where
    assert "factcat_period_start_shifted(current_date, 'week', 'monday', 0)" in spec.where


def test_events_sql_from_form_limits_most_recent_buckets():
    sql = events_sql_from_form(_form(query_row_limit=12))
    compact = " ".join(sql.split()).upper()
    assert "LIMIT 12" in compact
    assert "ORDER BY BUCKET DESC" in compact
    assert compact.index("LIMIT 12") < compact.rindex("ORDER BY BUCKET")


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
    assert "CURRENT_DATE - 90" in compact.replace("CURRENT_DATE()", "CURRENT_DATE")
    assert "OCCURRED_AT" in compact


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
    assert "occurred_at >= current_date - 30" in spec.where


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
    assert "INTERVAL" not in sql.upper()
    assert "CURRENT_DATE - 7" in sql.replace("\n", " ").replace("  ", " ")
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


def test_event_column_without_value_is_no_filter():
    spec = spec_from_form(_form(event_column="event_name", event_value=""))
    assert "event_name =" not in spec.where


def test_event_value_without_column_is_rejected():
    with pytest.raises(ValueError, match="event column"):
        spec_from_form(_form(event_column="", event_value="paid"))


def test_month_bucket_is_date_trunc_sugar():
    spec = spec_from_form(_form(grain="month"))
    assert spec.bucket == "CAST(date_trunc('month', occurred_at) AS DATE)"


def test_day_bucket_casts_to_date():
    spec = spec_from_form(_form(grain="day"))
    assert spec.bucket == "CAST(date_trunc('day', occurred_at) AS DATE)"
    sql = events_sql(spec, dialect="bigquery").upper()
    assert "DATE" in sql
    assert "DATE_TRUNC" in sql or "TIMESTAMP_TRUNC" in sql


def test_plain_table_transpiles():
    spec = spec_from_form(_form(table="analytics.events"))
    sql = events_sql(spec, dialect="bigquery")
    assert "analytics" in sql
    assert "events" in sql
    assert "INTERVAL" not in sql.upper()


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
    assert "INTERVAL" not in upper
    assert "CURRENT_DATE - 7" in compact.upper().replace("CURRENT_DATE()", "CURRENT_DATE")
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
    sql = events_sql(spec, dialect="bigquery").upper()
    assert "COUNT(*)" in sql.replace(" ", "") or "COUNT(*)" in sql
    assert "APPROX_COUNT_DISTINCT" in sql
