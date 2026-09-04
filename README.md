# Factcat

<img src="packages/engine/factcat_app/static/waiting.jpg" width="200" alt="Factcat">

An open-source alternative to Amplitude and Mixpanel that runs in your own data
warehouse. Factcat generates SQL and runs it in your BigQuery (or Snowflake,
experimental) — no SDK, no ingestion, nothing hosted.

## The problem

Product analytics tools make your modelling decisions for you. The entity is a user
(accounts or groups are a paid add-on), a period is a calendar bucket, and "active",
"retained", or "converted" means an event occurred — never a predicate over your own
columns:

```
entity   = a user
period   = a calendar bucket (day / week / month)
retained = did an event
```

It starts with the simplest chart: weekly active *what*? Users, accounts, subscriptions,
bookings — that is a modelling decision, and Factcat's Uniques is `COUNT DISTINCT` of
whatever you say the entity is.

It compounds in the definitions that actually get negotiated. Here is a retention
definition from an actual room, for an actual subscription business:

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

On the Events chart, **Break down by** fills that expression (a column, or
SQL). **Add breakdown** adds a second or third; top N ranks the
**combination** (not nested top-N per property). As-of, top N, and Show
(other) are sugar; they do not replace the tuple. The chart legend joins
labels with a middle dot (``US · Chrome``); the table and CSV keep one
column per breakdown.

Split a series by caller expressions. ``top_n`` (default 8) folds a long
tail into ``(other)``; set ``include_other=False`` to drop the tail instead.
Each column carries its own value semantics: a plain string means
``breakdown_at`` (default ``rows`` — the value on the event); a
``Breakdown`` entry can instead take ``first`` / ``last`` (one non-null
value per entity, whole unfiltered table), the same bounded by ``since`` /
``until`` timestamp expressions ("as of the window start" is ``last`` plus
``until``), or ``carried`` — the last non-null value at or before each
row's instant, so a tier recorded only on ``subscription_started`` still
labels every later login. A row never borrows a future value.
``fill_from`` names which rows may supply a value (with
``own_value_first`` the charted row's own value still outranks the
narrowed stream); the strict ``before`` bound is the exclusive-boundary
spelling, so "state at the window end" never reads a value recorded at
exactly the end instant; ``backfill=True`` (with an upper bound) falls
back to the entity's first recorded value when nothing exists by it.
Attribution always reads the unfiltered table — the value can sit on an
event the chart excludes — and none of it replaces the expression.

``values_table`` points a column at a relation that already holds its
recorded values — three columns, ``fc_entity``, ``fc_t`` (the type of
``event_time``) and ``fc_value`` (the type of the expression), one row per
recorded value — so the full-history scan is not paid on every run. It
can be your own model or a small derived index; narrow it yourself, since
``fill_from`` applies only to live rows — an un-narrowed relation paired
with ``fill_from`` returns values ``fill_from`` would have excluded.
``values_watermark`` says the
relation is complete through that instant; rows after it are read live
from the table, so a relation that lags still yields exact results (leave
it unset to declare the relation complete). When ``event_time`` is an
expression, name the stored column in ``EventsSpec.event_time_column`` so
the live-tail bound compares the bare column and a partitioned warehouse
prunes it.

```python
from factcat import Breakdown

print(events_sql(EventsSpec(
    table="analytics.fct_events",
    entity="subscription_id",
    event_time="occurred_at",
    measure="uniques",
    where="event_name = 'paid'",
    breakdowns=(
        Breakdown(
            "subscription_tier",
            at="carried",
            fill_from="event_name = 'subscription_started'",
        ),
        "country",
    ),
    top_n=8,
), dialect="bigquery"))
```

Two honest costs. Without ``values_table`` the value scan is unbounded
history by design (a pre-window value must resolve): on BigQuery that is
the referenced columns' full history in bytes. ``fill_from`` prunes rows,
not bytes, on an unclustered table — but when the table clusters by the
event-name column (a common hub layout), an event-named ``fill_from``
prunes storage blocks too (observed ~20× on a month-partitioned hub with
event name as the leading cluster key). ``values_table`` is the remedy
for a column you break down every week: the relation costs the bytes of
the values actually recorded, and the live tail is a short
partition-pruned read. And under ``rows`` / ``carried``, Uniques slices
can sum to more than the unsplit line (an entity whose value changes
inside a bucket counts in both groups); the one-value-per-entity modes
partition entities instead.

Uniques, Distinct, and Median default to **approx** (`exact=False`).
**Break down by** picks the top-N series the same way (labels only; measures
on those series stay exact). The app **Exact** toggle (`exact=True`) turns
every sketch off.

| Job | BigQuery | Snowflake |
|---|---|---|
| Uniques / Distinct | `APPROX_COUNT_DISTINCT` | `APPROX_COUNT_DISTINCT` |
| Median | `APPROX_QUANTILES` | `APPROX_PERCENTILE` |
| Top-N labels (one breakdown, by count) | `APPROX_TOP_COUNT` | `APPROX_TOP_K` |
| Top-N labels (property Sum) | `APPROX_TOP_SUM` | exact `GROUP BY` `LIMIT` |
| Total / Sum / property Average / time axis | exact | exact |

Postgres and other generated dialects without a sketch keep `COUNT DISTINCT`
/ `GROUP BY` `LIMIT`. Exact is `COUNT DISTINCT` / `PERCENTILE_CONT` or
`MEDIAN` / `LIMIT`. Sketches are CPU and memory, not fewer bytes scanned.

The Events chart lists both families: Volume / Unique {entities} /
Average per {entity}, then Sum / Average / Median / Distinct of a
column (`of=`). Distinct is mean distinct values **per mapped entity**,
not a global `COUNT DISTINCT` of the column.

Day/week/month buttons in the app fill a `bucket` expression (reporting
timezone, then week start). There is no `period: day|week|month` field.

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
is no Factcat-hosted warehouse. You bring credentials to your own BigQuery or
Snowflake. If you need event collection, keep using whatever you use.

## Recommended warehouse shape

The library accepts **any relation**. A payments fact with `status` and `subscription_id`
is a valid source. You do not have to have an events table.

The Events app expects **one wide events table** today (typed columns; unused
values null). JSON property bags and one table per event type are not
supported yet. Setup shows the matching guide
([`setup-bigquery.md`](packages/engine/factcat_app/guides/setup-bigquery.md) or
[`setup-snowflake.md`](packages/engine/factcat_app/guides/setup-snowflake.md)).
Reporting timezone and whether the timestamp is a UTC instant or civil
DATETIME are set on Setup.

For clickstream-style product analytics, this shape works well:

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
client. Factcat has no warehouse of its own. BigQuery ships today; **Snowflake is
experimental** — its SQL is generated and compiled in CI against Snowflake's grammar,
but no live Snowflake account has ever executed it, so treat a first run as a test.
The
contract is `dialect` plus `run(sql)` — identity, auth, and cost knobs stay on the
concrete class so Snowflake does not inherit `project` / `location` /
`maximum_bytes_billed`. A later warehouse is a module and one line in the registry; see
the docstring on `factcat.warehouses`.

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

One project: [factcat](https://pypi.org/project/factcat/). Python 3.10+, and
that is the only requirement — it is an ordinary Python package, so install it
with whatever you already use.

```bash
pip install factcat              # SQL generation + the local chart
pip install factcat[bigquery]    # run queries in BigQuery
pip install factcat[snowflake]   # run queries in Snowflake (experimental)
pip install factcat[all]         # every execute adapter we ship
```

`uv`, `pipx`, Poetry, PDM, conda, a container image — all fine, same package
and the same extras:

```bash
uv pip install "factcat[bigquery]"      # into the environment you are in
uv tool install "factcat[bigquery]"     # isolated, puts `factcat` on PATH
pipx install "factcat[bigquery]"        # same idea
```

The two `tool` forms are worth knowing about if you are installing into a
project that pins its own dependencies — a dbt repo, say — because they keep
factcat's requirements out of it while still giving you the `factcat`
command. Nothing here needs a virtual environment of its own; how you isolate
Python is your call, and factcat has no opinion about it.

There is no npm package and no standalone binary. It is a Python library and
a Python-served local page, so a Node or Homebrew distribution would only be a
Python runtime in a costume.

Each extra is named after `connect(kind=)` and installs that warehouse's
official driver. The default has **no** warehouse SDK. Do not install a
second PyPI project per warehouse. Setup guides ship in the app
(`setup-bigquery.md`, `setup-snowflake.md`).

If Setup finds a warehouse extra missing it shows the command, and offers to
run it when pip or uv can reach the interpreter it is running in.

To hack on the library:

```bash
pip install -e "packages/engine[dev,all]"
```

## Run the app

The app is a local web page. It does not ingest your data. It generates SQL and runs it
in **your** warehouse (BigQuery; Snowflake is experimental). No Docker. Start it from **your warehouse
repo** (or any project directory); that is where `.factcat.json` is written.

**You need:** Python 3.10+. For BigQuery, the [Google Cloud SDK](https://cloud.google.com/sdk/docs/install),
a GCP project with the BigQuery API enabled, and a table you can query (BigQuery Job User
on the project, Data Viewer on the dataset). For Snowflake, an account, a user with a
key-pair, a compute warehouse, and a table you can query.

```bash
uv tool install "factcat[bigquery]"    # or: pipx install "factcat[bigquery]"

cd /path/to/your/warehouse             # mapping is saved here
factcat
```

Either of those puts `factcat` on your PATH, so there is nothing to activate
first. `pip install "factcat[bigquery]"` works too and is the right call when
you want it inside an environment you already manage — though a system Python
will refuse a bare `pip install` (PEP 668), which is what the two commands
above sidestep.

The working directory decides which `.factcat.json` is read and written, so
run it from the project the mapping belongs to.

Open http://127.0.0.1:8000. First run opens **Setup** (`/setup`): pick **BigQuery** or
**Snowflake** (experimental — see Execute adapters). If that warehouse extra is not installed, Setup shows the
command and **Install** (into this environment; it does not install on
its own). Then that warehouse's connection and catalog, then entity id and
timestamp. Fields persist as you pick them (no Save button). Event names
load on Events **Refresh list**. Optional: allow Factcat to create and
maintain tables in your warehouse for better performance (BigQuery:
project and dataset; Snowflake: database and schema). Setup is a separate
control at the bottom of the left rail, not an analysis. **Events** stays
reachable; until a mapping is ready it shows a prompt, Run is disabled,
and Setup has a marker. **Preferences** sits above Setup (wording,
thousand/decimal separators, weekday/month display, time of day). Those
follow the person in `~/.factcat/preferences.json`, not the project file.

**BigQuery.** After the extra is present: `gcloud auth application-default login`
and `gcloud config set project YOUR_GCP_PROJECT`. Billing project from ADC,
then `GOOGLE_CLOUD_PROJECT`, then `gcloud config get-value project`. Dataset →
table (lists; greyed until the previous step is set). Lists are cached in
the mapping file so returning to Setup does not re-query; each field has
**Refresh**. Location is
taken from the dataset (do not guess `US`). Advanced is only if you use a key
file instead of ADC.

Map the event-name column on **Setup** (STRING). Names are not fetched
while mapping. **Look back for event names** on Setup (90 days; 0 is all
time) is the window for the event picker; that job does not use the scan
cap. **Refresh event names** on Events re-runs that window. The chevron next to it looks
further back (6 months / 12 months / all time, only steps at least twice
the current lookback). The time filter isolates
the timestamp column so a date-partitioned table can prune.
Recommended **Factcat-managed tables**: allow Factcat to create and maintain
tables in your warehouse for better performance. BigQuery is project and
dataset; Snowflake is database and schema. Setup checks create rights on
that dest (no test object). First fetch creates
`fc_event_names` if missing; later Refresh reads it. The object carries
a fingerprint of the mapped table and event-name column (JSON comment
on the relation, plus `.factcat.json`) so a remapping rebuilds it. A table
fallback is a snapshot — Refresh rebuilds it. Lookback and the Refresh chevron are
hidden while that dest is set. Catalog jobs do not use the scan cap.
`fc_event_names` is a census (names, rows per month, first and last seen),
refreshed daily where the warehouse keeps it as a materialized view.

With a destination set, Factcat also keeps **`fc_column_index`**: for a
few breakdown columns, every row where that column had a value — entity,
instant, value, event name. The expensive Value-at modes (fill from
earlier values, range start / end, first / latest ever) read that small
table plus the live rows newer than its bookmark instead of the events
table's full history, so results are exact and later runs cost about
what a plain chart costs (measured on a month-partitioned hub: 10 GB →
0.26 GB for one carried breakdown). It fills itself: the first Run that
uses an expensive mode on a sparse column builds the index first, then
queries through it. That first run reads the column's whole history for
every event name, which can be **several times** what the chart alone
would have scanned (on one measured hub, 74 GB against 10 GB), and it pays
back after a handful of runs. While it runs the copy says "Indexing `x`…
then running", and afterwards one line under the chip says what later runs
cost. Once the index exists the chip prices the cheap query; where the warehouse
can price a job before running it **and** the column has already been
measured, the chip covers a pending build too. Dense columns (the value is on most rows)
are left alone; **Value at: each event** already reads them for free. Columns that are not text are left alone too (the
index stores text); write `CAST(x AS STRING)` as the breakdown expression to
index one.
Builds are automatic on every warehouse; **Mode** is the consent. A
column no chart has used for
**Drop unused after** (60 days) is dropped and rebuilt on next use — the
clean-up runs during a chart Run, at most once a day, never in the
background (Factcat schedules nothing); a column the chart in front of it
is asking for is never dropped, so returning to a chart does not cost a
rebuild; **Refresh when older than** (7 days) decides when newer rows
are folded in; **Late-arrival lookback** (3 days) is how late a row can
land after its event time. Setup's **Factcat-managed tables** section
lists every table with size and age; Drop is the only per-table action —
building, refreshing and rebuilding are Automatic mode's job. **Mode:
Off** stops Factcat using the indexes: charts read the full history and
scan more, which is what turning indexing off means. It also stops every
build, refresh, rebuild and drop, and it deletes nothing while it is off.
Time spent off still counts toward **Drop unused after**, so switching back
on resumes the ordinary clean-up: a column no chart has used for longer
than that is dropped on the next Run, the same as if indexing had never
been off. Charts you are actually running keep their indexes either way. If a build fails (rights, cap), the chart runs on
the full history and the run row says so. Every managed table is derived
and safe to drop; nothing lives only there.

`fc_column_index` holds entity ids and column values copied out of your
events table, so check the grants on your write destination before indexing
a column that carries personal data — it is a second home for it. Rows
deleted from the events table leave the index at the next refresh of that
column (**Refresh when older than**, 7 days by default), detected through
the event-name census. For a column no chart uses, they stay until the
clean-up drops it (**Drop unused after**, 60 days). With **Mode: Off**
there are no refreshes and no clean-up, so deleted rows stay in the index
until you Drop the column or the table yourself.
If an erasure has to be immediate, Drop the column on Setup or drop the
table. Two changes the index cannot see are a value rewritten in place with
no change in row count, and an identity remap; Drop is the remedy for both.
Bookkeeping is local to this machine (`.factcat.json`): if it is ever lost
while the table already holds real data, Factcat re-derives which columns
and how far back from the table's own rows, matching them by name to what
the current mapping wants — trusting that the entity, table and timestamp
mapping have not changed underneath it, the same trust an identity remap
already breaks. The table is charged as ordinary storage in your own
warehouse, and it is only cleaned up while someone is using Factcat.
On Events, each **event series** is a card: event name, **measure** (and Of
when the measure is a property), and filters. Filter operators follow the
column type (boolean, number, date, time, timestamp, string). String rows
can contain / start with / end with several patterns (each value a pill), with a case-sensitive
option. On a date or timestamp, a part dropdown is either **start of**
(hour / day / week / month / quarter — month and quarter pickers, not a day
calendar) or **extract** (hour of day, day of week, month of year, year as four
digits, …). The mapped timestamp may be filtered on a series (intersects the
chart date range). **Combine** nests another event
into that series (OR); **Split** undoes it. Ungrouped series overlay as
separate lines. The config column is two sections — **Event series** and
**Break down** — with the **Exact** toggle between them (it spans both:
approximate uniques and approximate top-N labels); **Refresh list** sits
in the Break down header because the columns list serves every slot.
**Break down by** is chart-wide unless **Break down each
series** is on, in which case each series has its own split. Each breakdown
carries a **Value at** control (each event · range start · range end ·
first ever · latest ever) and an **If missing** choice (leave `(null)`,
or fill from the entity's history — for each event that is the last known
earlier value; at range boundaries an entity with none takes its first
recorded value). **Fill from** narrows which events may supply the value —
`(any event)`, `(charted events)`, `(this series' events)` in per-series
mode, one event name, or a SQL predicate; the mode entries carry
parentheses so they never read as an event literally so named, and the
event names sit in their own labelled group. The anchors are the chart's date range;
the history search always reads the whole table, so a tier recorded only on
`subscription_started` still labels logins, and the meaning of a legend
label never changes per series. Watch the estimate when flipping these on
a large table: every option except the plain each-event one reads the
column's full history. **Refresh event names**
reloads names for the current lookback (or from `fc_event_names` if you
gave Factcat a write project and dataset). **Time grain** and **date range**
sit above the chart with **Run** (warehouse cost is explicit). Grain is
day / week / month / hour / day of week / hour of day; sugar fills
`EventsSpec.bucket`, not a period enum. Date range is then in that grain
(Last 30 days, Last 8 weeks, Last 6 months). Hour reuses the day list plus
Last 24 hours. Day of week and hour of day use the range as a calendar
filter — last N days, weeks, months, or quarters, not locked to the chart
grain (default last 8 weeks / last 14 days). Include the current period
follows the *window* (this month, this quarter, today) on those last-N
filters. Day grain also offers This week / Last week / This month as
*windows of days*. Week and month last-N default to complete periods so the
first bar is a full week or month. Include this week/month is opt-in and the
current bar is marked incomplete. Custom is specific dates (snapped to the
grain) or relative (from 12 to 3 weeks ago; 0 = this period). Sugar on
`event_time`, not a period enum. If a write destination is set, **Refresh event names**
reads `fc_event_names` (created on first miss as a materialized
view, or a table if the source cannot back a view). Lookback does not apply
while that cache is in use. When a run indexes a column for faster breakdowns, the running copy says
so ("Indexing `x`… then running") and one line under the chip afterwards
says what later runs cost. Week start and reporting
timezone are **Preferences**: they change SQL, but they belong to whoever builds
the report rather than to the project, so a colleague keeps their own. Thousand/decimal separators, wording
(business user / SQL analyst, with uppercase or lowercase SQL and `<>` or `!=`
for the analyst), weekday/month display, day-of-month pad, hour
style (12-hour or 24-hour first, then a short list of complete formats) are
**Preferences**; number filters
use those separators, and the SQL pane stays warehouse SQL (period decimal, no
grouping). Catalog dropdowns are alphabetical.
Entity lists string and integer columns; timestamp lists TIMESTAMP /
DATETIME.

If the BigQuery table lives in another GCP project (billing in `dev`, data in `prod`), set
**Project that holds the table** before loading datasets.

**Snowflake.** Account identifier, user, and sign-in (key-pair path or browser
SSO). Then Role (optional) and compute warehouse from lists, then
database → schema → table. Catalog fields stay visible and greyed until the
previous step is set, then loaded so the first click has options. An encrypted
key's passphrase is
`SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` in the environment, not in `.factcat.json`.

There is no `user_id` default. **Entity name** on Setup is a display label
(default User; Other is free text). It does not pick the id column.
**Volume** is row count, **Unique Users** (or Unique Customers, …) is distinct
of the mapped id, **Average per User** is Volume / that unique count. Entity
singular and plural are set on Setup. Day/week/month buckets are dates, not
timestamps. Chart type is Auto (bar when there are one or two points, else
line), Line, Area, or Bar. **Format** on the chart sets value format, data
labels, axis labels, and grid (major / major+minor). Copy and PNG sit
beside it. The title updates only after a successful Run (unless you have
edited it). Next to **Run**, a dry-run **bytes scanned** estimate (free; not billed)
updates when the scan would change. The 10 GB cap is Factcat's
`maximum_bytes_billed` on the job, not a GCP project default. Exact unique
counts do not change bytes scanned (same columns), so they do not
re-estimate. **Result row limit** is a Setup crash fuse (default 1,000,000) in
SQL: most recent aggregated rows, `ORDER BY bucket DESC LIMIT n`. A time
series never hits it; a slice by a high-cardinality property might. If it
does, a warning offers **Load more**, which doubles the cap for that run
rather than removing LIMIT. There is no hard max. Sort is among loaded
rows and does not re-query. **Job scan
cap** is set in Setup for BigQuery (default 10 GB on the job). Snowflake has no
byte estimate; that chrome is hidden. When a BigQuery estimate exceeds the cap,
**Run** is disabled and an **Override cap** tickbox appears beside it; the
estimate reads `~24 GB / 10 GB cap`. Override takes no number — it removes the
cap rather than raising it to one. It is never written to `.factcat.json`, so it
is gone next start; while it stays ticked it keeps applying, and the tickbox
stays visible so it can be cleared. A query that outran its estimate and was
rejected by BigQuery offers the same tickbox, using the figures BigQuery
reported. The filter pane sits beside Chart, Table, and SQL result panes.

Click **Run**. The mapping is written to `.factcat.json` in the directory where you started
`factcat`, so the next start is already filled in. Add `.factcat.json` to that repo’s
`.gitignore`. ADC lives in your user profile; you do not log in to Google every time you
start the app.

Stop the server with Ctrl+C. To use a key file instead of ADC, paste the JSON path in the
form. The app never copies your events out of your warehouse.

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
