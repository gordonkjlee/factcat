"""Execute adapters: push SQL into the caller's warehouse.

Factcat has no warehouse and never takes a copy of the data. SQL generation
is a separate layer (``dialects.py`` + sqlglot). This package only **runs**
that SQL where the caller already stores events, the same way Lightdash
does. An adapter does not emit SQL, rewrite caller SQL, or know about
``RetentionSpec``.

The app stores connection settings (type, project, location, credentials)
and asks the adapter to run SQL on *their* warehouse::

    bq = connect("bigquery", project="my-proj", location="EU")
    sql = retention_sql(spec, dialect=bq.dialect)
    result = bq.run(sql)

Adding an adapter (Snowflake, Databricks, …)
--------------------------------------------

1. New module ``factcat/warehouses/<kind>.py`` with a frozen dataclass,
   ``dialect: ClassVar[str] = "<kind>"``, and
   ``run(self, sql, *, dry_run=False) -> QueryResult``.
2. Constructor takes **that** warehouse's identity and auth (the caller's
   project, account, host, …). Do not add those fields to ``Adapter`` —
   BigQuery has project/location; Snowflake does not.
3. Lazy-import the official driver. Extra ``factcat[<kind>]`` named after
   ``connect(kind=)``. The default install has **no** warehouse SDK; extra
   ``all`` is every shipped driver. Do not put the first warehouse in
   core dependencies.
4. One line in ``_ADAPTERS`` below, and the extra in ``pyproject.toml``.
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
    "bigquery": "factcat.warehouses.bigquery:BigQueryAdapter",
}

ADAPTERS = MappingProxyType(_ADAPTERS)


@dataclass(frozen=True)
class QueryResult:
    """Rows from the caller's warehouse, plus cost fields when it reports them.

    ``bytes_processed`` / ``bytes_billed`` stay ``None`` when the warehouse has
    no such number (credits, DBUs). Do not stuff those into bytes.
    """

    rows: list[dict[str, Any]]
    bytes_processed: int | None = None
    bytes_billed: int | None = None
    job_id: str | None = None


class Adapter(Protocol):
    """Connection to the caller's warehouse.

    ``dialect`` is the sqlglot name, the same string ``retention_sql`` takes.
    Identity, auth, and cost knobs live on the concrete class, not here.
    """

    dialect: str

    def run(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        """Execute ``sql`` in the caller's warehouse.

        Dry-run estimates cost and must not fetch rows. Adapters that cannot
        dry-run raise ``DryRunNotSupported``. They must not silently execute.
        """
        ...


class AdapterError(Exception):
    """The caller's warehouse rejected the job or the connection failed."""


class DryRunNotSupported(AdapterError):
    """This adapter cannot estimate cost without executing."""


class BytesCapError(AdapterError):
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


def connect(kind: str, **kwargs: Any) -> Adapter:
    """Connect to the caller's warehouse by sqlglot dialect name.

    ``kind`` is the same string the caller already passes to
    ``retention_sql(..., dialect=)``. Keyword arguments are that adapter's
    constructor fields (their project, location, key file, …), not a shared
    connection schema.
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
    "Adapter",
    "AdapterError",
    "BytesCapError",
    "DryRunNotSupported",
    "QueryResult",
    "connect",
]
