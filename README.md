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

The everyday report is a time series. **Total**, **Uniques**, and **Average**
(Total / Uniques: events per unique entity). Uniques is `COUNT DISTINCT` of your
entity, not of users. On a numeric column: **Sum**, **Average**, **Median**, and
**Distinct** (mean distinct values per entity). Not min/max.

```python
from factcat import EventsSpec, events_sql

spec = EventsSpec(
    table="analytics.fct_events",
    entity="subscription_id",
    event_time="occurred_at",
    measure="uniques",
    where="event_name = 'paid'",
)
print(events_sql(spec, dialect="bigquery"))
```

Uniques, Distinct, and Median default to **approx** (`exact=False`):
`APPROX_COUNT_DISTINCT` / `APPROX_QUANTILES` on BigQuery. The local app’s
Exact toggle sets `exact=True` for `COUNT DISTINCT` / `PERCENTILE_CONT`.
Total, Sum, and property Average stay exact either way.

Day/week/month buttons in the app fill a `bucket` expression such as
`date_trunc('week', occurred_at)`. There is no `period: day|week|month` field.

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

Factcat does not ship a tracking SDK, does not ingest, and never copies your data. There
is no Factcat-hosted warehouse. You bring credentials to your own BigQuery (or, later,
Snowflake). If you need event collection, keep using whatever you use.

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

**SQL generation:** DuckDB, Postgres, BigQuery, Snowflake, Databricks, Spark, Trino,
Presto, ClickHouse and Redshift.

Portability comes from [sqlglot](https://github.com/tobymao/sqlglot) rather than from ten
hand-written backends. Exactly one construct needs per-dialect SQL - generating a series of
integers for the period grid - and it lives in
[`dialects.py`](packages/engine/factcat/dialects.py).

**Execute adapters** push that SQL into the caller's warehouse through its official
client. Factcat has no warehouse of its own. BigQuery ships today. The contract is
`dialect` plus `run(sql)` - identity, auth, and cost knobs stay on the concrete class so
Snowflake does not inherit `project` / `location` / `maximum_bytes_billed`. A later
warehouse is a module and one line in the registry; see the docstring on
`factcat.warehouses`.

```python
from factcat import RetentionSpec, retention_sql
from factcat.warehouses import connect

sql = retention_sql(spec, dialect="bigquery")
bq = connect("bigquery", project="my-proj", location="EU")
result = bq.run(sql)
```

Application-default credentials by default (`gcloud auth application-default login`), or
pass a service-account JSON path as `credentials`. Queries are capped at 10 GiB scanned
unless you raise `maximum_bytes_billed` or pass `None` for unlimited. `project` and
`location` are required.

## Install

One project: [factcat](https://pypi.org/project/factcat/).

```bash
pip install factcat              # SQL generation + the local chart
pip install factcat[bigquery]    # run queries in BigQuery
```

`pip install factcat[bigquery]` is one command (it installs factcat plus the
driver). The default has **no** warehouse SDK. Later warehouses are extras
named the same way (`factcat[snowflake]`). `factcat[all]` is every execute
adapter we ship.

To hack on the library:

```bash
pip install -e "packages/engine[dev,all]"
```

## Run the app

The app is a local web page. It does not ingest your data. It generates SQL and runs it
in **your** BigQuery. No Docker. Start it from **your warehouse repo** (or any project
directory); that is where `.factcat.json` is written.

**You need:** Python 3.10+ and the [Google Cloud SDK](https://cloud.google.com/sdk/docs/install).
A GCP project with the BigQuery API enabled, and a table you can query (BigQuery Job User
on the project, Data Viewer on the dataset).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install factcat[bigquery]
gcloud auth application-default login
gcloud config set project YOUR_GCP_PROJECT

cd /path/to/your/warehouse   # mapping is saved here
factcat-app
```

Open http://127.0.0.1:8000. First run opens **Setup** (`/setup`): billing
project from ADC, then dataset → table → entity id and timestamp. Catalog
lists load when you open a dropdown, not when you visit the page. Location
is taken from the dataset (do not guess `US`). **Save and open Events**
writes `.factcat.json` and goes to the Events chart (`/`). Setup is a
separate control at the bottom of the left rail, not an analysis.
Advanced is only if you use a key file instead of ADC.

Map the event-name column on **Setup** (STRING). Event names are cached
when you save Setup. On Events, **Event** is which name to chart
(**All events** = no filter); **Refresh list** reloads names from the last
90 days of the timestamp column (not an all-time scan). **Date range** is Last N days/weeks/months (optional exclude current
period), This/Previous period, or custom from/to — sugar on `event_time`,
not a period enum. **Week starts on** is set in Setup (Monday by default). Catalog dropdowns are alphabetical.
Entity lists string and integer columns; timestamp lists TIMESTAMP /
DATETIME.

If the table lives in another GCP project (billing in `dev`, data in `prod`), set
**Project that holds the table** before loading datasets.

There is no `user_id` default. **Entity name** on Setup is a display label
(default User; Other is free text). It does not pick the id column.
**Volume** is row count, **Unique User** (or Unique Customer, …) is distinct
of the mapped id, **Average per User** is Volume / that unique count (hover
the measure names). Day/week/month buckets are dates, not timestamps. The
filter pane sits beside Chart, Table, and SQL result panes.

Click **Run**. The mapping is written to `.factcat.json` in the directory where you started
`factcat-app`, so the next start is already filled in. Add `.factcat.json` to that repo’s
`.gitignore`. ADC lives in your user profile; you do not log in to Google every time you
start the app.

Queries are capped at 10 GiB scanned (the BigQuery adapter default). The form does not
expose that cap; raise `maximum_bytes_billed` from Python if a table needs more.

Stop the server with Ctrl+C. To use a key file instead of ADC, paste the JSON path in the
form. The app never copies your events off BigQuery.

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
