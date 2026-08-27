"""Ground-truth tests for a retention definition no other tool can express.

A subscription business whose retention definition no product analytics tool
can express:

    1. Who is being measured:  the SUBSCRIPTION
    2. What is being measured: any subscription start date
    3. Period:                 35 days (30-day billing + 5 days dunning)
    4. Retained:               payment collected; churned if dunning fails

The entity is not the user, the period is not a calendar bucket, and "retained"
is a payment state rather than an event occurrence. Every expected matrix below
is computed by hand from the fixture in ``conftest.py``.
"""

from __future__ import annotations

from factcat import RetentionSpec, retention_sql

from .conftest import RETAINED


def _run(con, spec: RetentionSpec) -> dict[tuple[str, int], float]:
    rows = con.execute(retention_sql(spec)).fetchall()
    return {(str(cohort)[:10], int(period)): pct for cohort, period, _, _, pct in rows}


def _spec(**overrides) -> RetentionSpec:
    base = dict(
        table="payments",
        entity="subscription_id",
        entity_time="sub_start",
        event_time="paid_at",
        period_days=35,
        n_periods=2,
        retained=RETAINED,
    )
    base.update(overrides)
    return RetentionSpec(**base)


def test_retention_at_subscription_grain(con):
    """The grain the business actually bills at."""
    got = _run(con, _spec())

    # January cohort is S1, S2, S3.
    #   P0  all three pay on day 0                      -> 3/3
    #   P1  S1 (offset 0) and S2 (offset 3); S3 offset 6 -> 2/3
    #   P2  S1 only                                      -> 1/3
    assert got[("2026-01-01", 0)] == 100.00
    assert got[("2026-01-01", 1)] == 66.67
    assert got[("2026-01-01", 2)] == 33.33
    # February cohort is S4, whose day-35 payment FAILED.
    assert got[("2026-02-01", 0)] == 100.00
    assert got[("2026-02-01", 1)] == 0.00
    assert got[("2026-02-01", 2)] == 0.00


def test_same_data_at_user_grain_gives_a_different_answer(con):
    """Entity is a modelling decision, not a vendor's. U1 holds two subscriptions."""
    got = _run(con, _spec(entity="user_id"))

    # January cohort is U1 and U2.
    #   P1  U1 retained via either subscription; U2 missed the window -> 1/2
    assert got[("2026-01-01", 0)] == 100.00
    assert got[("2026-01-01", 1)] == 50.00
    assert got[("2026-01-01", 2)] == 50.00


def test_period_length_is_arbitrary(con):
    """At 30 days, S2's day-38 payment lands in period 1 at offset 8 - outside dunning."""
    got = _run(con, _spec(period_days=30))

    # S1 day 35 -> period 1 offset 5, just inside. S2 day 38 -> offset 8, out.
    # S3 day 41 -> offset 11, out. So 1 of 3.
    assert got[("2026-01-01", 1)] == 33.33


def test_failed_payments_never_retain(con):
    """Mutation guard: with the predicate disabled, February period 1 becomes 100%."""
    got = _run(con, _spec(retained="1 = 1"))

    assert got[("2026-02-01", 1)] == 100.00, (
        "the naive 'any event retains' model counts a FAILED payment as "
        "retention - this is the number the incumbent tools report"
    )
