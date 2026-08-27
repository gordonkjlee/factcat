# Factcat

Product analytics on the event model already in your warehouse. Your grain, your periods,
your definitions.

Named for the two words its users already own: a **fact** table is what you build, and
**cat** is how you read a file. Factcat reads your fact tables where they sit.

## The problem

Every product analytics tool hard-codes three things. SaaS tools do it, and so do the
warehouse-native ones:

```
entity   = user
period   = a calendar bucket (day / week / month)
retained = did any event
```

Real businesses violate all three. Here is a retention definition negotiated in an actual
room, for an actual subscription business:

> 1. Who is being measured: **the subscription**
> 2. Period: **35 days.** Billing periods are 30 days with 5 extra days allotted for dunning
>    payment collection
> 3. How is retention measured: considered churned if we didn't collect payment from dunning
>    in 5 days

Not one line of that fits a product analytics tool. The entity is a subscription, not a
user. The period is not a calendar bucket. "Retained" is a payment state machine, not an
event occurrence. So it gets built by hand, in SQL, by a consultant, again.

## The same definition, in Factcat

```python
from factcat import RetentionSpec, retention_sql

spec = RetentionSpec(
    table="analytics.fct_subscription_payments",
    entity="subscription_id",       # not the user
    entity_time="sub_start",
    event_time="paid_at",
    period_days=35,                 # not a calendar bucket
    n_periods=12,
    retained="status = 'collected' AND within_period_offset <= 5",
)

print(retention_sql(spec, dialect="snowflake"))
```

`retained` is arbitrary SQL. It can reference any column in your table plus three derived
columns: `offset_days`, `period_index`, and `within_period_offset`. A five-day dunning
window is `within_period_offset <= 5`. That is the whole idea.

Funnels work the same way - ordered steps as arbitrary predicates, at whatever grain you
choose, with the completion window measured from the first step:

```python
from factcat import FunnelSpec, funnel_sql

spec = FunnelSpec(
    table="analytics.fct_events",
    entity="account_id",
    event_time="occurred_at",
    steps=("event = 'trial_start'", "event = 'invited_teammate'", "event = 'paid'"),
    step_labels=("trial", "invited a teammate", "converted"),
    within_days=30,
)
```

## Why the entity matters

The same table, the same predicate, two grains, two different answers:

| Grain | Period 0 | Period 1 | Period 2 |
|---|---|---|---|
| `subscription_id` | 100% | 66.67% | 33.33% |
| `user_id` | 100% | 50% | 50% |

Neither is wrong. One user held two subscriptions and let one lapse. Which number you want
depends on whether you are forecasting revenue or judging engagement - and that is a
modelling decision, not something a vendor should have made for you.

## What it is not

Factcat does not ship a tracking SDK, does not ingest, and never copies your data. It
generates SQL. You run that SQL on your warehouse - there is no Factcat login to BigQuery
or Snowflake. If you need event collection, keep using whatever you use.

## Recommended warehouse shape

The library accepts **any relation**. A payments fact with `status` and `subscription_id`
is a valid source. You do not have to have an events table.

For clickstream-style product analytics, this shape works well (it is a recommendation,
not a requirement):

1. **Events** - one table. At least an event name, a timestamp, and an entity id.
   Other fields are real columns, not a JSON blob. Different event types may share the
   table; unused columns are null, which is fine.
2. **Identity mapping** - source system id plus a type, resolved to one canonical entity
   id. Do that in the warehouse (dbt). Factcat does not merge anonymous and logged-in ids.
3. **Entity dimension** - current attributes (country, plan) as columns, joined when you
   want a breakdown.

Extra grains (booking, account, subscription) are extra **id columns on the events table**,
not "any property". A report that counts bookings simply ignores rows where that id is
null.

A PostHog or Amplitude export with JSON properties should be flattened into columns in
dbt before you point Factcat at it.

## Supported warehouses

DuckDB, Postgres, BigQuery, Snowflake, Databricks, Spark, Trino, Presto, ClickHouse and
Redshift.

Portability comes from [sqlglot](https://github.com/tobymao/sqlglot) rather than from ten
hand-written backends. Exactly one construct needs per-dialect code - generating a series of
integers for the period grid - and it lives in
[`dialects.py`](packages/engine/factcat/dialects.py). That file is the entire per-warehouse
surface area.

## Install

```bash
pip install factcat
```

To hack on the library:

```bash
pip install -e "packages/engine[dev]"
```

## Tests

```bash
cd packages/engine && python -m pytest
```

The suite runs against DuckDB with hand-computed ground truth, and every expected number in
`tests/test_retention_subscription_dunning.py` was worked out on paper from the fixture. It includes
mutation guards: disable the `retained` predicate and February's period 1 reports **100%
retention on a payment that failed**, which is what the naive "any event retains" model
tells you.

## Licence

MIT.
