"""Per-dialect SQL that cannot be transpiled.

sqlglot handles almost everything: date arithmetic, casting, string functions,
window functions. Two constructs do not survive cleanly:

1. **Generating a series of integers** (the retention period grid).
   DuckDB ``UNNEST(GENERATE_SERIES)``; Redshift has no reachable generator.
2. **Median as an aggregate.** sqlglot emits ``MEDIAN`` / ``PERCENTILE_CONT
   WITHIN GROUP`` which is not valid BigQuery (PERCENTILE_CONT is a window
   function there). Proved before this helper was added.

Everything else is one emitter.
"""

from __future__ import annotations

SUPPORTED: tuple[str, ...] = (
    "duckdb",
    "postgres",
    "bigquery",
    "snowflake",
    "databricks",
    "spark",
    "trino",
    "presto",
    "clickhouse",
    "redshift",
)


def _union_all_grid(n: int) -> str:
    """The universal fallback: an explicit union of integers.

    Verbose, but valid in every SQL dialect ever written. Used for Redshift,
    which has no row generator reachable without a system table.
    """
    return " UNION ALL ".join(f"SELECT {i} AS period_index" for i in range(n + 1))


def median_agg(expr: str, dialect: str) -> str:
    """Aggregate median of ``expr`` in ``dialect``.

    BigQuery has no aggregate ``MEDIAN`` / ``PERCENTILE_CONT``; it gets
    ``APPROX_QUANTILES``. Exact medians elsewhere use ``percentile_cont`` or
    native ``median``.
    """
    if dialect == "duckdb":
        return f"median({expr})"
    if dialect in ("postgres", "redshift"):
        return f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {expr})"
    if dialect == "bigquery":
        return f"APPROX_QUANTILES({expr}, 100)[OFFSET(50)]"
    if dialect == "snowflake":
        return f"median({expr})"
    if dialect in ("databricks", "spark"):
        return f"percentile_approx({expr}, 0.5)"
    if dialect in ("trino", "presto"):
        return f"approx_percentile({expr}, 0.5)"
    if dialect == "clickhouse":
        return f"median({expr})"
    return f"median({expr})"


def period_grid(n_periods: int, dialect: str) -> str:
    """SQL returning one row per period index, 0..n_periods inclusive.

    The result must expose a single column named ``period_index``.
    """
    if n_periods < 0:
        raise ValueError("n_periods must be >= 0")
    n = n_periods

    if dialect == "duckdb":
        return f"SELECT UNNEST(GENERATE_SERIES(0, {n})) AS period_index"
    if dialect == "postgres":
        return f"SELECT GENERATE_SERIES(0, {n}) AS period_index"
    if dialect == "bigquery":
        return f"SELECT period_index FROM UNNEST(GENERATE_ARRAY(0, {n})) AS period_index"
    if dialect == "snowflake":
        return (
            "SELECT SEQ4() AS period_index "
            f"FROM TABLE(GENERATOR(ROWCOUNT => {n + 1}))"
        )
    if dialect in ("databricks", "spark"):
        return f"SELECT EXPLODE(SEQUENCE(0, {n})) AS period_index"
    if dialect in ("trino", "presto"):
        return f"SELECT period_index FROM UNNEST(SEQUENCE(0, {n})) AS t(period_index)"
    if dialect == "clickhouse":
        return f"SELECT arrayJoin(range(0, {n + 1})) AS period_index"

    # Redshift and anything unrecognised. Correct everywhere, ugly above ~200.
    return _union_all_grid(n)
