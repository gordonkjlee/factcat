"""Retention SQL generation.

Every product analytics tool, warehouse-native ones included, hard-codes three
things:

    entity   = user
    period   = a calendar bucket (day / week / month)
    retained = did any event

Real retention definitions violate all three. This module fixes none of them in
place, so a definition like

    "the subscription is retained in a 35-day billing period if payment is
     collected within 5 days of the period opening"

is expressed directly rather than approximated.

Internal join keys are prefixed ``fc_`` so that a caller whose source table
happens to contain a column called ``entity_id`` or ``cohort`` does not collide
with the generated SQL. The three derived columns a predicate may reference -
``offset_days``, ``period_index`` and ``within_period_offset`` - are deliberately
unprefixed, because they are the public API of the ``retained`` expression.
"""

from __future__ import annotations

from ._emit import GRID_RELATION, day_diff, floor_div, transpile_with_grid
from .spec import RetentionSpec


def build_sql(spec: RetentionSpec, dialect: str = "duckdb") -> str:
    """Render ``spec`` as SQL for ``dialect``.

    The result has one row per (cohort, period_index) with the cohort size, the
    number of retained entities, and the percentage.
    """
    where = f"WHERE {spec.where}" if spec.where else ""

    offset = day_diff("c.cohort_ts", "b.fc_event_ts")
    period_index = floor_div(offset, spec.period_days)
    within = f"{offset} - ({period_index}) * {spec.period_days}"

    sql = f"""
    WITH src AS (
        SELECT * FROM {spec.table} {where}
    ),
    base AS (
        SELECT
            src.*,
            {spec.entity}      AS fc_entity,
            {spec.entity_time} AS fc_entity_ts,
            {spec.event_time}  AS fc_event_ts
        FROM src
    ),
    entities AS (
        SELECT
            fc_entity,
            MIN(fc_entity_ts) AS cohort_ts
        FROM base
        GROUP BY 1
    ),
    -- `cohort_ts` is exposed under that exact name because `spec.cohort_bucket`
    -- is caller-supplied SQL that references it.
    cohorts AS (
        SELECT
            fc_entity,
            cohort_ts,
            {spec.cohort_bucket} AS fc_cohort
        FROM entities
    ),
    observations AS (
        SELECT
            b.*,
            c.fc_cohort,
            {offset}       AS offset_days,
            {period_index} AS period_index,
            {within}       AS within_period_offset
        FROM base b
        JOIN cohorts c ON b.fc_entity = c.fc_entity
    ),
    retained_periods AS (
        SELECT DISTINCT fc_entity, period_index
        FROM observations
        WHERE period_index BETWEEN 0 AND {spec.n_periods}
          AND ({spec.retained})
    ),
    grid AS (
        SELECT c.fc_cohort, c.fc_entity, p.period_index
        FROM cohorts c
        CROSS JOIN {GRID_RELATION} p
    )
    SELECT
        g.fc_cohort AS cohort,
        g.period_index,
        COUNT(DISTINCT g.fc_entity) AS cohort_size,
        COUNT(DISTINCT r.fc_entity) AS retained_entities,
        ROUND(
            100.0 * COUNT(DISTINCT r.fc_entity)
            / NULLIF(COUNT(DISTINCT g.fc_entity), 0),
            2
        ) AS retention_pct
    FROM grid g
    LEFT JOIN retained_periods r
           ON r.fc_entity = g.fc_entity
          AND r.period_index = g.period_index
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
    return transpile_with_grid(sql, dialect, spec.n_periods)
