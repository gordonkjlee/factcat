"""Per-dialect SQL that cannot be transpiled.

sqlglot handles almost everything: date arithmetic, casting, string functions,
window functions. These constructs do not survive cleanly:

1. **Generating a series of integers** (the retention period grid).
   DuckDB ``UNNEST(GENERATE_SERIES)``; Redshift has no reachable generator.
2. **Median and approx NDV.** sqlglot emits aggregate ``MEDIAN(x)`` (invalid
   on BigQuery) and will not turn ``COUNT DISTINCT`` into
   ``APPROX_COUNT_DISTINCT``. Exact BigQuery median is
   ``PERCENTILE_CONT(x, 0.5) OVER (PARTITION BY ...)``. Approx is
   ``APPROX_QUANTILES``. Runners execute SQL; they do not translate it.
3. **Week-start DATE_TRUNC.** DuckDB ``date_trunc('week', x)`` is Monday;
   sqlglot emits BigQuery ``TIMESTAMP_TRUNC(x, WEEK)``, which is Sunday.
   ``WEEK(MONDAY)`` / ``WEEK(SUNDAY)`` must be spliced.
4. **Top-N category labels.** ``APPROX_TOP_COUNT`` / ``APPROX_TOP_K`` /
   ``approx_top_k`` do not transpile from DuckDB ``GROUP BY … LIMIT``. Exact
   pick is ordinary SQL.
5. **Reporting-timezone calendar.** BigQuery ``DATE(ts, tz)`` and
   ``CURRENT_DATE(tz)`` have no DuckDB equivalent sqlglot will emit.
   Snowflake is ``CONVERT_TIMEZONE`` plus an explicit week start (not
   session ``WEEK_START``). Week start is applied after that conversion.
6. **UTC instant from TIMESTAMP or DATETIME.** sqlglot rewrites
   ``CAST(col AS TIMESTAMP) >= TIMESTAMP(...)`` to ``CAST AS DATETIME``,
   which BigQuery then rejects. ``factcat_as_instant`` is spliced after
   transpile for SELECT ``fc_event_ts``. Window filters isolate the
   column and convert the bound (DATETIME / TIMESTAMP_NTZ for wall-clock
   UTC) so partition pruning can fire.
7. **Catalog cache DDL.** ``CREATE MATERIALIZED VIEW`` / ``CREATE TABLE``
   as a wrapper around a GROUP BY select. Not transpile.
8. **Hour trunc / hour-of-day / weekday index in a reporting timezone.**
   sqlglot will not emit BigQuery ``DATETIME(ts, tz)`` plus
   ``DATETIME_TRUNC(..., HOUR)``, nor Monday=0
   ``MOD(EXTRACT(DAYOFWEEK) + 5, 7)``. Same splice as calendar dates.

Everything else is one emitter.
"""

from __future__ import annotations

import re
from collections.abc import Callable

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


def median_select_from_base(
    dialect: str,
    *,
    exact: bool,
    extra_group: tuple[str, ...] = (),
    relation: str = "base",
) -> str:
    """``SELECT bucket, [extra…,] value`` from ``relation`` (``fc_bucket``, ``fc_of``).

    Exact BigQuery: ``PERCENTILE_CONT(fc_of, 0.5)`` window. Approx BigQuery:
    ``APPROX_QUANTILES``. Elsewhere exact is ``median``; approx is
    ``approx_quantile`` where it exists, else exact.
    """
    extras = ", ".join(extra_group)
    extra_select = f", {extras}" if extras else ""
    extra_part = f", {extras}" if extras else ""
    n_group = 1 + len(extra_group)
    group_by = ", ".join(str(i) for i in range(1, n_group + 1))
    if dialect == "bigquery":
        inner_keys = "fc_bucket" + extra_select
        if exact:
            return f"""SELECT
        {bucket_out()}{extra_select},
        MIN(fc_p) AS value
    FROM (
        SELECT
            {inner_keys},
            PERCENTILE_CONT(fc_of, 0.5 IGNORE NULLS) OVER (
                PARTITION BY fc_bucket{extra_part}
            ) AS fc_p
        FROM {relation}
    )
    GROUP BY {group_by}
    ORDER BY {group_by}"""
        return f"""SELECT
        {bucket_out()}{extra_select},
        APPROX_QUANTILES(fc_of, 100)[OFFSET(50)] AS value
    FROM {relation}
    GROUP BY {group_by}
    ORDER BY {group_by}"""
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
        {bucket_out()}{extra_select},
        {agg} AS value
    FROM {relation}
    GROUP BY {group_by}
    ORDER BY {group_by}"""


def top_labels_select(
    source: str,
    cols: tuple[str, ...],
    n: int,
    *,
    dialect: str,
    exact: bool,
    rank_sql: str,
) -> str:
    """SQL that returns the top ``n`` category keys from ``source``.

    Exact pick is ``GROUP BY cols ORDER BY rank LIMIT n``. ``exact=False`` and a
    single column use the dialect's frequent-item sketch when it has one
    (count rank; BigQuery also ``APPROX_TOP_SUM`` when ranking by ``SUM(fc_of)``).
    NULL is excluded from the sketch so it does not consume a top-N slot; the
    fold still keeps SQL NULL as its own series and never maps it to ``(other)``.
    Multi-column keys stay on the exact pick.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    col_csv = ", ".join(cols)
    n_cols = len(cols)
    group_by = ", ".join(str(i) for i in range(1, n_cols + 1))
    exact_sql = (
        f"SELECT {col_csv} FROM {source} "
        f"GROUP BY {group_by} "
        f"ORDER BY {rank_sql} DESC, {col_csv} "
        f"LIMIT {n}"
    )
    if exact or len(cols) != 1:
        return exact_sql
    col = cols[0]
    filtered = f"{source} WHERE {col} IS NOT NULL"
    if dialect == "bigquery":
        if rank_sql == "COUNT(*)":
            fn = f"APPROX_TOP_COUNT({col}, {n})"
        elif rank_sql == "SUM(fc_of)":
            fn = f"APPROX_TOP_SUM({col}, fc_of, {n})"
        else:
            return exact_sql
        return (
            f"SELECT rec.value AS {col} "
            f"FROM (SELECT {fn} AS fc_tops FROM {filtered}) t, "
            f"UNNEST(t.fc_tops) rec"
        )
    if rank_sql != "COUNT(*)":
        return exact_sql
    if dialect == "snowflake":
        return (
            f"SELECT f.value[0] AS {col} "
            f"FROM (SELECT APPROX_TOP_K({col}, {n}) AS fc_tops FROM {filtered}) t, "
            f"LATERAL FLATTEN(input => t.fc_tops) f"
        )
    if dialect == "duckdb":
        return (
            f"SELECT UNNEST(approx_top_k({col}, {n})) AS {col} FROM {filtered}"
        )
    if dialect in ("databricks", "spark"):
        return (
            f"SELECT rec.item AS {col} "
            f"FROM (SELECT explode(fc_tops) AS rec FROM "
            f"(SELECT approx_top_k({col}, {n}) AS fc_tops FROM {filtered}) _fc_tops"
            f") _fc_expl"
        )
    if dialect == "clickhouse":
        return (
            f"SELECT arrayJoin(topK({n})({col})) AS {col} FROM {filtered}"
        )
    return exact_sql


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


UNIX_KINDS = frozenset({"unix_s", "unix_ms", "unix_us"})
TIME_KINDS = frozenset({"utc", "reporting", "instant"}) | UNIX_KINDS


def from_unix(expr: str, dialect: str, kind: str) -> str:
    """Integer epoch → timestamp instant (UTC)."""
    kind = kind.lower()
    if kind == "unix_s":
        if dialect == "bigquery":
            return f"TIMESTAMP_SECONDS({expr})"
        if dialect == "snowflake":
            return f"TO_TIMESTAMP_TZ({expr})"
        return f"to_timestamp({expr})"
    if kind == "unix_ms":
        if dialect == "bigquery":
            return f"TIMESTAMP_MILLIS({expr})"
        if dialect == "snowflake":
            return f"TO_TIMESTAMP_TZ({expr}, 3)"
        return f"to_timestamp(({expr}) / 1000.0)"
    if kind == "unix_us":
        if dialect == "bigquery":
            return f"TIMESTAMP_MICROS({expr})"
        if dialect == "snowflake":
            return f"TO_TIMESTAMP_TZ({expr}, 6)"
        return f"to_timestamp(({expr}) / 1000000.0)"
    raise ValueError("time_kind must be unix_s, unix_ms, or unix_us")


def period_start_shifted(
    expr: str,
    unit: str,
    week_start: str,
    n: int,
    dialect: str,
    timezone: str = "UTC",
    time_kind: str = "utc",
) -> str:
    """Trunc ``expr`` to ``unit`` then shift ``n`` periods (negative = back).

    Week start is explicit so BigQuery does not inherit Sunday ``WEEK``.
    ``timezone`` is IANA. ``time_kind`` is ``instant`` (TIMESTAMP_TZ /
    TIMESTAMP_LTZ / BigQuery TIMESTAMP), ``utc`` (TIMESTAMP_NTZ / DATETIME
    whose wall-clock numbers are UTC), or ``reporting`` (NTZ / DATETIME
    already in that zone).
    """
    unit = unit.lower()
    week_start = week_start.lower()
    tz = (timezone or "UTC").strip() or "UTC"
    if not re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz):
        raise ValueError("timezone must be an IANA name")
    kind = (time_kind or "utc").strip().lower()
    if kind not in TIME_KINDS:
        raise ValueError(
            "time_kind must be utc, reporting, instant, unix_s, unix_ms, or unix_us"
        )
    if unit not in {"day", "week", "month", "quarter", "year"}:
        raise ValueError("unit must be day, week, month, quarter, or year")
    if week_start not in {"monday", "sunday"}:
        raise ValueError("week_start must be monday or sunday")
    if type(n) is not int:
        raise TypeError("n must be int")
    bq_week = "MONDAY" if week_start == "monday" else "SUNDAY"
    if dialect == "bigquery":
        if expr.lower() == "current_date":
            date_expr = f"CURRENT_DATE('{tz}')"
        elif kind in UNIX_KINDS:
            date_expr = f"DATE({from_unix(expr, dialect, kind)}, '{tz}')"
        elif kind == "reporting":
            date_expr = f"CAST({expr} AS DATE)"
        else:
            # TIMESTAMP and DATETIME-stored-as-UTC both CAST to an instant.
            date_expr = f"DATE(CAST({expr} AS TIMESTAMP), '{tz}')"
        if unit == "day":
            trunc = date_expr
        elif unit == "week":
            trunc = f"DATE_TRUNC({date_expr}, WEEK({bq_week}))"
        else:
            trunc = f"DATE_TRUNC({date_expr}, {unit.upper()})"
        if n == 0:
            return trunc
        op = "DATE_ADD" if n > 0 else "DATE_SUB"
        return f"{op}({trunc}, INTERVAL {abs(n)} {unit.upper()})"
    if dialect == "duckdb":
        if kind in UNIX_KINDS:
            expr = from_unix(expr, dialect, kind)
        if unit == "day":
            trunc = f"CAST({expr} AS DATE)"
        elif unit == "week" and week_start == "sunday":
            trunc = f"(CAST(date_trunc('week', CAST({expr} AS DATE) + 1) AS DATE) - 1)"
        elif unit == "week":
            trunc = f"CAST(date_trunc('week', {expr}) AS DATE)"
        else:
            trunc = f"CAST(date_trunc('{unit}', {expr}) AS DATE)"
        if n == 0:
            return trunc
        if unit == "day":
            return f"({trunc} + {n})"
        if unit == "week":
            return f"({trunc} + {n * 7})"
        return f"({trunc} + INTERVAL {n} {unit})"
    if dialect == "snowflake":
        if expr.lower() == "current_date":
            date_expr = f"CAST(CONVERT_TIMEZONE('{tz}', CURRENT_TIMESTAMP()) AS DATE)"
        elif kind in UNIX_KINDS:
            date_expr = (
                f"CAST(CONVERT_TIMEZONE('{tz}', {from_unix(expr, dialect, kind)}) AS DATE)"
            )
        elif kind == "reporting":
            # TIMESTAMP_NTZ / DATETIME already civil in ``tz``.
            date_expr = f"CAST({expr} AS DATE)"
        elif kind == "instant":
            # TIMESTAMP_TZ (offset on the value) / TIMESTAMP_LTZ (UTC storage).
            date_expr = f"CAST(CONVERT_TIMEZONE('{tz}', {expr}) AS DATE)"
        else:
            # TIMESTAMP_NTZ wall-clock stored as UTC numbers.
            date_expr = (
                f"CAST(CONVERT_TIMEZONE('UTC', '{tz}', {expr}) AS DATE)"
            )
        if unit == "day":
            trunc = date_expr
        elif unit == "week" and week_start == "sunday":
            trunc = (
                f"DATEADD('day', -MOD(DAYOFWEEKISO({date_expr}), 7), {date_expr})"
            )
        elif unit == "week":
            trunc = f"DATEADD('day', 1 - DAYOFWEEKISO({date_expr}), {date_expr})"
        else:
            trunc = f"CAST(DATE_TRUNC('{unit}', {date_expr}) AS DATE)"
        if n == 0:
            return trunc
        if unit == "week":
            return f"DATEADD('day', {n * 7}, {trunc})"
        return f"DATEADD('{unit}', {n}, {trunc})"
    # Fallback: ISO week (Monday) via date_trunc.
    if unit == "day":
        trunc = f"CAST({expr} AS DATE)"
    else:
        trunc = f"CAST(date_trunc('{unit}', {expr}) AS DATE)"
    if n == 0:
        return trunc
    return f"({trunc} + INTERVAL {n} {unit})"


_AS_INSTANT_RE = re.compile(
    r"factcat_as_instant\(\s*([A-Za-z_][A-Za-z0-9_.]*)"
    r"(?:\s*,\s*'(unix_s|unix_ms|unix_us)')?\s*\)",
    re.IGNORECASE,
)


def as_instant(expr: str, dialect: str, time_kind: str = "") -> str:
    """TIMESTAMP instant. DATETIME values are treated as UTC. Unix integers
    become TIMESTAMP_SECONDS / TO_TIMESTAMP_TZ / …"""
    kind = (time_kind or "").strip().lower()
    if kind in UNIX_KINDS:
        return from_unix(expr, dialect, kind)
    _ = dialect
    return f"CAST({expr} AS TIMESTAMP)"


def supports_json_value(dialect: str) -> bool:
    """JSON key extract (``JSON_VALUE``) is BigQuery SQL sugar in v1."""
    return dialect == "bigquery"


def json_value_sql(
    column: str, path: str, dialect: str, *, numeric: bool = False
) -> str:
    if not supports_json_value(dialect):
        raise ValueError("JSON key extract is not available for this warehouse")
    expr = f"JSON_VALUE({column}, '{path}')"
    if numeric:
        return f"SAFE_CAST({expr} AS FLOAT64)"
    return expr


def as_text(expr: str, dialect: str) -> str:
    """Cast ``expr`` to a string for CONCAT overlay labels.

    Overlay SQL is assembled after transpile, so this must be native.
    """
    if dialect == "bigquery":
        return f"CAST({expr} AS STRING)"
    if dialect == "clickhouse":
        return f"toString({expr})"
    return f"CAST({expr} AS VARCHAR)"


def timestamp_at_date(
    date_sql: str, dialect: str, timezone: str, time_kind: str
) -> str:
    """Midnight of ``date_sql`` as an instant comparable to event_time."""
    tz = (timezone or "UTC").strip() or "UTC"
    if not re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz):
        raise ValueError("timezone must be an IANA name")
    kind = (time_kind or "utc").strip().lower()
    if kind not in TIME_KINDS:
        raise ValueError(
            "time_kind must be utc, reporting, instant, unix_s, unix_ms, or unix_us"
        )
    if kind == "reporting":
        if dialect == "snowflake":
            return f"CAST({date_sql} AS TIMESTAMP_NTZ)"
        return f"CAST({date_sql} AS DATETIME)"
    if kind == "utc":
        # Wall-clock UTC (DATETIME / TIMESTAMP_NTZ). Bound in that type so
        # WHERE can isolate the column. CAST on the column defeats BigQuery
        # partition pruning and Snowflake micro-partition pruning.
        if dialect == "snowflake":
            return (
                f"CONVERT_TIMEZONE('{tz}', 'UTC', CAST({date_sql} AS TIMESTAMP_NTZ))"
            )
        return f"DATETIME(TIMESTAMP({date_sql}, '{tz}'))"
    if dialect == "snowflake":
        return (
            f"CONVERT_TIMEZONE('{tz}', "
            f"CAST({date_sql} AS TIMESTAMP_TZ))"
        )
    return f"TIMESTAMP({date_sql}, '{tz}')"


def create_or_replace_relation(
    dest: str,
    select_sql: str,
    dialect: str,
    *,
    materialized: bool,
    comment: str | None = None,
) -> str:
    """CREATE OR REPLACE a cache relation whose body is ``select_sql``.

    BigQuery and Snowflake share the CREATE spelling. ``materialized`` is a
    materialized view the warehouse can refresh; otherwise a table.
    ``comment`` is the cache fingerprint (JSON), stored as a BigQuery
    description or a Snowflake COMMENT.
    """
    kind = "MATERIALIZED VIEW" if materialized else "TABLE"
    head = f"CREATE OR REPLACE {kind} {dest}"
    note = (comment or "").strip()
    if note:
        safe = note.replace("'", "''")
        if dialect == "snowflake":
            head += f" COMMENT = '{safe}'"
        else:
            head += f" OPTIONS(description='{safe}')"
    return f"{head} AS {select_sql}"


def _civil_datetime(expr: str, dialect: str, timezone: str, time_kind: str) -> str:
    """Wall-clock datetime in ``timezone`` for EXTRACT / hour trunc."""
    tz = (timezone or "UTC").strip() or "UTC"
    if not re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz):
        raise ValueError("timezone must be an IANA name")
    kind = (time_kind or "utc").strip().lower()
    if kind in UNIX_KINDS:
        inst = from_unix(expr, dialect, kind)
        if dialect == "bigquery":
            return f"DATETIME({inst}, '{tz}')"
        if dialect == "snowflake":
            return f"CONVERT_TIMEZONE('{tz}', {inst})"
        return inst
    if dialect == "bigquery":
        if kind == "reporting":
            return f"CAST({expr} AS DATETIME)"
        return f"DATETIME(CAST({expr} AS TIMESTAMP), '{tz}')"
    if dialect == "snowflake":
        if kind == "reporting":
            return f"CAST({expr} AS TIMESTAMP_NTZ)"
        if kind == "instant":
            return f"CONVERT_TIMEZONE('{tz}', {expr})"
        return f"CONVERT_TIMEZONE('UTC', '{tz}', {expr})"
    return f"CAST({expr} AS TIMESTAMP)"


def hour_trunc(expr: str, dialect: str, timezone: str, time_kind: str) -> str:
    """Truncate to the hour in the reporting timezone. Not DATE."""
    civil = _civil_datetime(expr, dialect, timezone, time_kind)
    if dialect == "bigquery":
        return f"DATETIME_TRUNC({civil}, HOUR)"
    if dialect == "snowflake":
        return f"DATE_TRUNC('HOUR', {civil})"
    return f"date_trunc('hour', {civil})"


def hour_of_day(expr: str, dialect: str, timezone: str, time_kind: str) -> str:
    """0–23 in the reporting timezone."""
    civil = _civil_datetime(expr, dialect, timezone, time_kind)
    if dialect == "snowflake":
        return f"HOUR({civil})"
    return f"EXTRACT(HOUR FROM {civil})"


def weekday_index(expr: str, dialect: str, timezone: str, time_kind: str) -> str:
    """Monday=0 … Sunday=6 in the reporting timezone."""
    civil = _civil_datetime(expr, dialect, timezone, time_kind)
    if dialect == "bigquery":
        return f"MOD(EXTRACT(DAYOFWEEK FROM {civil}) + 5, 7)"
    if dialect == "snowflake":
        return f"(DAYOFWEEKISO(CAST({civil} AS DATE)) - 1)"
    return f"(CAST(EXTRACT(ISODOW FROM {civil}) AS INTEGER) - 1)"


def hours_ago(
    n: int,
    dialect: str,
    timezone: str = "UTC",
    time_kind: str = "utc",
) -> str:
    """Now minus *n* hours, same type as event_time ``lhs``."""
    if type(n) is not int or n < 0:
        raise ValueError("hours_ago n must be a non-negative int")
    tz = (timezone or "UTC").strip() or "UTC"
    if not re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz):
        raise ValueError("timezone must be an IANA name")
    kind = (time_kind or "utc").strip().lower()
    if dialect == "bigquery":
        if kind == "reporting":
            return (
                f"DATETIME_SUB(CURRENT_DATETIME('{tz}'), INTERVAL {n} HOUR)"
            )
        return f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {n} HOUR)"
    if dialect == "snowflake":
        if kind == "reporting":
            return (
                f"DATEADD('hour', -{n}, CAST(CONVERT_TIMEZONE('{tz}', "
                f"CURRENT_TIMESTAMP()) AS TIMESTAMP_NTZ))"
            )
        if kind == "instant" or kind in UNIX_KINDS:
            return (
                f"DATEADD('hour', -{n}, CONVERT_TIMEZONE('{tz}', "
                f"CURRENT_TIMESTAMP()))"
            )
        return (
            f"DATEADD('hour', -{n}, CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP()))"
        )
    return f"(CURRENT_TIMESTAMP - INTERVAL {n} HOUR)"


def current_hour_start(
    dialect: str, timezone: str = "UTC", time_kind: str = "utc"
) -> str:
    """Start of the current reporting hour, same type as event_time ``lhs``."""
    tz = (timezone or "UTC").strip() or "UTC"
    if not re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz):
        raise ValueError("timezone must be an IANA name")
    kind = (time_kind or "utc").strip().lower()
    if dialect == "bigquery":
        civil = f"DATETIME_TRUNC(CURRENT_DATETIME('{tz}'), HOUR)"
        if kind == "reporting":
            return civil
        return f"TIMESTAMP({civil}, '{tz}')"
    if dialect == "snowflake":
        civil = (
            f"DATE_TRUNC('HOUR', CONVERT_TIMEZONE('{tz}', CURRENT_TIMESTAMP()))"
        )
        if kind == "reporting":
            return f"CAST({civil} AS TIMESTAMP_NTZ)"
        if kind == "instant" or kind in UNIX_KINDS:
            return civil
        return f"CONVERT_TIMEZONE('{tz}', 'UTC', {civil})"
    return "date_trunc('hour', CURRENT_TIMESTAMP)"


def bucket_out(expr: str = "fc_bucket") -> str:
    """Result time-axis column. Type follows the spec bucket (DATE, timestamp, or int)."""
    return f"{expr} AS bucket"


_PERIOD_RE = re.compile(
    r"factcat_period_start_shifted\(\s*"
    r"([A-Za-z_][A-Za-z0-9_.]*)\s*,\s*"
    r"'(day|week|month|quarter|year)'\s*,\s*"
    r"'(monday|sunday)'\s*,\s*"
    r"(-?\d+)"
    r"(?:\s*,\s*'([^']+)'(?:\s*,\s*'(utc|reporting|instant|unix_s|unix_ms|unix_us)')?)?"
    r"\s*\)",
    re.IGNORECASE,
)


def _replace_func_calls(
    sql: str, name: str, rewrite: Callable[[str], str]
) -> str:
    """Replace ``name(args)`` with balanced parentheses, innermost first.

    sqlglot Snowflake emits unquoted function names in uppercase, so the
    match is case-insensitive. ASCII names only; length is unchanged.
    """
    token = name + "("
    haystack = sql.lower()
    needle = token.lower()
    while True:
        start = haystack.find(needle)
        if start < 0:
            return sql
        depth = 1
        i = start + len(token)
        while i < len(sql) and depth:
            ch = sql[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        if depth:
            return sql
        inner = sql[start + len(token) : i - 1]
        sql = sql[:start] + rewrite(inner) + sql[i:]
        haystack = sql.lower()


def _split_top_args(inner: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    quote = ""
    for ch in inner:
        if in_str:
            buf.append(ch)
            if ch == quote:
                in_str = False
            continue
        if ch in {'"', "'"}:
            in_str = True
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth -= 1
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return parts


def splice_placeholders(sql: str, dialect: str) -> str:
    """Replace app placeholders sqlglot cannot emit (week start, period shift)."""

    def repl(match: re.Match[str]) -> str:
        return period_start_shifted(
            match.group(1),
            match.group(2),
            match.group(3),
            int(match.group(4)),
            dialect,
            timezone=match.group(5) or "UTC",
            time_kind=match.group(6) or "utc",
        )

    sql = _AS_INSTANT_RE.sub(
        lambda m: as_instant(m.group(1), dialect, m.group(2) or ""), sql
    )
    sql = _PERIOD_RE.sub(repl, sql)

    def ts_at_date(inner: str) -> str:
        parts = _split_top_args(inner)
        if len(parts) != 3:
            raise ValueError("factcat_ts_at_date expects date, timezone, kind")
        tz = parts[1].strip().strip("'").strip('"')
        kind = parts[2].strip().strip("'").strip('"')
        return timestamp_at_date(parts[0], dialect, tz, kind)

    def _tz_kind_call(
        inner: str, name: str, fn: Callable[[str, str, str, str], str]
    ) -> str:
        parts = _split_top_args(inner)
        if len(parts) != 3:
            raise ValueError(f"{name} expects expr, timezone, kind")
        tz = parts[1].strip().strip("'").strip('"')
        kind = parts[2].strip().strip("'").strip('"')
        return fn(parts[0], dialect, tz, kind)

    sql = _replace_func_calls(sql, "factcat_ts_at_date", ts_at_date)
    sql = _replace_func_calls(
        sql,
        "factcat_hour_trunc",
        lambda inner: _tz_kind_call(inner, "factcat_hour_trunc", hour_trunc),
    )
    sql = _replace_func_calls(
        sql,
        "factcat_hour_of_day",
        lambda inner: _tz_kind_call(inner, "factcat_hour_of_day", hour_of_day),
    )
    sql = _replace_func_calls(
        sql,
        "factcat_dow",
        lambda inner: _tz_kind_call(inner, "factcat_dow", weekday_index),
    )

    def hours_ago_call(inner: str) -> str:
        parts = _split_top_args(inner)
        if not parts:
            raise ValueError("factcat_hours_ago expects an integer")
        try:
            n = int(parts[0].strip())
        except ValueError as exc:
            raise ValueError("factcat_hours_ago expects an integer") from exc
        if len(parts) == 1:
            return hours_ago(n, dialect)
        if len(parts) != 3:
            raise ValueError("factcat_hours_ago expects n, timezone, kind")
        tz = parts[1].strip().strip("'").strip('"')
        kind = parts[2].strip().strip("'").strip('"')
        return hours_ago(n, dialect, tz, kind)

    def current_hour_call(inner: str) -> str:
        parts = _split_top_args(inner)
        if len(parts) != 2:
            raise ValueError("factcat_current_hour_start expects timezone, kind")
        tz = parts[0].strip().strip("'").strip('"')
        kind = parts[1].strip().strip("'").strip('"')
        return current_hour_start(dialect, tz, kind)

    sql = _replace_func_calls(sql, "factcat_hours_ago", hours_ago_call)
    return _replace_func_calls(
        sql, "factcat_current_hour_start", current_hour_call
    )
