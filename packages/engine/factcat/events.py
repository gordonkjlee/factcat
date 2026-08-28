"""Events time-series SQL.

Event family: Total, Uniques, Average (Total / Uniques). Property family:
Sum, Average, Median of ``of``, or mean distinct ``of`` values per entity.
``exact=False`` (default) uses approx NDV / approx median where the dialect
has them. A chart toggle sets ``exact=True``.
"""

from __future__ import annotations

import re

from ._emit import transpile
from .dialects import count_distinct, median_select_from_base
from .spec import EventsSpec

_NDV_RE = re.compile(
    r"factcat_ndv\s*\(\s*(fc_entity|fc_of)\s*\)",
    re.IGNORECASE,
)


def _ndv(expr: str, spec: EventsSpec) -> str:
    if spec.exact:
        return f"COUNT(DISTINCT {expr})"
    return f"factcat_ndv({expr})"


def _splice_ndv(sql: str, dialect: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return count_distinct(match.group(1), dialect, exact=False)

    return _NDV_RE.sub(repl, sql)


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
                {_ndv("fc_of", spec)} AS fc_n
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
        return _splice_ndv(transpile(sql, dialect), dialect)

    if spec.on == "property" and spec.measure == "median":
        sql = f"{base}\n{median_select_from_base(dialect, exact=spec.exact)}"
        return transpile(sql, dialect)

    if spec.on == "events":
        agg = {
            "total": "COUNT(*)",
            "uniques": _ndv("fc_entity", spec),
            "average": (
                f"COUNT(*) * 1.0 / NULLIF({_ndv('fc_entity', spec)}, 0)"
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
    return _splice_ndv(transpile(sql, dialect), dialect)
