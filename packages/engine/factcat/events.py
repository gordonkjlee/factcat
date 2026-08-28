"""Events time-series SQL.

The everyday question: how many rows, how many distinct entities, or the
average/sum/min/max of a numeric expression, per time bucket. Entity and
bucket are caller-supplied so this does not collapse to unique users by day.
"""

from __future__ import annotations

from ._emit import transpile
from .spec import EventsSpec

_AGG = {
    "total": "COUNT(*)",
    "uniques": "COUNT(DISTINCT fc_entity)",
    "average": "AVG({of})",
    "sum": "SUM({of})",
    "min": "MIN({of})",
    "max": "MAX({of})",
}


def build_sql(spec: EventsSpec, dialect: str = "duckdb") -> str:
    """Render ``spec`` as SQL for ``dialect``.

    The result has one row per bucket: ``bucket``, ``value``.
    """
    where = f"WHERE {spec.where}" if spec.where else ""
    agg = _AGG[spec.measure]
    if spec.of is not None:
        agg = agg.format(of=spec.of)

    sql = f"""
    WITH src AS (
        SELECT * FROM {spec.table} {where}
    ),
    base AS (
        SELECT
            src.*,
            {spec.entity}          AS fc_entity,
            {spec.event_time}      AS fc_event_ts,
            {spec.bucket_sql()}    AS fc_bucket
        FROM src
    )
    SELECT
        fc_bucket AS bucket,
        {agg} AS value
    FROM base
    GROUP BY 1
    ORDER BY 1
    """
    return transpile(sql, dialect)
