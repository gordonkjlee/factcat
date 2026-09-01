# factcat

An open-source alternative to Amplitude and Mixpanel that runs in your own data
warehouse. Factcat generates SQL and runs it in your BigQuery or Snowflake —
no SDK, no ingestion, nothing hosted.

Product analytics tools make your modelling decisions for you: `entity = a user`,
`period = a calendar bucket`, `retained = did an event`. Real definitions violate all
three:

```python
from factcat import RetentionSpec, retention_sql

spec = RetentionSpec(
    table="analytics.fct_subscription_payments",
    entity="subscription_id",   # not the user
    entity_time="sub_start",
    event_time="paid_at",
    period_days=35,             # a billing cycle plus dunning, not a calendar bucket
    n_periods=12,
    retained="status = 'collected' AND within_period_offset <= 5",
)

print(retention_sql(spec, dialect="snowflake"))
```

```python
from factcat import EventsSpec, events_sql

print(events_sql(EventsSpec(
    table="analytics.fct_events",
    entity="subscription_id",
    event_time="occurred_at",
    measure="uniques",
)))
```

Breakdowns are caller SQL plus optional ``top_n`` (default 8) and
``include_other`` (default True). Value semantics ride per column: a plain
string means ``breakdown_at`` (``rows`` / ``first`` / ``last`` /
``carried``); a ``Breakdown`` entry adds ``fill_from``, ``since`` /
``until`` bounds, and ``backfill``. ``carried`` is the last non-null value
at or before each row's instant over the entity's unfiltered history.
None of it replaces the expression.

```python
print(events_sql(EventsSpec(
    table="analytics.fct_events",
    entity="subscription_id",
    event_time="occurred_at",
    measure="uniques",
    breakdowns=("country", "browser"),
    top_n=8,
)))
```

Event measures: `total`, `uniques`, `average` (Total / Uniques). Property
measures (`on="property"`, `of=` a column): `sum`, `average`, `median`,
`distinct` (mean distinct values per entity). Uniques is `COUNT DISTINCT` of
`entity` when `exact=True`; default `exact=False` is approx NDV, approx
median, and approx top-N breakdown labels (BigQuery and Snowflake included).
The same `exact` field turns every sketch off.

`retained` is arbitrary SQL over any column in your table, plus the derived columns
`offset_days`, `period_index` and `within_period_offset`.

Generates SQL and queries in place. No SDK, no ingestion, no copy of your data.

SQL generation supports DuckDB, Postgres, BigQuery, Snowflake, Databricks, Spark, Trino,
Presto, ClickHouse and Redshift. Execute adapters push that SQL into the caller's
warehouse. Factcat has no warehouse of its own. ``pip install factcat`` is the
product (SQL + chart) and includes no warehouse SDK. Run queries with
``pip install factcat[bigquery]`` or ``factcat[snowflake]``. ``factcat[all]`` is
every shipped driver. The adapter contract is ``dialect`` plus ``run(sql)``.

```python
from factcat import RetentionSpec, retention_sql
from factcat.warehouses import connect

sql = retention_sql(spec, dialect="bigquery")
bq = connect("bigquery", project="my-proj", location="EU")
result = bq.run(sql)
```

Snowflake is the same shape with that warehouse's fields (`account`, `user`,
`warehouse`, `database`, `schema`, `private_key_path`). It has no scan-cap dry-run.

Application-default credentials by default (`gcloud auth application-default login`), or
pass a service-account JSON path as `credentials`. BigQuery queries are capped at 10 GiB
scanned unless you raise `maximum_bytes_billed` or pass `None` for unlimited. `project` and
`location` are required so an EU dataset is not sent to US.

Full documentation: https://github.com/gordonkjlee/factcat
