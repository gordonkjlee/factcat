"""Ordered funnel SQL generation.

Same principle as retention: the entity is a parameter, the step predicates are
arbitrary SQL, and the completion window is measured from the first step rather
than from the previous one. Each step takes the **earliest** qualifying event at
or after the previous step, which is the semantics every funnel tool implements
and few document.

Internal join keys are prefixed ``fc_`` so a source column called ``entity_id``
cannot make the join ambiguous. Step predicates reference source columns
directly and unqualified, as the caller wrote them.

No period grid is needed here, so this transpiles straight through sqlglot.
"""

from __future__ import annotations

from ._emit import day_diff, transpile
from .spec import FunnelSpec


def _quote(label: str) -> str:
    """Render a label as a SQL string literal."""
    return "'" + label.replace("'", "''") + "'"


def build_sql(spec: FunnelSpec, dialect: str = "duckdb") -> str:
    """Render ``spec`` as SQL for ``dialect``.

    The result has one row per step: index, label, entities reaching that step,
    and the percentage of the first step's entities that got there.
    """
    where = f"WHERE {spec.where}" if spec.where else ""
    labels = spec.labels()

    ctes: list[str] = [
        f"""src AS (
        SELECT * FROM {spec.table} {where}
    )""",
        f"""base AS (
        SELECT
            src.*,
            {spec.entity}     AS fc_entity,
            {spec.event_time} AS fc_event_ts
        FROM src
    )""",
        f"""step_0 AS (
        SELECT
            fc_entity            AS fc_entity,
            MIN(fc_event_ts)     AS fc_reached_at,
            MIN(fc_event_ts)     AS fc_first_at
        FROM base
        WHERE ({spec.steps[0]})
        GROUP BY 1
    )""",
    ]

    for i, predicate in enumerate(spec.steps[1:], start=1):
        window = ""
        if spec.within_days is not None:
            elapsed = day_diff("p.fc_first_at", "b.fc_event_ts")
            window = f"AND {elapsed} <= {spec.within_days}"
        ctes.append(
            f"""step_{i} AS (
        SELECT
            p.fc_entity,
            MIN(b.fc_event_ts) AS fc_reached_at,
            p.fc_first_at
        FROM step_{i - 1} p
        JOIN base b ON b.fc_entity = p.fc_entity
        WHERE b.fc_event_ts >= p.fc_reached_at
          AND ({predicate})
          {window}
        GROUP BY p.fc_entity, p.fc_first_at
    )"""
        )

    counts = "\n        UNION ALL\n        ".join(
        f"SELECT {i} AS step_index, {_quote(labels[i])} AS step_label, "
        f"COUNT(*) AS entities FROM step_{i}"
        for i in range(len(spec.steps))
    )
    ctes.append(
        f"""counts AS (
        {counts}
    )"""
    )

    # Joined outside the f-string: a backslash in an f-string expression is only
    # legal from Python 3.12 (PEP 701), and this package supports 3.10.
    cte_sql = ",\n    ".join(ctes)
    sql = f"""
    WITH {cte_sql}
    SELECT
        c.step_index,
        c.step_label,
        c.entities,
        ROUND(100.0 * c.entities / NULLIF(t.total, 0), 2) AS pct_of_first
    FROM counts c
    CROSS JOIN (SELECT entities AS total FROM counts WHERE step_index = 0) t
    ORDER BY 1
    """
    return transpile(sql, dialect)
