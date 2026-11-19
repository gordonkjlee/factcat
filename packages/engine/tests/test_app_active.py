"""Active users through the app's form: lookback, Week and Month points,
overlay labels. Ground truth is counted by brute force from the fixture rows
(distinct entities with a row in the N days ending on each day), not from
the SQL under test.

Mutations that turn this file red:
- drop the ``lookback_days`` widening in ``_time_clauses``: the first days
  of the range undercount (``test_first_day_counts_the_days_before_the_range``);
- keep every daily row at Week grain (skip ``collapse_active_rows``): the
  week test sees seven points per week.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, timedelta

import duckdb
import pytest

from factcat_app.config import DEFAULTS
from factcat_app.query import (
    WAREHOUSE_KINDS,
    active_name,
    annotate_incomplete,
    collapse_active_rows,
    events_sql_from_form,
    spec_from_form,
)

TODAY = date.today()


def _fixture_rows() -> list[tuple[str, str, date]]:
    rng = random.Random(54)
    rows = []
    for i in range(14):
        for _ in range(rng.randint(1, 5)):
            rows.append((f"u{i}", rng.choice(["view", "buy"]), TODAY - timedelta(days=rng.randint(0, 80))))
    return rows


ROWS = _fixture_rows()


def _truth(day: date, window: int, event: str | None = None) -> int:
    first = day - timedelta(days=window - 1)
    return len({e for e, ev, d in ROWS if first <= d <= day and (event is None or ev == event)})


@pytest.fixture()
def con(monkeypatch) -> duckdb.DuckDBPyConnection:
    monkeypatch.setattr("factcat_app.query.WAREHOUSE_KINDS", (*WAREHOUSE_KINDS, "duckdb"))
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE main.events (user_id VARCHAR, event_name VARCHAR, occurred_at TIMESTAMP)"
    )
    connection.executemany(
        "INSERT INTO main.events VALUES (?, ?, ?)",
        [(e, ev, f"{d} 12:00:00") for e, ev, d in ROWS],
    )
    return connection


def _form(**extra) -> dict:
    base = {
        "kind": "duckdb",
        "table": "memory.main.events",
        "entity": "user_id",
        "event_time": "occurred_at",
        "event_time_tz": "reporting",
        "measure": "active",
        "window_days": 7,
        "grain": "day",
        "range_mode": "last",
        "range_unit": "day",
        "range_n": 14,
        "exact": True,
    }
    base.update(extra)
    return base


def _run(con, form) -> list[dict]:
    cur = con.execute(events_sql_from_form(form))
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _iso(b) -> str:
    return b if isinstance(b, str) else (b.date() if hasattr(b, "date") else b).isoformat()


def _by_day(rows, key="value") -> dict[date, int]:
    return {r["bucket"] if isinstance(r["bucket"], date) else r["bucket"].date(): r[key] for r in rows}


@pytest.mark.parametrize("window", [1, 7, 30])
def test_each_day_matches_brute_force(con, window):
    got = _by_day(_run(con, _form(window_days=window)))
    days = [TODAY - timedelta(days=k) for k in range(15)]
    assert set(got) <= set(days)
    for d in days:
        assert got.get(d, 0) == _truth(d, window), d


def test_first_day_counts_the_days_before_the_range(con):
    # 14 days shown, 30-day window: the earliest day reaches 29 days back.
    got = _by_day(_run(con, _form(window_days=30)))
    first = TODAY - timedelta(days=14)
    assert min(got) == first
    assert got[first] == _truth(first, 30)
    assert got[first] > _truth(first, 1)  # not just the day's own rows


def test_range_bounds_the_days_shown_not_the_scan(con):
    got = _by_day(_run(con, _form(window_days=30, range_n=3)))
    assert set(got) == {TODAY - timedelta(days=k) for k in range(4)}


def test_excluding_the_current_day_drops_it(con):
    got = _by_day(_run(con, _form(window_days=7, exclude_current=True)))
    assert TODAY not in got and TODAY - timedelta(days=1) in got


def test_week_points_are_the_last_days_value(con):
    form = _form(window_days=7, grain="week", range_unit="week", range_n=4, include_current=True)
    rows = collapse_active_rows(_run(con, form), form)
    got = {r["bucket"]: r["value"] for r in rows}
    this_week = TODAY - timedelta(days=TODAY.weekday())
    weeks = [this_week - timedelta(weeks=k) for k in range(3, -1, -1)]
    assert sorted(got) == [w.isoformat() for w in weeks]
    for w in weeks:
        as_of = min(w + timedelta(days=6), TODAY)
        assert got[w.isoformat()] == _truth(as_of, 7), w
    marked = annotate_incomplete(rows, form)
    assert [r["bucket"] for r in marked if r["incomplete"]] == [this_week.isoformat()]


def test_month_points_are_the_last_days_value(con):
    first = TODAY.replace(day=1) - timedelta(days=40)
    form = _form(
        window_days=30, grain="month", range_mode="custom", custom_kind="absolute",
        start_date=first.isoformat(), end_date=TODAY.isoformat(), include_current=True,
    )
    rows = collapse_active_rows(_run(con, form), form)
    got = {r["bucket"]: r["value"] for r in rows}
    this_month = TODAY.replace(day=1)
    assert this_month.isoformat() in got
    for key, value in got.items():
        start = date.fromisoformat(key)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        as_of = min(nxt - timedelta(days=1), TODAY)
        assert value == _truth(as_of, 30), key


def test_overlay_names_each_window_and_keeps_values_apart(con):
    form = _form(
        event_column="event_name",
        entity_label_plural="Users",
        series=[
            {"event": "view", "measure": "active", "window_days": 7},
            {"event": "view", "measure": "active", "window_days": 30},
            {"event": "buy", "measure": "total"},
        ],
    )
    rows = collapse_active_rows(_run(con, form), form)
    assert all("fc_roll" not in r for r in rows)
    series = {r["series"] for r in rows}
    assert series == {
        "view · Weekly active users (7 days)",
        "view · Monthly active users (30 days)",
        "buy",
    }
    for r in rows:
        if r["series"].startswith("view · Weekly"):
            d = r["bucket"] if isinstance(r["bucket"], date) else r["bucket"].date()
            assert r["value"] == _truth(d, 7, "view")


def test_overlay_mixing_active_and_volume_at_week_grain(con):
    form = _form(
        grain="week", range_unit="week", range_n=3, include_current=True,
        event_column="event_name",
        series=[
            {"event": "view", "measure": "active", "window_days": 7},
            {"event": "view", "measure": "total"},
        ],
    )
    rows = collapse_active_rows(_run(con, form), form)
    per = defaultdict(list)
    for r in rows:
        per[r["series"]].append(_iso(r["bucket"]))
    for buckets in per.values():
        assert len(buckets) == len(set(buckets)) <= 4
    assert len(per) == 2


def test_hour_and_cyclic_grains_are_refused():
    for grain in ("hour", "hour_of_day", "day_of_week"):
        with pytest.raises(ValueError, match="Choose Day, Week or Month"):
            spec_from_form(_form(grain=grain, kind="bigquery"))


@pytest.mark.parametrize("n", [0, 366, "x", "", None, 1.5, True])
def test_window_days_is_validated(n):
    with pytest.raises(ValueError, match="Days must be a whole number from 1 to 365"):
        spec_from_form(_form(window_days=n, kind="bigquery"))


def test_collapse_leaves_other_measures_and_day_grain_alone():
    rows = [{"bucket": "2026-11-02", "value": 3}]
    assert collapse_active_rows(rows, {"measure": "total", "grain": "week"}) is rows
    flagged = [{"bucket": "2026-11-02", "series": "a", "fc_roll": 1, "value": 3}]
    out = collapse_active_rows(flagged, {"measure": "active", "grain": "day"})
    assert out == [{"bucket": "2026-11-02", "series": "a", "value": 3}]


def test_collapse_keeps_as_of_day_and_today():
    form = {"measure": "active", "grain": "week", "week_start": "monday", "series": []}
    rows = [{"bucket": f"2026-11-{d:02d}", "value": d} for d in range(2, 12)]
    out = collapse_active_rows(rows, form, today=date(2026, 11, 11))
    # Mon 2 Nov week ends Sun 8 Nov; Mon 9 Nov week is current, as of Wed 11.
    assert out == [
        {"bucket": "2026-11-02", "value": 8},
        {"bucket": "2026-11-09", "value": 11},
    ]


def test_names_and_defaults():
    assert active_name(1) == "Daily active users (1 day)"
    assert active_name(7) == "Weekly active users (7 days)"
    assert active_name(30, "Members") == "Monthly active members (30 days)"
    assert active_name(14) == "Active users (14 days)"
    assert DEFAULTS["window_days"] == 7


def _api_run(monkeypatch, tmp_path, rows, **form):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from factcat.warehouses import QueryResult
    from factcat_app.main import app

    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=rows)
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    body = {
        "project": "p", "location": "EU", "table": "analytics.events",
        "entity": "account_id", "event_time": "occurred_at", "measure": "active",
        "window_days": 7, "grain": "week", "range_mode": "last", "range_unit": "week",
        "range_n": 4, "include_current": True, "credentials": "/tmp/sa.json",
    }
    body.update(form)
    res = TestClient(app).post("/api/run", json=body)
    return res.json(), warehouse


def test_run_endpoint_reduces_days_to_week_points(monkeypatch, tmp_path):
    monday = TODAY - timedelta(days=TODAY.weekday() + 14)
    rows = [{"bucket": str(monday + timedelta(days=k)), "value": k + 1} for k in range(7)]
    body, warehouse = _api_run(monkeypatch, tmp_path, rows)
    assert body["ok"] is True
    assert body["rows"] == [{"bucket": monday.isoformat(), "value": 7, "incomplete": False}]
    sql = warehouse.run.call_args.args[0]
    assert "COUNT(DISTINCT" in sql.upper().replace("APPROX_COUNT_DISTINCT", "COUNT(DISTINCT")
    # The caption is a header for the pane; the warehouse gets the bare SQL.
    assert not sql.lstrip().startswith("--")
    assert body["sql"].startswith("-- Weekly active users (WAU):")
    assert body["active_note"].endswith("the current week shows today's value.")


def test_run_endpoint_truncation_counts_fetched_rows(monkeypatch, tmp_path):
    monday = TODAY - timedelta(days=TODAY.weekday() + 14)
    rows = [{"bucket": str(monday + timedelta(days=k)), "value": 1} for k in range(7)]
    body, _ = _api_run(monkeypatch, tmp_path, rows, query_row_limit_run="7")
    assert len(body["rows"]) == 1
    assert body["truncated"] is True


def test_run_endpoint_refuses_hour_grain(monkeypatch, tmp_path):
    body, warehouse = _api_run(monkeypatch, tmp_path, [], grain="hour", range_unit="hour")
    assert body["ok"] is False
    assert "Choose Day, Week or Month" in body["error"]
    warehouse.run.assert_not_called()


def test_events_page_offers_the_active_options(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from factcat_app.main import app

    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(
        '{"project":"p","location":"EU","table":"a.events","dataset":"a",'
        '"table_name":"events","entity":"id","event_time":"t"}',
        encoding="utf-8",
    )
    html = TestClient(app).get("/events").text
    for value in ("active_1", "active_7", "active_30", "active_n"):
        assert f'value="{value}"' in html
    assert "Weekly active users (7 days)" in html
    assert 'id="active-note"' in html


def test_caption_names_the_window_in_words_and_the_tag():
    from factcat_app.query import active_caption

    assert active_caption(_form(window_days=7, kind="bigquery")) == (
        "Weekly active users (WAU): each point counts distinct users active "
        "in the 7 days ending that day."
    )
    assert active_caption(_form(window_days=1, kind="bigquery")) == (
        "Daily active users (DAU): each point counts distinct users active on that day."
    )
    custom = active_caption(_form(window_days=14, entity_label_plural="Accounts", kind="bigquery"))
    assert custom == (
        "Active accounts over 14 days: each point counts distinct accounts active "
        "in the 14 days ending that day."
    )
    assert active_caption(_form(measure="uniques", kind="bigquery")) is None


def test_caption_lists_every_window_and_the_as_of_rule():
    from factcat_app.query import active_caption

    form = _form(
        kind="bigquery",
        grain="month",
        series=[
            {"measure": "active", "window_days": 30},
            {"measure": "active", "window_days": 1},
            {"measure": "total"},
            {"measure": "active", "window_days": 30},
        ],
    )
    assert active_caption(form) == (
        "Daily active users (DAU), Monthly active users (MAU): each point counts "
        "distinct users active in that series' window (1 or 30 days) ending that day. "
        "A month shows the value on its last day; the current month shows today's value."
    )


def test_sql_pane_carries_the_caption_but_the_warehouse_does_not(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from factcat_app.main import app

    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    body = {
        "project": "p", "location": "EU", "table": "analytics.events",
        "entity": "account_id", "event_time": "occurred_at", "measure": "active",
        "window_days": 7, "grain": "day", "range_mode": "last", "range_unit": "day",
        "range_n": 30,
    }
    res = TestClient(app).post("/api/sql", json=body).json()
    assert res["ok"] is True
    assert res["active_note"].startswith("Weekly active users (WAU):")
    first, rest = res["sql"].split("\n", 1)
    assert first == "-- " + res["active_note"]
    assert rest == events_sql_from_form(body)
    assert not events_sql_from_form(body).lstrip().startswith("--")
    plain = TestClient(app).post("/api/sql", json={**body, "measure": "uniques"}).json()
    assert plain["active_note"] is None
    assert not plain["sql"].startswith("--")


def test_caption_header_cannot_break_out_of_the_comment(monkeypatch, tmp_path):
    from factcat_app.main import _pane_sql

    out = _pane_sql("SELECT 1", "Weekly active a\nDROP TABLE x: ...")
    assert out.count("\n") == 1
    assert out.endswith("\nSELECT 1")


def test_estimate_prices_the_lookback(monkeypatch, tmp_path):
    """The estimate is a dry run of the same SQL, so it scans N - 1 extra days."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from factcat.warehouses import QueryResult
    from factcat_app.main import app

    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=[], bytes_processed=10)
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    body = {
        "project": "p", "location": "EU", "table": "analytics.events",
        "entity": "account_id", "event_time": "occurred_at", "measure": "active",
        "window_days": 30, "grain": "day", "range_mode": "last", "range_unit": "day",
        "range_n": 30, "credentials": "/tmp/sa.json",
    }
    res = TestClient(app).post("/api/estimate", json=body).json()
    assert res["ok"] is True
    priced = [c for c in warehouse.run.call_args_list if c.kwargs.get("dry_run")]
    assert priced and "INTERVAL (-29) DAY" in priced[-1].args[0]
    warehouse.run.reset_mock()
    TestClient(app).post("/api/estimate", json={**body, "window_days": 1})
    priced = [c for c in warehouse.run.call_args_list if c.kwargs.get("dry_run")]
    assert priced and "INTERVAL (-" not in priced[-1].args[0]
