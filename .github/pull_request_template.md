The maintainer reviews weekly and merges. Small, focused PRs land fastest.

## What

The diff, in words.

## Why

## Tests

The suite tail. A new guard is shown to fail: break it, see red, restore, and say so here.

## Adapters walk

For a change that generates SQL, runs a warehouse job, or shows warehouse chrome: for each
shipped adapter (BigQuery, Snowflake), say which applies. It behaves the same; it is gated by
that adapter's declared capabilities; or it has a named branch with a test. Otherwise write
"not applicable".

## Known limits
