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

`retained` is arbitrary SQL over any column in your table, plus the derived columns
`offset_days`, `period_index` and `within_period_offset`.

Generates SQL and queries in place. No SDK, no ingestion, no copy of your data. Supports
DuckDB, Postgres, BigQuery, Snowflake, Databricks, Spark, Trino, Presto, ClickHouse and
Redshift.

Full documentation: https://github.com/gordonkjlee/factcat
