"""Per-dialect SQL that cannot be transpiled.

sqlglot handles almost everything: date arithmetic, casting, string functions,
window functions. Two constructs do not survive cleanly:

1. **Generating a series of integers** (the retention period grid).
   DuckDB ``UNNEST(GENERATE_SERIES)``; Redshift has no reachable generator.
2. **Median and approx NDV.** sqlglot emits aggregate ``MEDIAN(x)`` (invalid
   on BigQuery) and will not turn ``COUNT DISTINCT`` into
   ``APPROX_COUNT_DISTINCT``. Exact BigQuery median is
   ``PERCENTILE_CONT(x, 0.5) OVER (PARTITION BY ...)``. Approx is
   ``APPROX_QUANTILES``. Runners execute SQL; they do not translate it.

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


def count_distinct(expr: str, dialect: str, *, exact: bool) -> str:
    """NDV of ``expr``. Approx is HyperLogLog-style; Postgres has none and
    falls back to ``COUNT DISTINCT``.
    """
    if exact:
        return f"COUNT(DISTINCT {expr})"
    if dialect == "bigquery":
        return f"APPROX_COUNT_DISTINCT({expr})"
    if dialect == "snowflake":
        return f"APPROX_COUNT_DISTINCT({expr})"
    if dialect == "duckdb":
        return f"approx_count_distinct({expr})"
    if dialect in ("databricks", "spark"):
        return f"approx_count_distinct({expr})"
    if dialect in ("trino", "presto"):
        return f"approx_distinct({expr})"
    if dialect == "clickhouse":
        return f"uniq({expr})"
    if dialect == "redshift":
        return f"APPROXIMATE COUNT(DISTINCT {expr})"
    return f"COUNT(DISTINCT {expr})"


def median_select_from_base(dialect: str, *, exact: bool) -> str:
    """``SELECT bucket, value`` from ``base`` (columns ``fc_bucket``, ``fc_of``).

    Exact BigQuery: ``PERCENTILE_CONT(fc_of, 0.5)`` window. Approx BigQuery:
    ``APPROX_QUANTILES``. Elsewhere exact is ``median``; approx is
    ``approx_quantile`` where it exists, else exact.
    """
    if dialect == "bigquery":
        if exact:
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
        APPROX_QUANTILES(fc_of, 100)[OFFSET(50)] AS value
    FROM base
    GROUP BY 1
    ORDER BY 1"""
    if exact or dialect == "postgres":
        agg = "median(fc_of)"
    elif dialect == "duckdb":
        agg = "approx_quantile(fc_of, 0.5)"
    elif dialect == "snowflake":
        agg = "approx_percentile(fc_of, 0.5)"
    elif dialect in ("databricks", "spark"):
        agg = "approx_percentile(fc_of, 0.5)"
    elif dialect in ("trino", "presto"):
        agg = "approx_percentile(fc_of, 0.5)"
    else:
        agg = "median(fc_of)"
    return f"""SELECT
        fc_bucket AS bucket,
        {agg} AS value
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
