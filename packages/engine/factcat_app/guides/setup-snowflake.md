# Snowflake

Map **one wide events table** in your account. Factcat generates SQL and
runs it there. It does not ingest rows.

## Credentials

Two sign-in methods. Neither stores a password in `.factcat.json`.

**Key-pair (JWT).** Create an RSA key, assign the public key to the
Snowflake user, and put the **path** to the private key (`rsa_key.p8`)
on this page. Encrypted keys use the environment variable
`SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`.

**Browser SSO.** Uses the connector's `externalbrowser` authenticator.
The first query or catalog load opens a browser. After that, the
connector caches the SSO token in the OS keyring
(`snowflake-connector-python[secure-local-storage]`) until it expires
(typically about four hours), **if** the account allows it:

```sql
ALTER ACCOUNT SET ALLOW_ID_TOKEN = TRUE;
```

That is an account-admin setting, not something Factcat can turn on.
If caching is off, every new connection opens a browser again.

You also need an **account identifier** and **user**. After sign-in,
**Role** (optional; blank is the user's default) and **Compute warehouse**
are lists, then **database → schema → table**. Catalog fields stay visible
and greyed until the previous step is set (same as BigQuery dataset → table).
Each list loads as soon as the previous field is set the first time
(database → schema → table → columns), then is cached. Returning to Setup
does not re-query. **Refresh** on a field reloads that list.
Do not type those names. Sign-in does not fill database, table, or grain;
mapping persists as you pick fields. Events prompts until the mapping is ready.

## Table shape

One table. One **row per event**. Properties are **real columns**. Event
types that do not use a column leave it **null** (sparse). Not a VARIANT
properties bag, and not one table per event type.

```text
event_name     VARCHAR        -- 'purchase', 'page_view', …
occurred_at    TIMESTAMP_TZ   -- instant (offset stored)
-- or TIMESTAMP_LTZ           -- instant (UTC storage; session TZ is display)
-- or TIMESTAMP_NTZ           -- wall-clock, no zone (see Time)
account_id     NUMBER         -- null when the event has no account
country        VARCHAR
revenue        NUMBER
```

Flatten VARIANT in dbt before mapping.

## Entity id

Whichever grain this report is about — not hard-coded to `user_id`.
NUMBER preferred; VARCHAR is fine. Null means “not this grain”.

## Timestamp

Must be an **event time**, not a calendar DATE and not a TIME-of-day.

| Type | What Snowflake stores | Factcat |
|---|---|---|
| `TIMESTAMP_TZ` | UTC + the **offset** from the value (not the IANA name) | Instant. Reporting timezone is whose midnight is a day. |
| `TIMESTAMP_LTZ` | UTC. Session `TIMEZONE` is display only | Instant. Same. |
| `TIMESTAMP_NTZ` / `DATETIME` | Wall-clock numbers, **no zone** | Pick the zone those numbers are in (shown under Timestamp). |
| Bare `TIMESTAMP` | Alias; default mapping is NTZ | Treat as NTZ. |
| `NUMBER` / `INT` / `BIGINT` | Unix epoch | Instant. Unit (seconds / ms / µs) is inferred from a sample of values. |

`CONVERT_TIMEZONE` with two arguments is for instants (TZ/LTZ). NTZ
needs three arguments (source zone, target zone, value). We do not rely
on the session `TIMEZONE` for NTZ. TIMESTAMP_TZ, TIMESTAMP_LTZ, and Unix
epochs do not show a timezone picker.

**Reporting timezone** is whose midnight is a “day”, and whose Monday is
a “week”. It and **Week starts on** live on **Preferences**, not here: they
change the query, but they belong to whoever builds the report, so a
colleague keeps their own.

## Clustering

Snowflake has no partition column. Micro-partitions always exist; date
filters often prune if rows are loaded in time order, with no `CLUSTER
BY`. That is not Automatic Clustering — Automatic Clustering only
maintains a key you already set.

When you do cluster a large events table, order keys from lowest
cardinality to highest. Do not cluster on a raw nanosecond timestamp
(too many values; use `TO_DATE(event_time)`). Do not cluster on month
(too few values). A typical Factcat layout:

```sql
CLUSTER BY (event_name, TO_DATE(event_time))
```

A unique entity id as the leading key is usually too expensive to
maintain. Setup lists views as well as tables.

Recommended: allow Factcat to create and maintain tables in a database
and schema for better performance. Setup checks create rights on that
schema (no test object).

## Event name

Optional string column. Mapping persists as you pick fields; it does not query.
**Look back for event names** on this page (90 days by default; 0 is all
time) is the window for the Events picker. That job does not use the scan
cap. Events **Refresh event names** re-runs it; the chevron next to it raises the window. The filter isolates the timestamp column so Snowflake
can prune micro-partitions. First fetch creates
`fc_event_names` if it is missing (materialized view, or a table if the
source cannot back a view). Later Refresh reads the view, or rebuilds the
table snapshot. The object stores a fingerprint of the mapped table and
event-name column. Lookback does not apply (and is hidden here) while that
dest is set. Catalog jobs do not use the scan cap. Events then filters
`event_name = '…'`. The object is a census — names, rows per month, first
and last seen. Snowflake materialized views refresh on their own schedule
and need Enterprise edition; otherwise it is a table snapshot, and every
Refresh of that table is a full recompute of the event-name and timestamp
columns' history.

## Factcat-managed tables

With a write database and schema set, Factcat keeps `fc_column_index`
there: for a few breakdown columns, every row where the column had a
value (entity, instant, value, event name). No clustering key is set: one
would switch on Automatic Clustering, a standing serverless credit charge
you did not ask for; micro-partitions prune on the time bounds as they do
for your events table, and you can add a key yourself if the table grows
large. The expensive Value-at modes read it plus the live rows after its
bookmark, bounded on the bare timestamp column; results are exact
regardless of how stale the index is.

**Mode** Automatic indexes a sparse column the first time an expensive
mode uses it, on Snowflake as on BigQuery. Be deliberate about it here:
Snowflake has no cost preview and no byte ceiling, so unlike BigQuery
there is no scan cap standing behind the build, and that first run reads
the column's whole history for every event name — several times what the
chart alone would read. Snowflake also bills warehouse time rather than
bytes, so the saving shows up as a shorter query, not a smaller bill, and a
warm warehouse may show little. Set Mode to Off if that is not a trade you
want — it stops the builds and the reads together, so charts go back to
scanning the full history. Because there is no cost preview there is no estimate chip, and so no
"Indexing…" copy while it runs; one line under the Run button afterwards
says the index is in place. **Refresh when older than**
decides when newer rows are folded in, **Drop unused after** drops columns
no chart has used — checked during a chart Run, at most once a day, never
in the background, and never for a column the current chart asks for.
**Late-arrival lookback** is how late a
row can land after its event time. Off stops Factcat using the indexes:
charts read the full history and scan more. It also stops every build,
refresh, rebuild and drop, and deletes nothing while it is off. Time spent
off still counts toward **Drop unused after**, so switching back on
resumes the ordinary clean-up — a column no chart has used for longer than
that is dropped on the next Run. The chart you are running keeps its index
either way.

Columns that are not text are left alone (the index stores text); write
`CAST(x AS STRING)` as the breakdown expression to index one.

The table holds entity ids and column values copied from your events table,
so check who can read the write schema before indexing a column that
carries personal data. Rows deleted from the events table leave the index
at the next refresh of that column, which depends on the event-name census
noticing the row count fall — and without Enterprise materialized views
that census is a table snapshot that only refreshes when someone clicks
**Refresh event names**, so on standard edition the check is as fresh as
your last refresh. With **Mode: Off** there are no refreshes and no
clean-up at all, so deleted rows stay in the index until you Drop the
column or the table yourself. Drop the column for an immediate removal. One gap to know about: if an event name
that already existed starts carrying a column it never carried before, the
index does not pick that up on its own — Drop the column and let the next
chart rebuild it. The build
statements for Snowflake are verified by compiling them against Snowflake's
grammar in the test suite; no live Snowflake account ran them for this
release, which is why Snowflake is marked experimental. Result columns are
lower-cased on the way back, because Snowflake reports unquoted identifiers
upper-cased and every identifier Factcat generates is unquoted; if a chart
comes back empty rather than wrong, that is the first thing to check. If a build fails, the chart reads the full history and the Setup
list says why.

The list shows size and age per table (from `SHOW TABLES`); Drop is the
only per-table action (building, refreshing and rebuilding are Automatic
mode's job). Its bookkeeping
lives in `.factcat.json` on this machine, written the moment each column's
build finishes; a mapping change rebuilds, a new event name with
backfilled history is back-filled on the next refresh, a name whose row
count shrank is rebuilt. If that file is ever lost while the table already
holds real data, Factcat re-derives which columns and how far back from
the table's own rows, matched by name — trusting the entity, table and
timestamp mapping have not changed underneath it, single-install only.
Needs CREATE TABLE on the schema (the rights check above). A failed build never blocks a chart: it runs on the full
history and the run row says why.
