"""Ground-truth tests for the Events time series.

Uniques is COUNT DISTINCT of the caller entity. On the payments fixture U1
holds two subscriptions, so the same day has two unique subscriptions and
one unique user. Average/sum/min/max use a separate amounts table so the
numbers can be worked out by hand.
"""

from __future__ import annotations

import duckdb
import pytest

from factcat import EventsSpec, events_sql

# entity, occurred_at, amount — two rows for S1 on day 1.
AMOUNTS = [
    ("S1", "2026-01-05", 10.0),
    ("S1", "2026-01-05", 30.0),
    ("S2", "2026-01-05", 20.0),
    (None, "2026-01-05", 5.0),  # NULL grain: in Total, out of Uniques
    ("S1", "2026-01-12", 40.0),
]


@pytest.fixture()
def amounts() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE amounts ("
        "  entity_id VARCHAR,"
        "  occurred_at DATE,"
        "  amount DOUBLE"
        ")"
    )
    con.executemany("INSERT INTO amounts VALUES (?, ?, ?)", AMOUNTS)
    return con


def _spec(**overrides) -> EventsSpec:
    base = dict(
        table="payments",
        entity="subscription_id",
        event_time="paid_at",
        measure="uniques",
    )
    base.update(overrides)
    return EventsSpec(**base)


def _by_day(con, spec: EventsSpec) -> dict[str, float]:
    rows = con.execute(events_sql(spec)).fetchall()
    return {str(bucket)[:10]: float(value) for bucket, value in rows}


def test_uniques_at_subscription_grain_differs_from_user_grain(con):
    """U1 holds S1 and S2, both paying on 2026-01-01."""
    by_sub = _by_day(con, _spec(measure="uniques", entity="subscription_id"))
    by_user = _by_day(con, _spec(measure="uniques", entity="user_id"))
    assert by_sub["2026-01-01"] == 2
    assert by_user["2026-01-01"] == 1


def test_total_is_at_least_uniques(con):
    totals = _by_day(con, _spec(measure="total"))
    uniques = _by_day(con, _spec(measure="uniques"))
    assert totals.keys() == uniques.keys()
    for day in totals:
        assert totals[day] >= uniques[day]


def test_total_counts_rows(con):
    """Two collected payments on 2026-01-01 (S1 and S2)."""
    got = _by_day(con, _spec(measure="total"))
    assert got["2026-01-01"] == 2
    assert got["2026-01-15"] == 1


def test_null_entity_is_in_total_not_uniques(amounts):
    spec = dict(
        table="amounts",
        entity="entity_id",
        event_time="occurred_at",
    )
    total = _by_day(amounts, EventsSpec(measure="total", **spec))
    uniques = _by_day(amounts, EventsSpec(measure="uniques", **spec))
    # 4 rows on 2026-01-05, three with an id (S1 twice + S2).
    assert total["2026-01-05"] == 4
    assert uniques["2026-01-05"] == 2


def test_average_sum_min_max_are_hand_computed(amounts):
    spec = dict(
        table="amounts",
        entity="entity_id",
        event_time="occurred_at",
        of="amount",
    )
    # 10 + 30 + 20 + 5 = 65 over 4 rows; min 5, max 30.
    assert _by_day(amounts, EventsSpec(measure="sum", **spec))["2026-01-05"] == 65
    assert _by_day(amounts, EventsSpec(measure="min", **spec))["2026-01-05"] == 5
    assert _by_day(amounts, EventsSpec(measure="max", **spec))["2026-01-05"] == 30
    assert _by_day(amounts, EventsSpec(measure="average", **spec))["2026-01-05"] == 16.25
    assert _by_day(amounts, EventsSpec(measure="sum", **spec))["2026-01-12"] == 40


def test_default_bucket_is_calendar_day(con):
    got = _by_day(con, _spec(measure="total"))
    days = {
        str(row[0])[:10]
        for row in con.execute("SELECT DISTINCT paid_at FROM payments").fetchall()
    }
    assert set(got) == days
    sql = events_sql(_spec())
    assert "date_trunc" in sql.lower()
    assert "day" in sql.lower()


def test_explicit_week_bucket(amounts):
    """2026-01-05 and 2026-01-12 are different ISO weeks (Mon-start in DuckDB)."""
    spec = EventsSpec(
        table="amounts",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        bucket="date_trunc('week', occurred_at)",
    )
    rows = amounts.execute(events_sql(spec)).fetchall()
    assert len(rows) == 2
    assert sum(int(v) for _, v in rows) == 5


def test_unknown_measure_is_rejected():
    with pytest.raises(ValueError, match="measure"):
        EventsSpec(
            table="payments",
            entity="subscription_id",
            event_time="paid_at",
            measure="unique_users",  # type: ignore[arg-type]
        )


def test_average_requires_of():
    with pytest.raises(ValueError, match="requires of"):
        EventsSpec(
            table="amounts",
            entity="entity_id",
            event_time="occurred_at",
            measure="average",
        )


def test_total_rejects_of():
    with pytest.raises(ValueError, match="does not take of"):
        EventsSpec(
            table="payments",
            entity="subscription_id",
            event_time="paid_at",
            measure="total",
            of="amount",
        )


def test_where_filters_rows(con):
    got = _by_day(con, _spec(measure="total", where="status = 'failed'"))
    assert got == {"2026-03-08": 1.0}
