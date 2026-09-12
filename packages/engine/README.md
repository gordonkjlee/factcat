# factcat

[![CI](https://github.com/gordonkjlee/factcat/actions/workflows/ci.yml/badge.svg)](https://github.com/gordonkjlee/factcat/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/factcat)](https://pypi.org/project/factcat/) [![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue)](https://github.com/gordonkjlee/factcat/blob/main/LICENSE)

An open-source alternative to Amplitude and Mixpanel that runs in your own data
warehouse. Factcat generates SQL and runs it in your BigQuery (or Snowflake,
experimental) — no SDK, no ingestion, nothing hosted.

**Point it at an events table you already have** — one row per event, properties
as real columns. Python 3.10+ and credentials for your warehouse.

```bash
pip install "factcat[bigquery]"
cd /path/to/your/warehouse   # your mapping is saved here
factcat
```

That gives you a `factcat` command. Open http://127.0.0.1:8000. Setup asks which
table, which column identifies the thing you are counting, and which column is
the event timestamp. Then you have a chart.

Product analytics tools make your modelling decisions for you: `entity = a user`,
`period = a calendar bucket`, `retained = did an event`. Real definitions violate
all three, so Factcat makes all three yours:

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

**Status.** Factcat is 0.x. The SQL-generation API — `RetentionSpec`, `FunnelSpec`,
`EventsSpec` and the `*_sql` functions — is the surface that stays stable. A minor
release may change those dataclasses or the shape of `.factcat.json`, and says so at
the top of its release notes; a patch release never does. Pin with `factcat~=0.5.0`.

**Warehouses.** `pip install factcat` is SQL generation plus the local chart and
includes no warehouse SDK; `factcat[bigquery]` and `factcat[snowflake]` add the
official driver, `factcat[all]` adds every shipped one. BigQuery ships today;
**Snowflake is experimental** — its SQL is generated and compiled in CI against
Snowflake's grammar, but no live Snowflake account has ever executed it, so treat a
first run as a test. SQL generation alone also targets DuckDB, Postgres, Databricks,
Spark, Trino, Presto, ClickHouse and Redshift.

**Managed tables.** Factcat may create `fc_` tables in a write dataset you choose,
to make repeated breakdowns cheap; they hold entity ids and column values copied
from your events table, and they live in your warehouse. See the setup guide.

Long documentation: https://factcat.dev · Releases:
https://github.com/gordonkjlee/factcat/releases · Bugs and feedback:
https://github.com/gordonkjlee/factcat/issues
