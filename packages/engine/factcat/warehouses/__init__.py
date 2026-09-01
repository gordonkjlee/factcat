"""Execute adapters: push SQL into the caller's warehouse.

Factcat has no warehouse and never takes a copy of the data. SQL generation
is a separate layer (``dialects.py`` + sqlglot). This package only **runs**
that SQL where the caller already stores events, the same way Lightdash
does. An adapter does not emit SQL, rewrite caller SQL, or know about
``RetentionSpec``.

The app stores connection settings (kind plus that warehouse's fields)
and asks the adapter to run SQL on *their* warehouse::

    bq = connect("bigquery", project="my-proj", location="EU")
    sql = retention_sql(spec, dialect=bq.dialect)
    result = bq.run(sql)

    sf = connect(
        "snowflake",
        account="xy12345",
        user="ANALYST",
        warehouse="COMPUTE_WH",
        database="ANALYTICS",
        schema="MARTS",
        private_key_path="rsa_key.p8",
    )

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
7. Declare ``capabilities`` on the class (the flags below). The app asks
   ``capabilities(kind)``. Do not add a list of kinds next to each widget.
8. ``driver_available()`` on the class: True when the official driver
   imports. The app asks ``extra_installed(kind)``. Do not pip from here.

Changing a feature that compiles SQL, runs a job, or shows warehouse chrome
---------------------------------------------------------------------------

Walk every key in ``ADAPTERS`` (and ``SUPPORTED`` if it is generation). For
each kind: same behaviour, gated off via ``capabilities`` / a dialect helper,
or an explicit branch with a test. A BigQuery-only edit on a path Snowflake
also executes is a regression. Identity and cost knobs stay on the concrete
class — do not copy ``project`` / ``maximum_bytes_billed`` onto Snowflake.
Say whether that warehouse's Setup guide / README still match, and whether
the other kinds get the same chrome or why not. Setup catalog fields are
``factcat_app.catalog.CATALOG_STEPS`` (one list per kind): enable when
``needs`` are met, then load ``endpoint`` (dataset → tables → columns;
Snowflake the same chain). Greyed until the previous field is set. A
native select closes if options arrive while the menu is open, so do
not wait for first click. Do not copy a second enable/load chain in
the template.

No shared base class until a second real adapter shows duplicated code.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from ..dialects import SUPPORTED

# kind -> "module:Class". Edit this dict to ship a new adapter. Not a plugin hook.
_ADAPTERS: dict[str, str] = {
    "bigquery": "factcat.warehouses.bigquery:BigQueryAdapter",
    "snowflake": "factcat.warehouses.snowflake:SnowflakeAdapter",
}

ADAPTERS = MappingProxyType(_ADAPTERS)

# Execute-chrome flags. One definition: each adapter's ``capabilities``.
# The app asks ``capabilities(kind)``; it does not keep a list of kinds per widget.
CAP_DRY_RUN = "dry_run"
CAP_BYTES_PROCESSED = "bytes_processed"
CAP_SCAN_CAP = "scan_cap"


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
    ``capabilities`` is which execute chrome the adapter supports (dry-run,
    bytes, scan cap). Not connection fields. ``driver_available`` is whether
    the extra's official driver imports — not a connection attempt.
    """

    dialect: str
    capabilities: frozenset[str]

    def run(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        """Execute ``sql`` in the caller's warehouse.

        Dry-run estimates cost and must not fetch rows. Adapters that cannot
        dry-run raise ``DryRunNotSupported``. They must not silently execute.
        """
        ...


class AdapterError(Exception):
    """The caller's warehouse rejected the job or the connection failed."""

    def __init__(self, message: str = "", *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def is_missing_relation(exc: BaseException) -> bool:
    """True when ``exc`` means the relation is absent, not a permission failure.

    Snowflake often uses “does not exist or not authorized” for both; that
    still counts as missing here (CREATE then fails the same way).
    """
    if isinstance(exc, AdapterError) and exc.not_found:
        return True
    text = str(exc)
    lowered = text.lower()
    if re.search(
        r"not found:\s*(table|view|dataset|object|relation|materialized)",
        lowered,
    ):
        return True
    if lowered.strip() == "not found":
        return True
    if "does not exist" in lowered:
        return True
    if "002003" in text:
        return True
    cause = exc.__cause__
    if cause is not None and type(cause).__name__ == "NotFound":
        return True
    return False


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


def _adapter_class(kind: str) -> Any:
    target = _ADAPTERS.get(kind)
    if target is None:
        shipped = ", ".join(sorted(_ADAPTERS)) or "(none)"
        message = f"no execute adapter for {kind!r}; shipped: {shipped}"
        if kind not in SUPPORTED:
            message += f"; SQL generation supports: {', '.join(SUPPORTED)}"
        raise LookupError(message)
    module_name, _, class_name = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def capabilities(kind: str) -> frozenset[str]:
    """Execute-chrome flags for ``kind``. Does not open a warehouse."""
    cls = _adapter_class(kind)
    return frozenset(getattr(cls, "capabilities", ()))


def extra_requirement(kind: str) -> str:
    """PyPI extra named after ``connect(kind=)``. Does not install it."""
    _adapter_class(kind)
    return f"factcat[{kind}]"


def extra_installed(kind: str) -> bool:
    """True when this kind's official driver imports. Does not connect."""
    cls = _adapter_class(kind)
    probe = getattr(cls, "driver_available", None)
    if probe is None:
        return False
    return bool(probe())


def extras_status() -> dict[str, bool]:
    """``kind -> extra_installed`` for every shipped adapter."""
    return {kind: extra_installed(kind) for kind in _ADAPTERS}


def connect(kind: str, **kwargs: Any) -> Adapter:
    """Connect to the caller's warehouse by sqlglot dialect name.

    ``kind`` is the same string the caller already passes to
    ``retention_sql(..., dialect=)``. Keyword arguments are that adapter's
    constructor fields (their project, location, key file, …), not a shared
    connection schema.
    """
    cls = _adapter_class(kind)
    return cls(**kwargs)


__all__ = [
    "ADAPTERS",
    "CAP_BYTES_PROCESSED",
    "CAP_DRY_RUN",
    "CAP_SCAN_CAP",
    "Adapter",
    "AdapterError",
    "is_missing_relation",
    "BytesCapError",
    "DryRunNotSupported",
    "QueryResult",
    "capabilities",
    "connect",
    "extra_installed",
    "extra_requirement",
    "extras_status",
]
