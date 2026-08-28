"""Events time-series SQL.

Event family: Total, Uniques, Average (Total / Uniques). Property family:
Sum, Average, Median of ``of``, or mean distinct ``of`` values per entity.
Entity and bucket are caller-supplied so this does not collapse to unique
users by day.
"""

from __future__ import annotations

from ._emit import transpile
from .dialects import median_select_from_base
from .spec import EventsSpec


def build_sql(spec: EventsSpec, dialect: str = "duckdb") -> str:
    """Render ``spec`` as SQL for ``dialect``.

    The result has one row per bucket: ``bucket``, ``value``.
    """
    where = f"WHERE {spec.where}" if spec.where else ""
    of_col = f"{spec.of} AS fc_of," if spec.of else ""

    base = f"""
    WITH src AS (
        SELECT * FROM {spec.table} {where}
    ),
    base AS (
        SELECT
            src.*,
            {spec.entity}          AS fc_entity,
            {spec.event_time}      AS fc_event_ts,
            {of_col}
            {spec.bucket_sql()}    AS fc_bucket
        FROM src
    )
    """

    if spec.on == "property" and spec.measure == "distinct":
        sql = f"""
        {base},
        per_entity AS (
            SELECT
                fc_bucket,
                fc_entity,
                COUNT(DISTINCT fc_of) AS fc_n
            FROM base
            WHERE fc_entity IS NOT NULL
            GROUP BY 1, 2
        )
        SELECT
            fc_bucket AS bucket,
            AVG(fc_n) AS value
        FROM per_entity
        GROUP BY 1
        ORDER BY 1
        """
        return transpile(sql, dialect)

    if spec.on == "property" and spec.measure == "median":
        sql = f"{base}\n{median_select_from_base(dialect)}"
        return transpile(sql, dialect)

    if spec.on == "events":
        agg = {
            "total": "COUNT(*)",
            "uniques": "COUNT(DISTINCT fc_entity)",
            "average": (
                "COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT fc_entity), 0)"
            ),
        }[spec.measure]
    else:
        agg = {
            "sum": "SUM(fc_of)",
            "average": "AVG(fc_of)",
        }[spec.measure]

    sql = f"""
    {base}
    SELECT
        fc_bucket AS bucket,
        {agg} AS value
    FROM base
    GROUP BY 1
    ORDER BY 1
    """
    return transpile(sql, dialect)
