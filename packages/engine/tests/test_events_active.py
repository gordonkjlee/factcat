"""Rolling active users: distinct entities in the trailing ``window_days``.

On day D the value is the number of entities with a row on any day in
[D - window_days + 1, D]. Ground truth here is a hand-counted fixture plus an
independent brute-force reference in Python; the SQL is executed on DuckDB.

Mutations these tests must fail: a window of N - 1 or N + 1 days; the fan-out
leaking days past ``report_until`` or before ``report_from``; a day with
nobody active reported as zero. (Dropping the NULL-entity filter in
``active_days`` is not a mutation: ``COUNT(DISTINCT)`` ignores NULL anyway, so
the filter is only a cost saving.)
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from factcat import EventsSpec, events_sql
from factcat.dialects import SUPPORTED, date_shift

D0 = date(2026, 3, 1)


def d(n: int) -> str:
    """Day ``n`` of the fixture (day 0 is 1 March 2026)."""
    return (D0 + timedelta(days=n)).isoformat()


# entity, day number, platform. A gap from day 12 to day 19 has nobody active.
ROWS = [
    ("u1", 0, "ios"), ("u1", 0, "ios"), ("u1", 3, "web"), ("u1", 20, "ios"),
    ("u2", 1, "ios"), ("u2", 4, "ios"), ("u2", 21, "web"),
    ("u3", 2, "web"), ("u3", 11, "web"),
    ("u4", 5, "ios"), ("u4", 5, "ios"), ("u4", 6, "ios"),
    ("u5", 8, "ios"), ("u5", 30, "web"),
    ("u6", 11, "ios"), ("u6", 12, "ios"),
    ("u7", 20, "web"), ("u7", 38, "ios"),
    ("u8", 39, "ios"),
    (None, 3, "ios"), (None, 20, "web"),
]


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    c.execute("CREATE TABLE ev (user_id VARCHAR, occurred_at DATE, platform VARCHAR)")
    c.executemany("INSERT INTO ev VALUES (?, ?, ?)", [(u, d(n), p) for u, n, p in ROWS])
    return c


def _spec(window: int, **overrides) -> EventsSpec:
    base = dict(
        table="ev",
        entity="user_id",
        event_time="occurred_at",
        measure="active",
        window_days=window,
        exact=True,
    )
    base.update(overrides)
    return EventsSpec(**base)


def _series(con, spec: EventsSpec) -> dict[str, int]:
    rows = con.execute(events_sql(spec, "duckdb")).fetchall()
    return {r[0].isoformat(): r[1] for r in rows}


def _reference(window: int, first: int, last: int) -> dict[str, int]:
    """Brute force, written separately from the SQL: who is active on each day."""
    out = {}
    for day in range(first, last + 1):
        active = {u for u, n, _ in ROWS if u is not None and day - window < n <= day}
        if active:
            out[d(day)] = len(active)
    return out


# ---- hand counts -----------------------------------------------------------


def test_daily_is_the_distinct_count_per_day(con):
    got = _series(con, _spec(1))
    assert got[d(0)] == 1  # u1 twice
    assert got[d(5)] == 1  # u4 twice
    assert got[d(11)] == 2  # u3 and u6
    assert got[d(20)] == 2  # u1 and u7; the NULL entity is not counted
    assert d(15) not in got  # nobody active: absent, not zero


def test_three_day_window_by_hand(con):
    got = _series(con, _spec(3))
    assert got[d(2)] == 3  # u1(0), u2(1), u3(2)
    assert got[d(5)] == 3  # u1(3), u2(4), u4(5); u3(2) has left
    assert got[d(6)] == 2  # u2(4), u4(5 and 6)
    assert got[d(7)] == 1  # u4 only
    assert got[d(11)] == 2  # u3(11), u6(11)
    assert got[d(14)] == 1  # u6(12)
    assert d(15) not in got


def test_seven_day_window_by_hand(con):
    got = _series(con, _spec(7))
    assert got[d(6)] == 4  # u1, u2, u3, u4
    assert got[d(8)] == 5  # + u5
    assert got[d(11)] == 4  # u4, u5, u3, u6
    assert got[d(15)] == 2  # u3(11), u6(11/12)
    assert got[d(18)] == 1  # u6(12)
    assert d(19) not in got
    assert got[d(26)] == 3  # u1(20), u7(20), u2(21)
    assert got[d(27)] == 1  # u2(21)


def test_thirty_day_window_by_hand(con):
    got = _series(con, _spec(30))
    assert got[d(12)] == 6  # the window reaches before the data starts
    assert got[d(29)] == 7  # u1..u7
    assert got[d(39)] == 7  # u3, u6, u1, u7, u2, u5, u8
    assert got[d(41)] == 6  # u3 (day 11) has just left


def test_window_edge_is_inclusive_of_n_minus_one_and_exclusive_of_n(con):
    # u1's days are 0, 3, 20. With a 3-day window u1 is active on days 3..5.
    got = _series(con, _spec(3, where="user_id = 'u1'"))
    assert [k for k in got if d(3) <= k <= d(6)] == [d(3), d(4), d(5)]
    assert d(6) not in got and d(2) in got  # day 2 is still inside day 0's window


@pytest.mark.parametrize("window", [1, 2, 3, 7, 30])
def test_matches_a_brute_force_reference_on_every_day(con, window):
    spec = _spec(window, report_from=f"DATE '{d(0)}'", report_until=f"DATE '{d(46)}'")
    assert _series(con, spec) == _reference(window, 0, 45)


def test_one_day_window_is_daily_uniques(con):
    active = _series(con, _spec(1))
    uniq = con.execute(
        events_sql(
            EventsSpec(
                table="ev", entity="user_id", event_time="occurred_at",
                measure="uniques", exact=True,
            ),
            "duckdb",
        )
    ).fetchall()
    assert active == {r[0].date().isoformat(): r[1] for r in uniq}  # Uniques buckets are timestamps


def test_null_entity_is_not_counted(con):
    # Day 20 has u1, u7 and a NULL entity row.
    assert _series(con, _spec(1))[d(20)] == 2


def test_approximate_agrees_with_exact_on_a_small_fixture(con):
    window = 7
    bounds = dict(report_from=f"DATE '{d(0)}'", report_until=f"DATE '{d(46)}'")
    assert _series(con, _spec(window, exact=False, **bounds)) == _series(
        con, _spec(window, exact=True, **bounds)
    )


# ---- the report bounds and the lookback ------------------------------------


def test_report_bounds_limit_the_days_shown(con):
    spec = _spec(7, report_from=f"DATE '{d(10)}'", report_until=f"DATE '{d(14)}'")
    assert sorted(_series(con, spec)) == [d(10), d(11), d(12), d(13)]


def test_a_lookback_in_where_feeds_the_first_days(con):
    bounds = dict(report_from=f"DATE '{d(15)}'", report_until=f"DATE '{d(20)}'")
    # Scan starts at day 10: day 15's 7-day window (9..15) is fully read.
    with_lookback = _series(con, _spec(7, where=f"occurred_at >= DATE '{d(10)}'", **bounds))
    # Scan starts at day 15: nothing before it is read, so day 15 is missing.
    without = _series(con, _spec(7, where=f"occurred_at >= DATE '{d(15)}'", **bounds))
    assert with_lookback[d(15)] == 2
    assert d(15) not in without


def test_the_fan_out_never_leaks_past_report_until(con):
    # u8's only row is day 39; its 30-day window would reach day 68.
    spec = _spec(30, report_until=f"DATE '{d(41)}'")
    assert max(_series(con, spec)) == d(40)


# ---- breakdowns -------------------------------------------------------------


def test_breakdown_counts_an_entity_in_each_group_it_was_active_in(con):
    spec = _spec(7, breakdowns=("platform",), report_from=f"DATE '{d(3)}'", report_until=f"DATE '{d(4)}'")
    rows = con.execute(events_sql(spec, "duckdb")).fetchall()
    # Window days 0..3: ios has u1(0), u2(1); web has u3(2), u1(3). u1 is in both.
    assert {r[1]: r[2] for r in rows} == {"ios": 2, "web": 2}
    overall = _series(con, _spec(7))
    assert overall[d(3)] == 3  # u1, u2, u3: the groups overlap, the total does not


def test_breakdown_folds_the_tail_into_other(con):
    spec = _spec(
        7, breakdowns=("platform",), top_n=1,
        report_from=f"DATE '{d(3)}'", report_until=f"DATE '{d(4)}'",
    )
    rows = con.execute(events_sql(spec, "duckdb")).fetchall()
    assert {r[1]: r[2] for r in rows} == {"ios": 2, "(other)": 2}
    dropped = con.execute(
        events_sql(
            _spec(
                7, breakdowns=("platform",), top_n=1, include_other=False,
                report_from=f"DATE '{d(3)}'", report_until=f"DATE '{d(4)}'",
            ),
            "duckdb",
        )
    ).fetchall()
    assert {r[1]: r[2] for r in dropped} == {"ios": 2}


# ---- the spec ---------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, 366, 1.5, "7", True])
def test_window_days_is_an_integer_from_1_to_365(bad):
    with pytest.raises(ValueError, match="window_days"):
        _spec(bad)


def test_window_days_365_is_allowed():
    assert _spec(365).window_days == 365


@pytest.mark.parametrize("measure", ["total", "uniques", "average"])
def test_window_and_report_bounds_belong_to_active_only(measure):
    base = dict(table="ev", entity="user_id", event_time="occurred_at", measure=measure)
    with pytest.raises(ValueError, match="window_days"):
        EventsSpec(**base, window_days=7)
    with pytest.raises(ValueError, match="report_from"):
        EventsSpec(**base, report_from="DATE '2026-01-01'")


def test_report_bounds_must_not_be_blank():
    with pytest.raises(ValueError, match="report_until"):
        _spec(7, report_until="  ")


def test_active_is_an_event_measure_not_a_property_one():
    with pytest.raises(ValueError, match="allows measures"):
        EventsSpec(
            table="ev", entity="user_id", event_time="occurred_at",
            measure="active", on="property", of="amount",
        )


# ---- dialects ---------------------------------------------------------------


@pytest.mark.parametrize("dialect", SUPPORTED)
@pytest.mark.parametrize("breakdowns", [(), ("platform",)])
def test_every_dialect_builds_with_nothing_left_unspliced(dialect, breakdowns):
    spec = _spec(
        7, exact=False, breakdowns=breakdowns,
        report_from="DATE '2026-03-01'", report_until="DATE '2026-04-01'",
    )
    sql = events_sql(spec, dialect)
    assert "factcat_date_shift" not in sql.lower()
    assert "factcat_ndv" not in sql.lower()
    assert "factcat_period_grid" in sql  # the grid is defined, then joined


@pytest.mark.parametrize(
    ("dialect", "needle"),
    [
        ("bigquery", "DATE_ADD(CAST(x AS DATE), INTERVAL (n) DAY)"),
        ("snowflake", "DATEADD('day', n, CAST(x AS DATE))"),
        ("databricks", "DATE_ADD(CAST(x AS DATE), n)"),
        ("trino", "DATE_ADD('day', n, CAST(x AS DATE))"),
        ("clickhouse", "addDays(CAST(x AS DATE), n)"),
        ("redshift", "CAST(DATEADD(day, n, CAST(x AS DATE)) AS DATE)"),
        ("postgres", "(CAST(x AS DATE) + CAST(n AS INTEGER))"),
        ("duckdb", "(CAST(x AS DATE) + CAST(n AS INTEGER))"),
    ],
)
def test_date_shift_per_dialect(dialect, needle):
    assert date_shift("x", "n", dialect) == needle


@pytest.mark.parametrize(
    ("source", "dialect", "wrong"),
    [
        # The column is dropped: nothing left to shift by.
        ("DATE_ADD(CAST(a.d AS DATE), INTERVAL (p.n) DAY)", "snowflake", "DATEADD(DAY, INTERVAL DAY,"),
        ("DATE_ADD(CAST(a.d AS DATE), INTERVAL (p.n) DAY)", "redshift", "DATEADD(DAY, INTERVAL DAY,"),
        # The column becomes the string 'n'.
        ("DATE_ADD(CAST(a.d AS DATE), INTERVAL (p.n) DAY)", "postgres", "INTERVAL 'n DAY'"),
        # An interval nested inside an interval.
        ("DATE_ADD(CAST(a.d AS DATE), INTERVAL (p.n) DAY)", "bigquery", "INTERVAL (INTERVAL (p.n) DAY) DAY"),
        # DuckDB's TO_DAYS leaks into a dialect that has no such function.
        ("DATE_ADD(CAST(a.d AS DATE), TO_DAYS(p.n))", "snowflake", "TO_DAYS(p.n)"),
        # DATE + integer passes through untouched; BigQuery and Trino have no
        # such operator.
        ("CAST(a.d AS DATE) + CAST(p.n AS INTEGER)", "bigquery", "+ CAST(p.n AS INT64)"),
        ("CAST(a.d AS DATE) + CAST(p.n AS INTEGER)", "trino", "+ CAST(p.n AS INTEGER)"),
    ],
)
def test_sqlglot_cannot_emit_a_per_row_day_shift(source, dialect, wrong):
    """Why ``date_shift`` is written per dialect instead of transpiled.

    Every DuckDB spelling of "this date plus ``n`` days, ``n`` a column" comes
    out of sqlglot wrong for at least one supported dialect, and silently. If a
    case here goes red, sqlglot has learned that spelling: check every dialect,
    then move ``date_shift`` onto ``transpile`` and delete its branches.
    """
    import sqlglot

    out = sqlglot.transpile(f"SELECT {source} FROM t", read="duckdb", write=dialect)[0]
    assert wrong in out, out


def test_date_shift_rejects_an_unknown_dialect():
    with pytest.raises(ValueError, match="unsupported dialect"):
        date_shift("x", "1", "oracle")
