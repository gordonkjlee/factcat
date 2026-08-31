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
chain.

## Table shape

One table. One **row per event**. Properties are **real columns**. Event
types that do not use a column leave it **null** (sparse). Not a JSON
properties bag, and not one table per event type.

```text
event_name     STRING      -- 'purchase', 'page_view', …
occurred_at    TIMESTAMP   -- UTC instant (see Time below)
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
| `DATETIME` | Civil date-time, no zone | Reporting timezone |
| `INT64` / `INTEGER` | Unix epoch | Seconds, milliseconds, or microseconds since 1970-01-01 UTC |

STRING timestamps are not accepted. DATE is a day, not an event time. FLOAT
epochs are not accepted.

**Reporting timezone** is whose midnight is a “day”, and whose Monday is
a “week”. `CURRENT_DATE` and day buckets follow that zone. Week start
(Monday/Sunday) is applied **after** the instant is converted to that
calendar.

If the column is DATETIME, it is already civil time in the reporting
zone. Do not store local time in a TIMESTAMP and label it UTC.

## Event name

Optional STRING column. Mapping persists as you pick fields; it does not query.
Events **Refresh list** loads names for the event-name lookback on Setup
(90 days by default; 0 is all time). That job does not use the scan cap.
The filter isolates the timestamp column so a table partitioned on it can
prune. Optional: allow Factcat to create and maintain tables in a
project and dataset for better performance. First Refresh creates
`fc_event_names` if it is missing; later Refresh reads that cache (lookback
does not apply). Events then filters `event_name = '…'`.
