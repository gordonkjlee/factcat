"""Events breakdowns. Ground truth is hand-computed from the fixture.

Mutation: ignore ``breakdowns`` (always one series) and the US/UK as-of
tests go red. Drop the ``(other)`` fold and the high-card test goes red.
Carried/bounds mutations that must go red: build stamps from ``src`` not
the table (S1's pre-window stamp is lost); drop the ``until`` bound
(as-of-start returns silver); substitute unbounded ``last`` for as-of-end
(returns gold); flip the stamp/needle tie key (S2 loses its own-instant
stamp); drop the value tiebreak (S4 flips); drop ``fill_from`` (S5 gains
enterprise); drop ``WHERE fc_is_row = 1`` (stamp rows inflate totals);
apply spec ``breakdown_at`` over per-column ``at`` (the mixed test);
drop ``backfill`` (S2 stays NULL at range start).
"""

from __future__ import annotations

import duckdb
import pytest

from factcat import Breakdown, EventsSpec, events_sql
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
        rest = list(row[1:])
        value = float(rest[-1])
        dims = rest[:-1]
        out.append((bucket, *dims, value))
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


def test_duckdb_approx_top_k_executes(pages):
    spec = EventsSpec(
        table="pages",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        exact=False,
        breakdowns=("path",),
        breakdown_labels=("path",),
        top_n=3,
        include_other=True,
    )
    sql = events_sql(spec, dialect="duckdb")
    assert "approx_top_k" in sql.lower()
    got = {path: value for _, path, value in _rows(pages, spec)}
    assert got == {"/a": 6.0, "/b": 5.0, "/c": 4.0, OTHER_LABEL: 6.0}
    assert sum(got.values()) == 21.0


def test_fold_is_a_join_not_correlated_exists():
    sql = events_sql(_spec(), dialect="bigquery").upper()
    assert "EXISTS" not in sql
    assert "LEFT JOIN" in sql
    assert "TOP_LABELS" in sql


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


# country × browser on one day. Counts: US/Chrome 5, US/Firefox 4,
# UK/Chrome 3, UK/Firefox 2, DE/Chrome 1. Total 15.
PAIR_HITS: list[tuple[str, str, str, str]] = []
for n, country, browser in (
    (5, "US", "Chrome"),
    (4, "US", "Firefox"),
    (3, "UK", "Chrome"),
    (2, "UK", "Firefox"),
    (1, "DE", "Chrome"),
):
    for i in range(n):
        PAIR_HITS.append((f"{country}-{browser}-{i}", "2026-01-01", country, browser))


@pytest.fixture()
def pairs() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE hits ("
        "  entity_id VARCHAR,"
        "  occurred_at DATE,"
        "  country VARCHAR,"
        "  browser VARCHAR"
        ")"
    )
    con.executemany("INSERT INTO hits VALUES (?, ?, ?, ?)", PAIR_HITS)
    return con


def _pair_spec(**overrides) -> EventsSpec:
    base = dict(
        table="hits",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        exact=True,
        breakdowns=("country", "browser"),
        breakdown_labels=("country", "browser"),
    )
    base.update(overrides)
    return EventsSpec(**base)


def test_two_breakdowns_pair_counts(pairs):
    got = {
        (country, browser): value
        for _, country, browser, value in _rows(pairs, _pair_spec(top_n=8))
    }
    assert got == {
        ("US", "Chrome"): 5.0,
        ("US", "Firefox"): 4.0,
        ("UK", "Chrome"): 3.0,
        ("UK", "Firefox"): 2.0,
        ("DE", "Chrome"): 1.0,
    }
    assert OTHER_LABEL not in {c for c, _ in got} | {b for _, b in got}


def test_top_n_ranks_pairs_not_first_column(pairs):
    """US is the top country (9) but DE/Chrome (1) is not a top-3 pair.

    Nested top-N countries would still show DE. Pair ranking must not.
    """
    got = {
        (country, browser): value
        for _, country, browser, value in _rows(pairs, _pair_spec(top_n=3))
    }
    assert got == {
        ("US", "Chrome"): 5.0,
        ("US", "Firefox"): 4.0,
        ("UK", "Chrome"): 3.0,
        (OTHER_LABEL, OTHER_LABEL): 3.0,
    }
    assert ("DE", "Chrome") not in got
    assert "DE" not in {c for c, _ in got}
    assert sum(got.values()) == 15.0


def test_pair_null_axis_stays_null(pairs):
    pairs.execute("INSERT INTO hits VALUES ('N1', '2026-01-01', 'US', NULL)")
    got = {
        (country, browser): value
        for _, country, browser, value in _rows(pairs, _pair_spec(top_n=8))
    }
    assert got[("US", None)] == 1.0
    assert OTHER_LABEL not in {got_k for pair in got for got_k in pair}


def test_three_breakdowns_compile():
    spec = EventsSpec(
        table="events",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        exact=True,
        breakdowns=("country", "browser", "plan"),
        breakdown_labels=("country", "browser", "plan"),
        top_n=8,
    )
    sql = events_sql(spec, dialect="duckdb")
    assert "fc_bd_2" in sql
    assert "plan" in sql
    assert "APPROX_TOP" not in sql.upper()
    assert "LIMIT" in sql.upper()


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


# entity_id, occurred_at, event_name, plan_tier. Logins are the metric;
# plan_tier is stamped sparsely. Hand-computed paper for every mode:
#   S1  free stamped pre-window (Dec 1); pro stamped Jan 5.
#       carried: Jan 2 login = free, Jan 6 login = pro.
#   S2  stamp and login share one instant (Jan 3 08:00): the login sees it.
#   S3  never stamped: NULL in every mode, never (other).
#   S4  two stamps at one instant (pro, free): greatest value (pro) wins.
#   S5  enterprise stamped on profile_update only: fill_from
#       'subscription_started' excludes it.
#   S6  bronze Dec 15, silver Jan 10, gold Feb 5. With a January window:
#       as-of start = bronze, as-of end = silver, last ever = gold,
#       first-in-window = silver. Every anchor answers differently.
SUBS = [
    ("S1", "2025-12-01 00:00:00", "subscription_started", "free"),
    ("S1", "2026-01-02 10:00:00", "login", None),
    ("S1", "2026-01-05 09:00:00", "subscription_started", "pro"),
    ("S1", "2026-01-06 10:00:00", "login", None),
    ("S2", "2026-01-03 08:00:00", "subscription_started", "pro"),
    ("S2", "2026-01-03 08:00:00", "login", None),
    ("S3", "2026-01-04 12:00:00", "login", None),
    ("S4", "2026-01-01 00:00:00", "subscription_started", "pro"),
    ("S4", "2026-01-01 00:00:00", "subscription_started", "free"),
    ("S4", "2026-01-02 12:00:00", "login", None),
    ("S5", "2026-01-01 06:00:00", "profile_update", "enterprise"),
    ("S5", "2026-01-02 07:00:00", "login", None),
    ("S6", "2025-12-15 00:00:00", "subscription_started", "bronze"),
    ("S6", "2026-01-10 00:00:00", "subscription_started", "silver"),
    ("S6", "2026-01-20 09:00:00", "login", None),
    ("S6", "2026-02-05 00:00:00", "subscription_started", "gold"),
    # S7 mirrors S4 with the opposite insertion order (free before pro),
    # so dropping the value tiebreak flips the attr answer under a stable
    # sort; S4's order catches the carried side of the same mutation.
    ("S7", "2026-01-15 00:00:00", "subscription_started", "free"),
    ("S7", "2026-01-15 00:00:00", "subscription_started", "pro"),
    ("S7", "2026-01-16 10:00:00", "login", None),
    # S8's only stamp lands at exactly the window's EXCLUSIVE end — the
    # midnight snapshot job. before=end must not see it; until=end would.
    ("S8", "2026-01-25 08:00:00", "login", None),
    ("S8", "2026-02-01 00:00:00", "subscription_started", "edge"),
    # S9's login carries its own speculative 'trial' while the
    # authoritative stream says basic — the own_value_first paper.
    ("S9", "2026-01-03 09:00:00", "subscription_started", "basic"),
    ("S9", "2026-01-10 11:00:00", "login", "trial"),
]

WINDOW_START = "TIMESTAMP '2026-01-01 00:00:00'"
WINDOW_END = "TIMESTAMP '2026-02-01 00:00:00'"


@pytest.fixture()
def subs() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE subs ("
        "  entity_id VARCHAR,"
        "  occurred_at TIMESTAMP,"
        "  event_name VARCHAR,"
        "  plan_tier VARCHAR"
        ")"
    )
    con.executemany("INSERT INTO subs VALUES (?, ?, ?, ?)", SUBS)
    return con


def _subs_spec(*breakdowns, **overrides) -> EventsSpec:
    base = dict(
        table="subs",
        entity="entity_id",
        event_time="occurred_at",
        measure="total",
        exact=True,
        where="event_name = 'login'",
        breakdowns=tuple(breakdowns) or (Breakdown("plan_tier", at="carried"),),
        breakdown_labels=None,
    )
    base.update(overrides)
    return EventsSpec(**base)


def test_carried_resolves_stamps_outside_window_and_where(subs):
    """S1's free stamp is pre-window AND outside where (not a login).

    Mutations: stamps built from ``src`` (the filtered scan) lose it;
    so does bounding the stamp scan to the chart window.
    """
    got = {(d, t): v for d, t, v in _rows(subs, _subs_spec())}
    assert got[("2026-01-02", "free")] == 1.0  # S1 before the upgrade
    assert got[("2026-01-06", "pro")] == 1.0  # S1 after it
    assert got[("2026-01-20", "silver")] == 1.0  # S6: latest at-or-before


def test_carried_same_instant_stamp_is_seen(subs):
    """S2's stamp shares the login's instant; the login sees it."""
    got = {(d, t): v for d, t, v in _rows(subs, _subs_spec())}
    assert got[("2026-01-03", "pro")] == 1.0
    assert ("2026-01-03", None) not in got


def test_carried_same_instant_greatest_value_wins(subs):
    """S4 has pro and free stamped at one instant; pro > free."""
    got = {(d, t): v for d, t, v in _rows(subs, _subs_spec())}
    assert got[("2026-01-02", "pro")] == 1.0
    assert ("2026-01-02", "free") in got  # that one is S1, not S4


def test_carried_never_stamped_stays_null_not_other(subs):
    got = {(d, t): v for d, t, v in _rows(subs, _subs_spec(top_n=1))}
    assert got[("2026-01-04", None)] == 1.0
    assert all(t != OTHER_LABEL for d, t in got if d == "2026-01-04")


def test_carried_fill_from_excludes_other_stamps(subs):
    """S5's enterprise lives on profile_update; fill_from must drop it."""
    spec = _subs_spec(
        Breakdown(
            "plan_tier",
            at="carried",
            fill_from="event_name = 'subscription_started'",
        )
    )
    got = {(d, t): v for d, t, v in _rows(subs, spec)}
    assert got[("2026-01-02", None)] == 1.0  # S5, no authoritative stamp
    without = {(d, t): v for d, t, v in _rows(subs, _subs_spec())}
    assert without[("2026-01-02", "enterprise")] == 1.0


def test_attr_fill_from_excludes_other_stamps(subs):
    """S5's enterprise lives on profile_update; fill_from must drop it on
    the first/last path too, not only under carried.

    Mutation: drop the fill_from condition from the attr CTE's WHERE.
    """
    spec = _subs_spec(
        Breakdown(
            "plan_tier",
            at="last",
            fill_from="event_name = 'subscription_started'",
        )
    )
    got = {(d, t): v for d, t, v in _rows(subs, spec)}
    assert got[("2026-01-02", None)] == 1.0  # S5: no authoritative stamp
    without = {(d, t): v for d, t, v in _rows(subs, _subs_spec(
        Breakdown("plan_tier", at="last")
    ))}
    assert without[("2026-01-02", "enterprise")] == 1.0


def test_own_value_first_beats_narrowed_fill_from(subs):
    """S9's charted login carries its own speculative 'trial' while the
    fill_from stream says basic. Source-of-truth (default) ignores the
    row's own value; own_value_first keeps it. Without fill_from the two
    contracts agree — the row is its own stamp.

    Mutation: drop the COALESCE / fc_self column and the own-first case
    returns basic.
    """
    authoritative = _subs_spec(
        Breakdown(
            "plan_tier",
            at="carried",
            fill_from="event_name = 'subscription_started'",
        )
    )
    got = {(d, t): v for d, t, v in _rows(subs, authoritative)}
    assert got[("2026-01-10", "basic")] == 1.0
    own_first = _subs_spec(
        Breakdown(
            "plan_tier",
            at="carried",
            fill_from="event_name = 'subscription_started'",
            own_value_first=True,
        )
    )
    got_own = {(d, t): v for d, t, v in _rows(subs, own_first)}
    assert got_own[("2026-01-10", "trial")] == 1.0
    plain = {(d, t): v for d, t, v in _rows(subs, _subs_spec())}
    assert plain[("2026-01-10", "trial")] == 1.0


def test_own_value_first_requires_carried():
    with pytest.raises(ValueError, match="own_value_first"):
        Breakdown("plan", at="last", own_value_first=True)


def test_carried_slices_sum_to_unsplit_total(subs):
    """Attribution assigns groups; it must not invent or drop rows.

    Mutation: drop ``WHERE fc_is_row = 1`` and stamp rows inflate this.
    """
    unsplit = {
        d: v
        for d, v in _rows(
            subs,
            EventsSpec(
                table="subs",
                entity="entity_id",
                event_time="occurred_at",
                measure="total",
                exact=True,
                where="event_name = 'login'",
            ),
        )
    }
    split = _rows(subs, _subs_spec())
    by_day: dict[str, float] = {}
    for d, _, v in split:
        by_day[d] = by_day.get(d, 0.0) + v
    assert by_day == unsplit


def test_every_anchor_answers_differently_on_s6(subs):
    """S6: bronze pre-window, silver in-window, gold post-window.

    Mutations: drop the ``until`` bound and as-of-start returns silver;
    substitute unbounded last for as-of-end and it returns gold.
    """
    def tier(spec: EventsSpec) -> str | None:
        got = {(d, t): v for d, t, v in _rows(subs, spec)}
        hits = [t for (d, t) in got if d == "2026-01-20"]
        assert len(hits) == 1
        return hits[0]

    asof_start = _subs_spec(Breakdown("plan_tier", at="last", until=WINDOW_START))
    asof_end = _subs_spec(Breakdown("plan_tier", at="last", until=WINDOW_END))
    last_ever = _subs_spec(Breakdown("plan_tier", at="last"))
    first_in_window = _subs_spec(
        Breakdown("plan_tier", at="first", since=WINDOW_START, until=WINDOW_END)
    )
    first_ever = _subs_spec(Breakdown("plan_tier", at="first"))
    assert tier(asof_start) == "bronze"
    assert tier(asof_end) == "silver"
    assert tier(last_ever) == "gold"
    assert tier(first_in_window) == "silver"
    assert tier(first_ever) == "bronze"


def test_asof_boundary_is_inclusive(subs):
    """S4's stamps land exactly at the window-start instant and count."""
    spec = _subs_spec(Breakdown("plan_tier", at="last", until=WINDOW_START))
    got = {(d, t): v for d, t, v in _rows(subs, spec)}
    assert got[("2026-01-02", "pro")] == 1.0  # S4: at-boundary, greatest


def test_before_bound_excludes_the_boundary_instant(subs):
    """S8's stamp sits at exactly the exclusive window end. ``before``
    keeps it out (the stamp belongs to the next period); ``until`` — the
    inclusive-anchor spelling — would read it.

    Mutation: substitute ``<=`` for ``<`` (or until for before).
    """
    strict = _subs_spec(Breakdown("plan_tier", at="last", before=WINDOW_END))
    got = {(d, t): v for d, t, v in _rows(subs, strict)}
    assert got[("2026-01-25", None)] == 1.0
    inclusive = _subs_spec(Breakdown("plan_tier", at="last", until=WINDOW_END))
    got_inc = {(d, t): v for d, t, v in _rows(subs, inclusive)}
    assert got_inc[("2026-01-25", "edge")] == 1.0
    filled = _subs_spec(
        Breakdown("plan_tier", at="last", before=WINDOW_END, backfill=True)
    )
    got_fill = {(d, t): v for d, t, v in _rows(subs, filled)}
    assert got_fill[("2026-01-25", "edge")] == 1.0  # backfill from first ever


def test_attr_same_instant_greatest_value_wins(subs):
    """S7 has free and pro stamped at one instant, free inserted first;
    the attr pick must choose pro by value, not by a stable sort."""
    spec = _subs_spec(Breakdown("plan_tier", at="last", until=WINDOW_END))
    got = {(d, t): v for d, t, v in _rows(subs, spec)}
    assert got[("2026-01-16", "pro")] == 1.0


def test_backfill_fills_only_entities_empty_at_the_anchor(subs):
    """S2 has nothing by Jan 1 and backfills to pro; S1's real as-of
    value (free) is never overridden; S3 has nothing to backfill from."""
    spec = _subs_spec(
        Breakdown("plan_tier", at="last", until=WINDOW_START, backfill=True)
    )
    got = {(d, t): v for d, t, v in _rows(subs, spec)}
    assert got[("2026-01-03", "pro")] == 1.0  # S2 backfilled
    assert got[("2026-01-02", "free")] == 1.0  # S1 stays as-of
    assert got[("2026-01-06", "free")] == 1.0
    assert got[("2026-01-04", None)] == 1.0  # S3 stays NULL
    strict = _subs_spec(Breakdown("plan_tier", at="last", until=WINDOW_START))
    strict_got = {(d, t): v for d, t, v in _rows(subs, strict)}
    assert strict_got[("2026-01-03", None)] == 1.0


def test_carried_with_attr_column_slices_sum_to_unsplit_total(subs):
    """carried + first in one tuple exercises the joined stream sliced;
    attribution must still neither invent nor drop rows."""
    unsplit = {
        d: v
        for d, v in _rows(
            subs,
            EventsSpec(
                table="subs",
                entity="entity_id",
                event_time="occurred_at",
                measure="total",
                exact=True,
                where="event_name = 'login'",
            ),
        )
    }
    split = _rows(
        subs,
        _subs_spec(
            Breakdown("plan_tier", at="carried"),
            Breakdown("event_name", at="first"),
        ),
    )
    by_day: dict[str, float] = {}
    for d, _t, _e, v in split:
        by_day[d] = by_day.get(d, 0.0) + v
    assert by_day == unsplit


def test_mixed_semantics_per_column(subs):
    """One carried column and one rows column in the same tuple; the
    per-column ``at`` wins over the spec-level default.

    Mutation: apply ``breakdown_at`` to every column and S1's Jan 2 login
    groups as (NULL, login) instead of (free, login).
    """
    spec = _subs_spec(
        Breakdown("plan_tier", at="carried"),
        "event_name",
        breakdown_at="rows",
    )
    got = {
        (d, t, e): v for d, t, e, v in _rows(subs, spec)
    }
    assert got[("2026-01-02", "free", "login")] == 1.0
    assert ("2026-01-02", None, "login") not in got


def test_breakdown_object_matches_string_spec_sql():
    """A plain string plus breakdown_at is the same spec as a Breakdown."""
    for at in ("rows", "first", "last"):
        as_string = _spec(breakdown_at=at)
        as_object = _spec(
            breakdowns=(Breakdown("country", at=at),), breakdown_at="rows"
        )
        assert events_sql(as_string) == events_sql(as_object), at


def test_rows_only_spec_emits_no_stream_ctes():
    sql = events_sql(_spec())
    assert "fc_stamps" not in sql
    assert "fc_stream" not in sql


def test_breakdown_validation():
    with pytest.raises(ValueError, match="since/until"):
        Breakdown("plan", at="rows", until="TIMESTAMP '2026-01-01'")
    with pytest.raises(ValueError, match="since/until"):
        Breakdown("plan", at="carried", since="TIMESTAMP '2026-01-01'")
    with pytest.raises(ValueError, match="backfill"):
        Breakdown("plan", at="last", backfill=True)
    with pytest.raises(ValueError, match="backfill"):
        Breakdown("plan", at="first", until="x", backfill=True)
    with pytest.raises(ValueError, match="fill_from"):
        Breakdown("plan", at="rows", fill_from="event_name = 'x'")
    with pytest.raises(ValueError, match="fill_from"):
        Breakdown("plan", at="carried", fill_from="   ")
    with pytest.raises(ValueError, match="expr"):
        Breakdown("   ")


def test_pair_null_first_axis_stays_null(pairs):
    """Regression: the fold's match sentinel read t.fc_bd_0, so a matched
    tuple whose FIRST axis is NULL folded its other axes into (other)."""
    pairs.execute("INSERT INTO hits VALUES ('N2', '2026-01-01', NULL, 'Safari')")
    got = {
        (country, browser): value
        for _, country, browser, value in _rows(pairs, _pair_spec(top_n=8))
    }
    assert got[(None, "Safari")] == 1.0
    assert OTHER_LABEL not in {k for pair in got for k in pair}


def test_distinct_measure_with_breakdown_executes(subs):
    """Regression: folded was not the last CTE on the distinct path and
    per_entity followed without a comma — broken SQL on every dialect."""
    for breakdowns in (("event_name",), (Breakdown("plan_tier", at="carried"),)):
        spec = EventsSpec(
            table="subs",
            entity="entity_id",
            event_time="occurred_at",
            measure="distinct",
            on="property",
            of="event_name",
            exact=True,
            where="event_name = 'login'",
            breakdowns=breakdowns,
        )
        rows = _rows(subs, spec)
        assert rows, breakdowns
        assert all(v == 1.0 for *_dims, v in rows)
