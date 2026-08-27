"""Shared SQL emission helpers.

The strategy: build one query in DuckDB dialect, transpile it with sqlglot, and
splice in the one construct that does not transpile (the integer series). See
``dialects.py`` for why that construct is the only per-warehouse code here.
"""

from __future__ import annotations

import sqlglot

from .dialects import period_grid

# The relation name the retention query joins against for its period grid. It is
# never defined as a CTE in the pre-transpile SQL - sqlglot simply renders it as
# an identifier - and the real CTE is prepended afterwards.
GRID_RELATION = "factcat_period_grid"


def day_diff(start: str, end: str) -> str:
    """Whole days between two instants, in DuckDB dialect for later transpilation."""
    return f"date_diff('day', {start}, {end})"


def floor_div(numerator: str, divisor: int) -> str:
    """Integer division spelled portably.

    DuckDB's ``/`` is float division, and a float period index will not join
    against the integer period grid. ``//`` does not transpile cleanly, so this
    goes through FLOOR and an explicit cast.
    """
    return f"CAST(FLOOR(({numerator}) * 1.0 / {divisor}) AS BIGINT)"


def transpile(sql: str, dialect: str) -> str:
    """Transpile DuckDB-dialect SQL to ``dialect``."""
    if dialect == "duckdb":
        return sql
    return sqlglot.transpile(sql, read="duckdb", write=dialect, pretty=True)[0]


def transpile_with_grid(sql: str, dialect: str, n_periods: int) -> str:
    """Transpile ``sql`` and prepend the dialect-native period grid as a CTE.

    ``sql`` must reference :data:`GRID_RELATION` and must already begin with a
    ``WITH`` clause, which every query in this package does.

    Raises:
        RuntimeError: if the transpiled SQL does not start with ``WITH``. That
            would mean the grid CTE could not be attached, leaving a query that
            references an undefined relation - a failure worth making loud
            rather than emitting SQL that dies at the warehouse.
    """
    out = transpile(sql, dialect).lstrip()
    if not out.upper().startswith("WITH "):
        raise RuntimeError(
            "expected the transpiled query to open with a WITH clause so the "
            f"period grid could be prepended, got: {out[:60]!r}"
        )
    grid = period_grid(n_periods, dialect)
    return f"WITH {GRID_RELATION} AS (\n{grid}\n),\n{out[len('WITH '):]}"
