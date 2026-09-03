# BigQuery

Map **one wide events table** in your project. Factcat generates SQL and
runs it there. It does not ingest rows.

## Credentials

`gcloud auth application-default login` unless you set a key file under
Advanced. Billing project runs the job; the field is pre-filled from ADC,
then `GOOGLE_CLOUD_PROJECT`, then `gcloud config get-value project`.
**Project that holds the table** is only if the dataset is in another GCP
project. Dataset, table, entity id, and timestamp are not in that cache —
pick them here. Location is taken from the dataset.

Need BigQuery Job User on the billing project and Data Viewer on the
dataset.

**Dataset** and **table** are lists after the billing project is set.
They stay visible and greyed until then, same as Snowflake's catalog
chain. Each list loads as soon as the previous field is set the first time
(tables when the dataset is set, columns when the table is set), then
is cached. Returning to Setup does not re-query. **Refresh** on a field
reloads that list.

## Table shape

One table. One **row per event**. Properties are **real columns**. Event
types that do not use a column leave it **null** (sparse). Not a JSON
properties bag, and not one table per event type.

```text
event_name     STRING      -- 'purchase', 'page_view', …
occurred_at    TIMESTAMP   -- UTC instant
account_id     INT64       -- null when the event has no account
country        STRING      -- null when unknown
revenue        NUMERIC     -- null on non-purchase events
```

`purchase` fills `revenue`; `page_view` leaves it null. Extra grains
(account, subscription, booking) are extra **id columns**, null when that
event is not about that grain.

Flatten JSON / STRUCT in dbt before mapping. Other layouts (JSON bags,
one table per event) are a later **closed** list, not “any schema”.

## Entity id

Whichever grain this report is about — not hard-coded to `user_id`.

- **Stable.** An id, not a display name or an email that can change.
- **Null means “not this grain”.** Uniques skip nulls. Total still
  counts the row. Do not invent a sentinel id.
- **Not unique on the events table.** Many events share one id.
- **INT64 / NUMERIC preferred** (cheaper DISTINCT). **STRING is fine.**
  Not FLOAT64, BOOL, or a timestamp.

The User / Customer label is display only. It does not pick the column.

## Timestamp

Must be an **instant**, not a calendar DATE and not a wall-clock TIME.

| Type | Meaning | Set “Timestamp stored as” |
|---|---|---|
| `TIMESTAMP` | UTC instant (BigQuery) | UTC instant |
| `DATETIME` | Civil date-time, no zone | Pick the zone those numbers are in (shown under Timestamp) |
| `INT64` / `INTEGER` | Unix epoch | Inferred (seconds / ms / µs) from a sample of values |

STRING timestamps are not accepted. DATE is a day, not an event time. FLOAT
epochs are not accepted.

**Reporting timezone** is whose midnight is a “day”, and whose Monday is
a “week”. `CURRENT_DATE` and day buckets follow that zone. Week start
(Monday/Sunday) is applied **after** the instant is converted to that
calendar. Both live on **Preferences**, not here: they change the query,
but they belong to whoever builds the report, so a colleague keeps their
own.

DATETIME has no zone: a timezone picker appears under Timestamp. TIMESTAMP
and Unix epochs are instants and do not need it. Reporting timezone is
still whose midnight is a day. Do not store local time in a TIMESTAMP and
label it UTC.

## Partitioning and clustering

BigQuery has **one** partition column. Partition the events table on the
mapped timestamp so date filters prune. Average partition size should be
about 10 GB: many tiny day partitions inflate metadata and slow listing
them. Month on a large table is often the right size.

Clustering is a second sort **inside** each partition (up to four
columns). Order is a leftmost prefix: a filter on the first key skips
blocks; a filter on only the second does not. Typical layout: partition
by event time, cluster `event_name`, then the entity id. Cluster pruning
does not show in a dry-run estimate.

A view does not advertise the hub's partition column. Date filters still
prune if the view pushes them down. Setup checks that with a dry-run
when metadata is silent. A view over many per-event tables may prune
event-name filters by selecting a spoke; that is not a missing
`event_name` cluster.

Recommended: allow Factcat to create and maintain tables in a project
and dataset for better performance. Setup checks create rights on that
dataset (no test object).

## Event name

Optional STRING column. Mapping persists as you pick fields; it does not query.
**Look back for event names** on this page (90 days by default; 0 is all
time) is the window for the Events picker. That job does not use the scan
cap. Events **Refresh event names** re-runs it; the chevron next to it raises the window. The filter isolates the timestamp column so a table
partitioned on it can prune. First fetch creates
`fc_event_names` if it is missing (materialized view, or a table if the
source cannot back a view). Later Refresh reads the view, or rebuilds the
table snapshot. The object stores a fingerprint of the mapped table and
event-name column. Lookback does not apply (and is hidden here) while that
dest is set. Catalog jobs do not use the scan cap. Events then filters
`event_name = '…'`. The view is a census — names, rows per month, first
and last seen — refreshed daily (`refresh_interval_minutes = 1440`), never
BigQuery's 30-minute default: each refresh over a rebuilt source table is a
full recompute of two columns' history.

## Factcat-managed tables

With a write project and dataset set, Factcat keeps `fc_column_index`
there: for a few breakdown columns, every row where the column had a
value (entity, instant, value, event name), day-partitioned on the
instant and clustered by column and entity. The expensive Value-at modes
read it plus the live rows after its bookmark, bounded on the bare
timestamp column so partitions prune; results are exact regardless of how
stale the index is.

**Mode** Automatic indexes a sparse column the first time an expensive
mode uses it. That run reads the column's whole history for every event
name, which can be several times what the chart alone would scan (74 GB
against 10 GB on one measured hub) and pays back after a handful of runs;
where the probe is already cached the estimate chip includes it. The
running copy says "Indexing `x`… then running", and afterwards one line
under the chip says what later runs cost. Once the index exists the chip
prices the cheap query. The build runs under the same scan cap as a chart;
over the cap it fails cleanly and the chart reads the full history. Automatic refreshes
it when older than **Refresh when older than**, and drops columns no
chart has used for **Drop unused after** on a daily sweep. **Late-arrival
lookback** is how late a row can land after its event time; a refresh
re-reads that much before each event name's bookmark. Dense columns (the
value on more than about a quarter of recent rows) are not indexed —
**Value at: each event** already has them. Columns that are not text are left alone too (the
index stores text); write `CAST(x AS STRING)` as the breakdown expression to
index one. Off changes nothing: no build, refresh, rebuild or drop; an
existing index is still read.

The list shows size and age per table; Drop is the only per-table action
(building, refreshing and rebuilding are Automatic mode's job). Its
bookkeeping (fingerprints,
last use, census snapshot) lives in the table's own description; a
mapping change rebuilds, a new event name with backfilled history is
back-filled on the next refresh, a name whose row count shrank is rebuilt.
Needs `bigquery.tables.create` on the dataset (the rights check above).
A failed build never blocks a chart: it runs on the full history and the
run row says why.

The table holds entity ids and column values copied from your events table,
so check who can read the write dataset before indexing a column that
carries personal data. Rows deleted from the events table leave the index
at the next refresh of that column (the census notices the row count fall);
with Mode: Off, or for a column no chart uses, they stay until the sweep
drops it. Drop the column for an immediate removal. One gap to know about: if an event name
that already existed starts carrying a column it never carried before, the
index does not pick that up on its own — Drop the column and let the next
chart rebuild it. The index is
day-partitioned, and BigQuery refuses a single statement that touches more
than 4,000 partitions, so a column with more than about eleven years of
history cannot be built in one pass — the build fails cleanly and the chart
reads the full history.
