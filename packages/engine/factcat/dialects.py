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
4. **Top-N category labels.** BigQuery ``APPROX_TOP_COUNT`` does not transpile
   from DuckDB ``GROUP BY … LIMIT``. Exact pick is ordinary SQL.
5. **Reporting-timezone calendar.** BigQuery ``DATE(ts, tz)`` and
   ``CURRENT_DATE(tz)`` have no DuckDB equivalent sqlglot will emit.
   Snowflake is ``CONVERT_TIMEZONE`` plus an explicit week start (not
   session ``WEEK_START``). Week start is applied after that conversion.
6. **UTC instant from TIMESTAMP or DATETIME.** sqlglot rewrites
   ``CAST(col AS TIMESTAMP) >= TIMESTAMP(...)`` to ``CAST AS DATETIME``,
   which BigQuery then rejects. ``factcat_as_instant`` is spliced after
   transpile.

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

    Exact pick is ``GROUP BY cols ORDER BY rank LIMIT n``. BigQuery ``exact=False``
    and a single column ranked by ``COUNT(*)`` uses ``APPROX_TOP_COUNT``.
    ``APPROX_TOP_COUNT`` skips NULL; the fold never maps NULL to ``(other)``.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    col_csv = ", ".join(cols)
    n_cols = len(cols)
    group_by = ", ".join(str(i) for i in range(1, n_cols + 1))
    if dialect == "bigquery" and not exact and len(cols) == 1 and rank_sql == "COUNT(*)":
        col = cols[0]
        return (
            f"SELECT rec.value AS {col} "
            f"FROM (SELECT APPROX_TOP_COUNT({col}, {n}) AS fc_tops FROM {source}) t, "
            f"UNNEST(t.fc_tops) rec"
        )
    return (
        f"SELECT {col_csv} FROM {source} "
        f"GROUP BY {group_by} "
        f"ORDER BY {rank_sql} DESC, {col_csv} "
        f"LIMIT {n}"
    )


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
    if dialect == "snowflake":
        if kind == "instant" or kind in UNIX_KINDS:
            return (
                f"CONVERT_TIMEZONE('{tz}', "
                f"CAST({date_sql} AS TIMESTAMP_TZ))"
            )
        return (
            f"CONVERT_TIMEZONE('{tz}', 'UTC', CAST({date_sql} AS TIMESTAMP_NTZ))"
        )
    return f"TIMESTAMP({date_sql}, '{tz}')"


def bucket_out(expr: str = "fc_bucket") -> str:
    """Result time-axis column. Always DATE so ORDER BY is not DATETIME/TIMESTAMP."""
    return f"CAST({expr} AS DATE) AS bucket"


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
    """Replace ``name(args)`` with balanced parentheses, innermost first."""
    token = name + "("
    while True:
        start = sql.find(token)
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

    return _replace_func_calls(sql, "factcat_ts_at_date", ts_at_date)
