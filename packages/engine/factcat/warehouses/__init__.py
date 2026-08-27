"""Execute adapters: run generated SQL on a warehouse.

SQL generation is a separate layer (``dialects.py`` + sqlglot). This package
only **runs** SQL. An adapter does not emit SQL, rewrite caller SQL, or know
about ``RetentionSpec``.

The later app holds a ``Warehouse`` and does::

    warehouse = connect("bigquery", project="my-proj", location="EU")
    sql = retention_sql(spec, dialect=warehouse.dialect)
    result = warehouse.run(sql)

Adding an adapter (Snowflake, Databricks, …)
--------------------------------------------

1. New module ``factcat/warehouses/<kind>.py`` with a frozen dataclass,
   ``dialect: ClassVar[str] = "<kind>"``, and
   ``run(self, sql, *, dry_run=False) -> QueryResult``.
2. Constructor takes **that** warehouse's identity and auth. Do not add those
   fields to ``Warehouse`` — BigQuery has project/location; Snowflake does not.
3. Lazy-import the official driver. Optional extra ``factcat[<kind>]``.
4. One line in ``_ADAPTERS`` below.
5. Mock the vendor client in tests. No live warehouse in CI.
6. Do not add SQL generation here. If a construct cannot transpile, it belongs
   in ``dialects.py``.

No shared base class until a second real adapter shows duplicated code.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from ..dialects import SUPPORTED

# kind -> "module:Class". Edit this dict to ship a new adapter. Not a plugin hook.
_ADAPTERS: dict[str, str] = {
    "bigquery": "factcat.warehouses.bigquery:BigQueryWarehouse",
}

ADAPTERS = MappingProxyType(_ADAPTERS)


@dataclass(frozen=True)
class QueryResult:
    """Rows from a warehouse, plus cost fields when the warehouse reports them.

    ``bytes_processed`` / ``bytes_billed`` stay ``None`` when the warehouse has
    no such number (credits, DBUs). Do not stuff those into bytes.
    """

    rows: list[dict[str, Any]]
    bytes_processed: int | None = None
    bytes_billed: int | None = None
    job_id: str | None = None


class Warehouse(Protocol):
    """The whole execute API.

    ``dialect`` is the sqlglot name, the same string ``retention_sql`` takes.
    Identity, auth, and cost knobs live on the concrete class, not here.
    """

    dialect: str

    def run(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        """Execute ``sql``. Dry-run estimates cost and must not fetch rows.

        Adapters that cannot dry-run raise ``DryRunNotSupported``. They must
        not silently execute.
        """
        ...


class WarehouseError(Exception):
    """A warehouse job or connection failed."""


class DryRunNotSupported(WarehouseError):
    """This adapter cannot estimate cost without executing."""


class BytesCapError(WarehouseError):
    """The query exceeded (or would exceed) a scan cap."""

    def __init__(
        self,
        message: str,
        *,
        bytes_processed: int | None = None,
        maximum_bytes_billed: int | None = None,
    ) -> None:
        super().__init__(message)
        self.bytes_processed = bytes_processed
        self.maximum_bytes_billed = maximum_bytes_billed


def connect(kind: str, **kwargs: Any) -> Warehouse:
    """Construct an execute adapter by sqlglot dialect name.

    ``kind`` is the same string the caller already passes to
    ``retention_sql(..., dialect=)``. Keyword arguments are that adapter's
    constructor fields, not a shared connection schema.
    """
    target = _ADAPTERS.get(kind)
    if target is None:
        shipped = ", ".join(sorted(_ADAPTERS)) or "(none)"
        message = f"no execute adapter for {kind!r}; shipped: {shipped}"
        if kind not in SUPPORTED:
            message += f"; SQL generation supports: {', '.join(SUPPORTED)}"
        raise LookupError(message)
    module_name, _, class_name = target.partition(":")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    return cls(**kwargs)


__all__ = [
    "ADAPTERS",
    "BytesCapError",
    "DryRunNotSupported",
    "QueryResult",
    "Warehouse",
    "WarehouseError",
    "connect",
]
