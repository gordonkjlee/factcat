# Live dry-run tier

The hermetic suite proves generated SQL compiles through sqlglot and runs on DuckDB. It
cannot prove a warehouse accepts it: sqlglot does not type-check, and DuckDB coerces where
BigQuery refuses. This tier hands every statement shape the product emits to a real
BigQuery project as a dry run, so the warehouse's own compiler is the judge.

It needs a mapping file in the shape the app writes, pointed at a sandbox table, with a
write destination of its own. The file must not be `.factcat.json` (the production mapping
is refused); `.factcat.dev.json` is ignored by git.

```bash
FACTCAT_LIVE_CONFIG=/path/to/.factcat.dev.json python -m pytest tests/live -m live
```

Dry runs bill zero bytes. One bootstrap creates the managed relations the later statements
reference, using the product's own DDL, under a small scan cap. Without `FACTCAT_LIVE_CONFIG`
every test skips, which is what CI and the default `python -m pytest` see. Run it by hand
before a release PR merges.
