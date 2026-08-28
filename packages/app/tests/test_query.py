"""Form → EventsSpec. No warehouse."""

from __future__ import annotations

import pytest

from factcat import events_sql
from factcat_app.query import spec_from_form


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


def test_week_bucket_is_date_trunc_sugar():
    spec = spec_from_form(_form(grain="week"))
    assert spec.bucket == "date_trunc('week', occurred_at)"


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


def test_event_column_without_value_is_rejected():
    with pytest.raises(ValueError, match="together"):
        spec_from_form(_form(event_column="event_name", event_value=""))


def test_month_bucket_is_date_trunc_sugar():
    spec = spec_from_form(_form(grain="month"))
    assert spec.bucket == "date_trunc('month', occurred_at)"


def test_plain_table_transpiles():
    spec = spec_from_form(_form(table="analytics.events"))
    sql = events_sql(spec, dialect="bigquery")
    assert "analytics" in sql
    assert "events" in sql
    assert "INTERVAL" not in sql.upper()


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
