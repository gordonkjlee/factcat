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
`event_name = '…'`.
