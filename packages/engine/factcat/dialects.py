"""Per-dialect SQL that cannot be transpiled.

sqlglot handles almost everything: date arithmetic, casting, string functions,
window functions. Two constructs do not survive cleanly:

1. **Generating a series of integers** (the retention period grid).
   DuckDB ``UNNEST(GENERATE_SERIES)``; Redshift has no reachable generator.
2. **Median on BigQuery.** sqlglot emits aggregate ``MEDIAN(x)``, which
   BigQuery does not have. The exact form is the window function
   ``PERCENTILE_CONT(x, 0.5) OVER (PARTITION BY ...)``. Runners execute
   SQL; they do not translate it. This helper is generation.

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


def median_select_from_base(dialect: str) -> str:
    """``SELECT bucket, value`` from ``base`` (columns ``fc_bucket``, ``fc_of``).

    DuckDB-shaped ``median(fc_of)`` everywhere except BigQuery, which has no
    aggregate median. BigQuery is ``PERCENTILE_CONT(fc_of, 0.5)`` as a window.
    """
    if dialect == "bigquery":
        return """SELECT
        fc_bucket AS bucket,
        MIN(fc_p) AS value
    FROM (
        SELECT
            fc_bucket,
            PERCENTILE_CONT(fc_of, 0.5 IGNORE NULLS) OVER (
                PARTITION BY fc_bucket
            ) AS fc_p
        FROM base
    )
    GROUP BY 1
    ORDER BY 1"""
    return """SELECT
        fc_bucket AS bucket,
        median(fc_of) AS value
    FROM base
    GROUP BY 1
    ORDER BY 1"""


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
