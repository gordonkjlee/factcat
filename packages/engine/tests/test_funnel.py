"""Ground-truth tests for ordered funnels.

The fixture is built to exercise the two things funnels get wrong: **ordering**
(a step only counts if it happens at or after the previous one) and the
**completion window** (measured from the first step, not the previous one).
"""

from __future__ import annotations

import duckdb
import pytest

from factcat import FunnelSpec, funnel_sql

# entity_id, event_name, occurred_at
EVENTS = [
    # E1 completes the whole funnel inside the window.
    ("E1", "view", "2026-03-01"),
    ("E1", "cart", "2026-03-02"),
    ("E1", "checkout", "2026-03-03"),
    # E2 stops at the cart.
    ("E2", "view", "2026-03-01"),
    ("E2", "cart", "2026-03-02"),
    # E3 checks out without ever adding to cart - must not skip a step.
    ("E3", "view", "2026-03-01"),
    ("E3", "checkout", "2026-03-02"),
    # E4 carts on day 10, outside a 7-day window.
    ("E4", "view", "2026-03-01"),
    ("E4", "cart", "2026-03-11"),
    ("E4", "checkout", "2026-03-12"),
    # E5 carted BEFORE viewing, so the cart does not count.
    ("E5", "cart", "2026-03-01"),
    ("E5", "view", "2026-03-02"),
    ("E5", "checkout", "2026-03-03"),
]


@pytest.fixture()
def events() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE events (entity_id VARCHAR, event_name VARCHAR, occurred_at DATE)"
    )
    con.executemany("INSERT INTO events VALUES (?, ?, ?)", EVENTS)
    return con


def _spec(**overrides) -> FunnelSpec:
    base = dict(
        table="events",
        entity="entity_id",
        event_time="occurred_at",
        steps=(
            "event_name = 'view'",
            "event_name = 'cart'",
            "event_name = 'checkout'",
        ),
        step_labels=("viewed", "carted", "checked out"),
        within_days=7,
    )
    base.update(overrides)
    return FunnelSpec(**base)


def test_ordered_funnel_counts(events):
    rows = events.execute(funnel_sql(_spec())).fetchall()
    counts = {int(i): (label, int(n), pct) for i, label, n, pct in rows}

    # Every entity has a view.
    assert counts[0] == ("viewed", 5, 100.00)
    # E1 and E2 only: E3 never carted, E4's cart is outside the window,
    # and E5 carted before viewing.
    assert counts[1] == ("carted", 2, 40.00)
    # E1 alone reaches checkout through the ordered path.
    assert counts[2] == ("checked out", 1, 20.00)


def test_a_step_cannot_be_skipped(events):
    """E3 checked out but never carted, so it must not reach the final step."""
    rows = events.execute(funnel_sql(_spec())).fetchall()
    checkout = next(int(n) for i, _, n, _ in rows if int(i) == 2)
    assert checkout == 1, "an out-of-order checkout was counted as a completion"


def test_widening_the_window_admits_the_late_cart(events):
    """Mutation guard: E4's day-10 cart is excluded only by the window."""
    rows = events.execute(funnel_sql(_spec(within_days=30))).fetchall()
    counts = {int(i): int(n) for i, _, n, _ in rows}

    assert counts[1] == 3, "widening the window should admit E4's late cart"
    assert counts[2] == 2, "E4 should now complete the funnel too"


def test_unbounded_window_is_allowed(events):
    rows = events.execute(funnel_sql(_spec(within_days=None))).fetchall()
    counts = {int(i): int(n) for i, _, n, _ in rows}
    assert counts[1] == 3
    assert counts[2] == 2


def test_labels_default_when_not_supplied(events):
    rows = events.execute(funnel_sql(_spec(step_labels=None))).fetchall()
    assert [label for _, label, _, _ in rows] == ["step_0", "step_1", "step_2"]


def test_a_funnel_needs_two_steps():
    with pytest.raises(ValueError, match="at least two steps"):
        FunnelSpec(
            table="events",
            entity="entity_id",
            event_time="occurred_at",
            steps=("event_name = 'view'",),
        )
