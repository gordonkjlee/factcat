"""Events time-series SQL.

Event family: Total, Uniques, Average (Total / Uniques). Property family:
Sum, Average, Median of ``of``, or mean distinct ``of`` values per entity.
``exact=False`` (default) uses approx NDV / approx median where the dialect
has them. A chart toggle sets ``exact=True``.

Breakdowns split the series by caller SQL expressions. Empty ``breakdowns``
emits the same SQL as a spec without those fields.
"""

from __future__ import annotations

import re

from ._emit import transpile
from .dialects import (
    bucket_out,
    count_distinct,
    median_select_from_base,
    splice_placeholders,
    top_labels_select,
)
from .spec import OTHER_LABEL, EventsSpec

_NDV_RE = re.compile(
    r"factcat_ndv\s*\(\s*(fc_entity|fc_of)\s*\)",
    re.IGNORECASE,
)

_TOP_PLACEHOLDER = "SELECT * FROM fc_top_placeholder"


def _ndv(expr: str, spec: EventsSpec) -> str:
    if spec.exact:
        return f"COUNT(DISTINCT {expr})"
    return f"factcat_ndv({expr})"


def _splice_ndv(sql: str, dialect: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return count_distinct(match.group(1), dialect, exact=False)

    return _NDV_RE.sub(repl, sql)


def _finish(sql: str, dialect: str) -> str:
    return splice_placeholders(_splice_ndv(transpile(sql, dialect), dialect), dialect)


def _inject_top_labels(sql: str, spec: EventsSpec, dialect: str) -> str:
    cols = tuple(f"fc_bd_{i}" for i in range(len(spec.breakdowns)))
    rank = "SUM(fc_of)" if spec.on == "property" and spec.measure == "sum" else "COUNT(*)"
    body = top_labels_select(
        "sliced",
        cols,
        spec.top_n,
        dialect=dialect,
        exact=spec.exact,
        rank_sql=rank,
    )
    token = "fc_top_placeholder"
    if token not in sql and f"`{token}`" in sql:
        token = f"`{token}`"
    if token not in sql:
        raise RuntimeError("top-labels placeholder missing after transpile")
    return sql.replace(token, f"({body}) AS _fc_top", 1)


def _base_cte(spec: EventsSpec) -> str:
    where = f"WHERE {spec.where}" if spec.where else ""
    of_col = f"{spec.of} AS fc_of," if spec.of else ""
    # typed.fc_event_ts is the instant used everywhere after src. src.where
    # still filters the table column (partition prune). Same-list aliasing
    # cannot see fc_event_ts, so the bucket is computed one CTE later.
    return f"""
    WITH src AS (
        SELECT * FROM {spec.table} {where}
    ),
    typed AS (
        SELECT
            src.*,
            {spec.event_time} AS fc_event_ts
        FROM src
    ),
    base AS (
        SELECT
            typed.*,
            {spec.entity}          AS fc_entity,
            {of_col}
            {spec.bucket_sql()}    AS fc_bucket
        FROM typed
    )
    """


def _agg(spec: EventsSpec) -> str:
    if spec.on == "events":
        return {
            "total": "COUNT(*)",
            "uniques": _ndv("fc_entity", spec),
            "average": f"COUNT(*) * 1.0 / NULLIF({_ndv('fc_entity', spec)}, 0)",
        }[spec.measure]
    return {
        "sum": "SUM(fc_of)",
        "average": "AVG(fc_of)",
    }[spec.measure]


def _plain_sql(spec: EventsSpec, dialect: str) -> str:
    base = _base_cte(spec)
    if spec.on == "property" and spec.measure == "distinct":
        sql = f"""
        {base},
        per_entity AS (
            SELECT
                fc_bucket,
                fc_entity,
                {_ndv("fc_of", spec)} AS fc_n
            FROM base
            WHERE fc_entity IS NOT NULL
            GROUP BY 1, 2
        )
        SELECT
            {bucket_out()},
            AVG(fc_n) AS value
        FROM per_entity
        GROUP BY 1
        ORDER BY 1
        """
        return _finish(sql, dialect)

    if spec.on == "property" and spec.measure == "median":
        sql = f"{base}\n{median_select_from_base(dialect, exact=spec.exact)}"
        return splice_placeholders(transpile(sql, dialect), dialect)

    sql = f"""
    {base}
    SELECT
        {bucket_out()},
        {_agg(spec)} AS value
    FROM base
    GROUP BY 1
    ORDER BY 1
    """
    return _finish(sql, dialect)


def _attr_cte(spec: EventsSpec, index: int, expr: str) -> str:
    direction = "ASC" if spec.breakdown_at == "first" else "DESC"
    return f"""
    attr_{index} AS (
        SELECT fc_entity, expr_val AS fc_bd_{index}
        FROM (
            SELECT
                {spec.entity} AS fc_entity,
                {expr} AS expr_val,
                ROW_NUMBER() OVER (
                    PARTITION BY {spec.entity}
                    ORDER BY {spec.event_time} {direction}, {spec.entity} {direction}
                ) AS fc_rn
            FROM {spec.table}
            WHERE ({expr}) IS NOT NULL
              AND ({spec.entity}) IS NOT NULL
        ) _ranked_{index}
        WHERE fc_rn = 1
    )
    """


def _pair_match(left_alias: str, right_alias: str, n: int) -> str:
    parts = [
        f"{left_alias}.fc_bd_{i} IS NOT DISTINCT FROM {right_alias}.fc_bd_{i}"
        for i in range(n)
    ]
    return " AND ".join(parts)


def _breakdown_sql(spec: EventsSpec, dialect: str) -> str:
    n = len(spec.breakdowns)
    labels = spec.bd_labels()
    bd_cols = [f"fc_bd_{i}" for i in range(n)]
    fold_cols = [f"fc_fold_{i}" for i in range(n)]
    base = _base_cte(spec)
    extras: list[str] = []
    if spec.breakdown_at in ("first", "last"):
        extras.extend(
            _attr_cte(spec, i, expr) for i, expr in enumerate(spec.breakdowns)
        )
        joins = " ".join(
            f"LEFT JOIN attr_{i} ON base.fc_entity = attr_{i}.fc_entity"
            for i in range(n)
        )
        select_bds = ", ".join(f"attr_{i}.fc_bd_{i}" for i in range(n))
        sliced = f"""
        sliced AS (
            SELECT base.*, {select_bds}
            FROM base
            {joins}
        )
        """
    else:
        select_bds = ", ".join(
            f"({expr}) AS fc_bd_{i}" for i, expr in enumerate(spec.breakdowns)
        )
        sliced = f"""
        sliced AS (
            SELECT base.*, {select_bds}
            FROM base
        )
        """

    join_on = _pair_match("sliced", "t", n)
    null_keep = " AND ".join(f"sliced.fc_bd_{i} IS NULL" for i in range(n))
    # LEFT JOIN, not EXISTS: BigQuery rejects correlated EXISTS against
    # another CTE ("cannot be de-correlated"). NULL is never folded into
    # (other). top_labels keys are non-null (APPROX_TOP_COUNT skips NULL).
    fold_selects = []
    for i in range(n):
        fold_selects.append(
            f"""CASE
                WHEN sliced.fc_bd_{i} IS NULL THEN sliced.fc_bd_{i}
                WHEN t.fc_bd_0 IS NOT NULL
                    THEN sliced.fc_bd_{i}
                ELSE '{OTHER_LABEL}'
            END AS fc_fold_{i}"""
        )
    fold_csv = ", ".join(fold_selects)
    if spec.include_other:
        folded = f"""
        folded AS (
            SELECT sliced.*, {fold_csv}
            FROM sliced
            LEFT JOIN top_labels t ON {join_on}
        )
        """
    else:
        folded = f"""
        folded AS (
            SELECT sliced.*, {", ".join(f"sliced.{c} AS {f}" for c, f in zip(bd_cols, fold_cols))}
            FROM sliced
            LEFT JOIN top_labels t ON {join_on}
            WHERE t.fc_bd_0 IS NOT NULL
               OR ({null_keep})
        )
        """

    extra_select = "".join(
        f", {fold} AS {lab}" for fold, lab in zip(fold_cols, labels)
    )
    n_group = 1 + n
    group_by = ", ".join(str(i) for i in range(1, n_group + 1))
    order_by = group_by
    dim_group = ", ".join(["fc_bucket", *fold_cols])

    ctes = base.rstrip()
    if extras:
        ctes = ctes + "," + ",".join(extras)
    ctes = ctes + "," + sliced + f""",
        top_labels AS (
            {_TOP_PLACEHOLDER}
        ),
        {folded}
    """

    if spec.on == "property" and spec.measure == "distinct":
        sql = f"""
        {ctes}
        per_entity AS (
            SELECT
                {dim_group},
                fc_entity,
                {_ndv("fc_of", spec)} AS fc_n
            FROM folded
            WHERE fc_entity IS NOT NULL
            GROUP BY {", ".join(str(i) for i in range(1, n_group + 2))}
        )
        SELECT
            {bucket_out()}{extra_select},
            AVG(fc_n) AS value
        FROM per_entity
        GROUP BY {group_by}
        ORDER BY {order_by}
        """
        return _inject_top_labels(_finish(sql, dialect), spec, dialect)

    if spec.on == "property" and spec.measure == "median":
        med = median_select_from_base(
            dialect,
            exact=spec.exact,
            extra_group=tuple(fold_cols),
            relation="folded",
        )
        sql = f"""
        {ctes}
        {med}
        """
        finished = _inject_top_labels(
            splice_placeholders(transpile(sql, dialect), dialect), spec, dialect
        )
        rename = ", ".join(
            ["bucket"]
            + [f"{fold} AS {lab}" for fold, lab in zip(fold_cols, labels)]
            + ["value"]
        )
        return f"SELECT {rename} FROM ({finished}) _fc_med"

    sql = f"""
    {ctes}
    SELECT
        {bucket_out()}{extra_select},
        {_agg(spec)} AS value
    FROM folded
    GROUP BY {group_by}
    ORDER BY {order_by}
    """
    return _inject_top_labels(_finish(sql, dialect), spec, dialect)


def build_sql(spec: EventsSpec, dialect: str = "duckdb") -> str:
    """Render ``spec`` as SQL for ``dialect``.

    No breakdowns: one row per bucket (``bucket``, ``value``).
    With breakdowns: one row per bucket and label (``(other)`` when folded).
    """
    if not spec.breakdowns:
        return _plain_sql(spec, dialect)
    return _breakdown_sql(spec, dialect)
