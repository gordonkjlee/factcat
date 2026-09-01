"""Events time-series SQL.

Event family: Total, Uniques, Average (Total / Uniques). Property family:
Sum, Average, Median of ``of``, or mean distinct ``of`` values per entity.
``exact=False`` (default) uses approx NDV / approx median / approx top-N
labels where the dialect has them. A chart toggle sets ``exact=True``.

Breakdowns split the series by caller SQL expressions. Empty ``breakdowns``
emits the same SQL as a spec without those fields. Each column may carry its
own value semantics (``Breakdown``): ``rows`` on the metric row, ``first`` /
``last`` per entity over the unfiltered table (optionally bounded by
``since`` / ``until``), or ``carried`` — last non-null at or before the
row's instant. ``carried`` is emitted with a counting trick (a cumulative
stamp counter then ``FIRST_VALUE`` per group) rather than
``LAST_VALUE(... IGNORE NULLS)``, which several dialects lack — sqlglot
warns and silently strips the modifier for Postgres, yielding wrong SQL.
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
from .spec import OTHER_LABEL, Breakdown, EventsSpec

_NDV_RE = re.compile(
    r"factcat_ndv\s*\(\s*(fc_entity|fc_of)\s*\)",
    re.IGNORECASE,
)

# fc_hit marks a top-labels match in the fold LEFT JOIN. Testing a value
# column instead (t.fc_bd_0 IS NOT NULL) misreads a matched tuple whose
# first axis is NULL as a miss and folds its other axes into (other).
_TOP_PLACEHOLDER = "SELECT *, 1 AS fc_hit FROM fc_top_placeholder"


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


def _attr_cte(
    spec: EventsSpec, name: str, col: str, bd: Breakdown, at: str, *, bounded: bool
) -> str:
    # One non-null value per entity from the UNFILTERED table (spec.where
    # filters the metric, not the attribute — sparse stamps still resolve),
    # narrowed only by explicit fill_from and, when bounded, since/until.
    # Tiebreak at one instant: greatest expression value wins — the same
    # rule the carried stream applies. (The entity is constant within its
    # own partition, so it can never break a tie.)
    direction = "ASC" if at == "first" else "DESC"
    conds = [
        f"({bd.expr}) IS NOT NULL",
        f"({spec.entity}) IS NOT NULL",
    ]
    if bd.fill_from:
        conds.append(f"({bd.fill_from})")
    if bounded and bd.since:
        conds.append(f"({spec.event_time}) >= ({bd.since})")
    if bounded and bd.until:
        conds.append(f"({spec.event_time}) <= ({bd.until})")
    if bounded and bd.before:
        conds.append(f"({spec.event_time}) < ({bd.before})")
    where = "\n              AND ".join(conds)
    return f"""
    {name} AS (
        SELECT fc_entity, expr_val AS {col}
        FROM (
            SELECT
                {spec.entity} AS fc_entity,
                {bd.expr} AS expr_val,
                ROW_NUMBER() OVER (
                    PARTITION BY {spec.entity}
                    ORDER BY {spec.event_time} {direction}, ({bd.expr}) DESC
                ) AS fc_rn
            FROM {spec.table}
            WHERE {where}
        ) _ranked_{name}
        WHERE fc_rn = 1
    )
    """


def _carried_order_keys(i: int) -> str:
    # Stamp-before-needle at one instant (a row sees a stamp at its own
    # time); among same-instant stamps the greatest value sorts last and
    # opens the group the needles join. Every key carries an explicit
    # NULLS FIRST: BigQuery forbids the modifier inside aggregate-function
    # windows, and explicit-in-source matches its default so sqlglot omits
    # it there and emits it verbatim everywhere else.
    return (
        f"fc_t NULLS FIRST, "
        f"CASE WHEN fc_stamp_{i} IS NOT NULL THEN 0 ELSE 1 END NULLS FIRST, "
        f"fc_stamp_{i} NULLS FIRST"
    )


def _carried_ctes(
    spec: EventsSpec,
    carried: list[tuple[int, Breakdown]],
    rows_items: list[tuple[int, Breakdown]],
) -> str:
    # LOCF without IGNORE NULLS: union the stamp stream with the metric
    # rows, count stamps cumulatively per entity so each stamp opens a
    # group, then FIRST_VALUE per (entity, group) is the group's stamp.
    # ONE stamp scan serves every carried column; metric rows with a NULL
    # instant sort into group 0 and carry NULL, but the row survives.
    # The stamp branch pairs bare NULLs against the needle branch's typed
    # columns. That is valid on the union type resolution of every
    # SUPPORTED family, and was verified live on BigQuery (the strictest):
    # dry-run of carried / mixed / property / backfill specs against a
    # real DATETIME-bucketed table validated and returned byte estimates
    # (2026-09-01). Do not "harden" this to CAST(NULL AS ...) — the
    # caller expressions' types are unknown by design.
    stamp_selects: list[str] = []
    stamp_preds: list[str] = []
    for i, bd in carried:
        pred = f"({bd.expr}) IS NOT NULL"
        if bd.fill_from:
            pred = f"{pred} AND ({bd.fill_from})"
        stamp_preds.append(f"({pred})")
        stamp_selects.append(f"CASE WHEN {pred} THEN {bd.expr} END AS fc_stamp_{i}")
    needle_cols = ["fc_entity", "fc_event_ts AS fc_t", "1 AS fc_is_row", "fc_bucket"]
    stamp_cols = ["fc_entity", "fc_t", "0 AS fc_is_row", "NULL AS fc_bucket"]
    if spec.of:
        needle_cols.append("fc_of")
        stamp_cols.append("NULL AS fc_of")
    for i, bd in rows_items:
        needle_cols.append(f"({bd.expr}) AS fc_bd_{i}")
        stamp_cols.append(f"NULL AS fc_bd_{i}")
    for i, bd in carried:
        needle_cols.append(f"NULL AS fc_stamp_{i}")
        stamp_cols.append(f"fc_stamp_{i}")
        if bd.own_value_first:
            # The metric row's own value, evaluated in the needle branch,
            # outranks the (possibly fill_from-narrowed) stamp stream.
            needle_cols.append(f"({bd.expr}) AS fc_self_{i}")
            stamp_cols.append(f"NULL AS fc_self_{i}")
    grp_windows = ",\n            ".join(
        f"""SUM(CASE WHEN fc_stamp_{i} IS NOT NULL THEN 1 ELSE 0 END) OVER (
                PARTITION BY fc_entity
                ORDER BY {_carried_order_keys(i)}
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS fc_grp_{i}"""
        for i, _ in carried
    )
    def _locf_value(i: int, bd: Breakdown) -> str:
        window = f"""FIRST_VALUE(fc_stamp_{i}) OVER (
                PARTITION BY fc_entity, fc_grp_{i}
                ORDER BY {_carried_order_keys(i)}
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            )"""
        if bd.own_value_first:
            return f"COALESCE(fc_self_{i}, {window}) AS fc_bd_{i}"
        return f"{window} AS fc_bd_{i}"

    locf_windows = ",\n            ".join(
        _locf_value(i, bd) for i, bd in carried
    )
    needle_csv = ",\n            ".join(needle_cols)
    stamp_csv = ",\n            ".join(stamp_cols)
    return f"""
    fc_stamps AS (
        SELECT
            {spec.entity}     AS fc_entity,
            {spec.event_time} AS fc_t,
            {", ".join(stamp_selects)}
        FROM {spec.table}
        WHERE ({spec.entity}) IS NOT NULL
          AND ({spec.event_time}) IS NOT NULL
          AND ({" OR ".join(stamp_preds)})
    ),
    fc_stream AS (
        SELECT
            {needle_csv}
        FROM base
        UNION ALL
        SELECT
            {stamp_csv}
        FROM fc_stamps
    ),
    fc_grp AS (
        SELECT fc_stream.*,
            {grp_windows}
        FROM fc_stream
    ),
    fc_locf AS (
        SELECT fc_grp.*,
            {locf_windows}
        FROM fc_grp
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
    resolved = spec.resolved_breakdowns()
    rows_items = [(i, b) for i, b in enumerate(resolved) if b.at == "rows"]
    attr_items = [(i, b) for i, b in enumerate(resolved) if b.at in ("first", "last")]
    carried_items = [(i, b) for i, b in enumerate(resolved) if b.at == "carried"]

    extras: list[str] = []
    for i, bd in attr_items:
        extras.append(
            _attr_cte(spec, f"attr_{i}", f"fc_bd_{i}", bd, bd.at, bounded=True)
        )
        if bd.backfill:
            # Entities with no value by the bound fall back to their first
            # recorded value ever; COALESCE in sliced never overrides a
            # real as-of value.
            extras.append(
                _attr_cte(spec, f"attr_{i}_fb", f"fc_bd_{i}", bd, "first", bounded=False)
            )
    if carried_items:
        extras.append(_carried_ctes(spec, carried_items, rows_items))

    src_alias = "fc_locf" if carried_items else "base"
    join_parts: list[str] = []
    for i, bd in attr_items:
        join_parts.append(
            f"LEFT JOIN attr_{i} ON {src_alias}.fc_entity = attr_{i}.fc_entity"
        )
        if bd.backfill:
            join_parts.append(
                f"LEFT JOIN attr_{i}_fb ON {src_alias}.fc_entity = attr_{i}_fb.fc_entity"
            )
    joins = " ".join(join_parts)

    entry_by_index: dict[int, str] = {}
    for i, bd in attr_items:
        if bd.backfill:
            entry_by_index[i] = (
                f"COALESCE(attr_{i}.fc_bd_{i}, attr_{i}_fb.fc_bd_{i}) AS fc_bd_{i}"
            )
        else:
            entry_by_index[i] = f"attr_{i}.fc_bd_{i}"
    if carried_items:
        # The metric rows come back out of the union stream; rows-mode
        # expressions were evaluated in the needle branch. Invariant: on
        # this path sliced carries ONLY fc_* columns (fc_bucket,
        # fc_entity, fc_of, fc_bd_*) — the rows path keeps base.* but
        # nothing downstream of sliced may rely on source columns.
        for i, _ in carried_items:
            entry_by_index[i] = f"fc_locf.fc_bd_{i}"
        for i, _ in rows_items:
            entry_by_index[i] = f"fc_locf.fc_bd_{i}"
        select_cols = ["fc_locf.fc_bucket", "fc_locf.fc_entity"]
        if spec.of:
            select_cols.append("fc_locf.fc_of")
        select_cols.extend(entry_by_index[i] for i in range(n))
        select_bds = ", ".join(select_cols)
        if joins:
            sliced = f"""
        sliced AS (
            SELECT {select_bds}
            FROM fc_locf
            {joins}
            WHERE fc_is_row = 1
        )
        """
        else:
            sliced = f"""
        sliced AS (
            SELECT {select_bds}
            FROM fc_locf
            WHERE fc_is_row = 1
        )
        """
    else:
        for i, bd in rows_items:
            entry_by_index[i] = f"({bd.expr}) AS fc_bd_{i}"
        select_bds = ", ".join(entry_by_index[i] for i in range(n))
        if joins:
            sliced = f"""
        sliced AS (
            SELECT base.*, {select_bds}
            FROM base
            {joins}
        )
        """
        else:
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
    # (other). The approx pick filters NULL so it does not consume a top-N
    # slot; the first WHEN still keeps a NULL series.
    fold_selects = []
    for i in range(n):
        fold_selects.append(
            f"""CASE
                WHEN sliced.fc_bd_{i} IS NULL THEN sliced.fc_bd_{i}
                WHEN t.fc_hit IS NOT NULL
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
            WHERE t.fc_hit IS NOT NULL
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
        # The comma is load-bearing: folded is not the last CTE here.
        sql = f"""
        {ctes}
        , per_entity AS (
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
