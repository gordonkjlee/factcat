# factcat

Product analytics on the event model already in your warehouse. Your grain, your periods,
your definitions.

Every other product analytics tool hard-codes `entity = user`, `period = a calendar bucket`,
and `retained = did any event`. Real definitions violate all three:

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
``include_other`` (default True). ``breakdown_at`` is ``rows`` / ``first`` /
``last`` and does not replace the expression.

```python
print(events_sql(EventsSpec(
    table="analytics.fct_events",
    entity="subscription_id",
    event_time="occurred_at",
    measure="uniques",
    breakdowns=("country",),
    top_n=8,
)))
```

Event measures: `total`, `uniques`, `average` (Total / Uniques). Property
measures (`on="property"`, `of=` a column): `sum`, `average`, `median`,
`distinct` (mean distinct values per entity). Uniques is `COUNT DISTINCT` of
`entity` when `exact=True`; default `exact=False` is approx NDV.

`retained` is arbitrary SQL over any column in your table, plus the derived columns
`offset_days`, `period_index` and `within_period_offset`.

Generates SQL and queries in place. No SDK, no ingestion, no copy of your data.

SQL generation supports DuckDB, Postgres, BigQuery, Snowflake, Databricks, Spark, Trino,
Presto, ClickHouse and Redshift. Execute adapters push that SQL into the caller's
warehouse. Factcat has no warehouse of its own. ``pip install factcat`` is the
product (SQL + chart) and includes no warehouse SDK. Run queries with
``pip install factcat[bigquery]``. Later warehouses are extras of the same
shape; ``factcat[all]`` is every shipped driver. The adapter contract is
``dialect`` plus ``run(sql)``.

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
`location` are required so an EU dataset is not sent to US.

Full documentation: https://github.com/gordonkjlee/factcat
