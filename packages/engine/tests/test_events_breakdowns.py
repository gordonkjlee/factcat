"""Events breakdowns. Ground truth is hand-computed from the fixture.

Mutation: ignore ``breakdowns`` (always one series) and the US/UK as-of
tests go red. Drop the ``(other)`` fold and the high-card test goes red.
"""

from __future__ import annotations

import duckdb
import pytest

from factcat import EventsSpec, events_sql
from factcat.spec import OTHER_LABEL

# entity_id, occurred_at, country, path, event_name
VISITS = [
    ("S1", "2026-01-01", "US", "/home", "paid"),
    ("S1", "2026-01-08", "UK", "/home", "paid"),
    ("S2", "2026-01-01", "US", "/home", "paid"),
    ("S2", "2026-01-08", "US", "/home", "paid"),
    # Country only on a row the metric where excludes.
    ("S3", "2025-12-01", "US", "/home", "signup"),
    ("S3", "2026-01-01", None, "/home", "paid"),
]

# path counts on 2026-01-01: /a 6, /b 5, /c 4, /d 3, /e 2, /f 1. Total 21.
PAGES: list[tuple[str, str, str]] = []
for n, path in ((6, "/a"), (5, "/b"), (4, "/c"), (3, "/d"), (2, "/e"), (1, "/f")):
    for i in range(n):
        PAGES.append((f"{path}-{i}", "2026-01-01", path))


@pytest.fixture()
def visits() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE visits ("
        "  entity_id VARCHAR,"
        "  occurred_at DATE,"
        "  country VARCHAR,"
        "  path VARCHAR,"
        "  event_name VARCHAR"
        ")"
    )
    con.executemany("INSERT INTO visits VALUES (?, ?, ?, ?, ?)", VISITS)
    return con


@pytest.fixture()
def pages() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE pages ("
        "  entity_id VARCHAR,"
        "  occurred_at DATE,"
        "  path VARCHAR"
        ")"
    )
    con.executemany("INSERT INTO pages VALUES (?, ?, ?)", PAGES)
    return con


def _spec(**overrides) -> EventsSpec:
    base = dict(
        table="visits",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        exact=True,
        where="event_name = 'paid'",
        breakdowns=("country",),
        breakdown_labels=("country",),
    )
    base.update(overrides)
    return EventsSpec(**base)


def _rows(con, spec: EventsSpec) -> list[tuple]:
    raw = con.execute(events_sql(spec)).fetchall()
    out = []
    for row in raw:
        bucket = str(row[0])[:10]
        if len(row) == 2:
            out.append((bucket, float(row[1])))
        else:
            out.append((bucket, row[1], float(row[2])))
    return out


def test_empty_breakdowns_sql_matches_plain():
    plain = EventsSpec(
        table="visits",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        exact=True,
    )
    extras = EventsSpec(
        table="visits",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        exact=True,
        breakdowns=(),
        breakdown_at="last",
        top_n=3,
        include_other=False,
    )
    assert events_sql(plain) == events_sql(extras)


def test_one_country_matches_unsplit_total(visits):
    visits.execute("UPDATE visits SET country = 'US' WHERE event_name = 'paid'")
    split = _rows(visits, _spec())
    plain = _rows(
        visits,
        EventsSpec(
            table="visits",
            entity="entity_id",
            event_time="occurred_at",
            measure="total",
            exact=True,
            where="event_name = 'paid'",
        ),
    )
    by_day = {d: v for d, v in plain}
    assert {c for _, c, _ in split} == {"US"}
    for day, country, value in split:
        assert country == "US"
        assert value == by_day[day]


def test_rows_first_last_differ_on_us_then_uk(visits):
    """S1 is US on the 1st and UK on the 8th. Hand-computed.

    rows: 1st US=2 (S1,S2), NULL=1 (S3); 8th UK=1 (S1), US=1 (S2).
    first: S1 and S2 and S3 (signup) are US. 1st US=3; 8th US=2.
    last: S1 is UK, S2 US, S3 US. 1st UK=1 US=2; 8th UK=1 US=1.
    """
    rows = _rows(visits, _spec(breakdown_at="rows"))
    assert set(rows) == {
        ("2026-01-01", "US", 2.0),
        ("2026-01-01", None, 1.0),
        ("2026-01-08", "UK", 1.0),
        ("2026-01-08", "US", 1.0),
    }
    first = _rows(visits, _spec(breakdown_at="first"))
    assert set(first) == {
        ("2026-01-01", "US", 3.0),
        ("2026-01-08", "US", 2.0),
    }
    last = _rows(visits, _spec(breakdown_at="last"))
    assert set(last) == {
        ("2026-01-01", "UK", 1.0),
        ("2026-01-01", "US", 2.0),
        ("2026-01-08", "UK", 1.0),
        ("2026-01-08", "US", 1.0),
    }


def test_sparse_stamp_found_by_first_not_rows(visits):
    """S3's US is on signup, outside where. rows → NULL; first → US."""
    rows = {(d, c): v for d, c, v in _rows(visits, _spec(breakdown_at="rows"))}
    first = {(d, c): v for d, c, v in _rows(visits, _spec(breakdown_at="first"))}
    assert rows[("2026-01-01", None)] == 1.0
    assert ("2026-01-01", None) not in first
    assert first[("2026-01-01", "US")] == 3.0


def test_low_cardinality_has_no_other(visits):
    got = _rows(visits, _spec(top_n=8))
    assert all(c != OTHER_LABEL for _, c, _ in got)


def test_top_n_other_adds_up(pages):
    spec = EventsSpec(
        table="pages",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        exact=True,
        breakdowns=("path",),
        breakdown_labels=("path",),
        top_n=3,
        include_other=True,
    )
    got = {path: value for _, path, value in _rows(pages, spec)}
    assert got == {"/a": 6.0, "/b": 5.0, "/c": 4.0, OTHER_LABEL: 6.0}
    assert sum(got.values()) == 21.0


def test_include_other_false_drops_the_tail(pages):
    spec = EventsSpec(
        table="pages",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        exact=True,
        breakdowns=("path",),
        breakdown_labels=("path",),
        top_n=3,
        include_other=False,
    )
    got = {path: value for _, path, value in _rows(pages, spec)}
    assert got == {"/a": 6.0, "/b": 5.0, "/c": 4.0}
    assert OTHER_LABEL not in got
    assert sum(got.values()) == 15.0


def test_labels_length_mismatch_is_rejected():
    with pytest.raises(ValueError, match="breakdown_labels"):
        EventsSpec(
            table="visits",
            entity="entity_id",
            event_time="occurred_at",
            measure="total",
            breakdowns=("country",),
            breakdown_labels=("a", "b"),
        )


def test_top_n_must_be_positive():
    with pytest.raises(ValueError, match="top_n"):
        EventsSpec(
            table="visits",
            entity="entity_id",
            event_time="occurred_at",
            measure="total",
            breakdowns=("country",),
            top_n=0,
        )


def test_invalid_breakdown_at_is_rejected():
    with pytest.raises(ValueError, match="breakdown_at"):
        EventsSpec(
            table="visits",
            entity="entity_id",
            event_time="occurred_at",
            measure="total",
            breakdowns=("country",),
            breakdown_at="person",  # type: ignore[arg-type]
        )
