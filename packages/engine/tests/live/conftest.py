"""The live tier's one fixture: a sandbox warehouse named by a mapping file.

``FACTCAT_LIVE_CONFIG`` points at a non-production mapping (the same shape
the app writes) whose write destination belongs to the tier. Without it
every live test skips; a file called ``.factcat.json`` is refused, because
that name is the production mapping and the bootstrap executes DDL.
Nothing read from the file is printed, logged, or put in a repr.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from factcat.warehouses.bigquery import BigQueryAdapter
from factcat_app.catalog import type_sets
from factcat_app.config import warehouse_kind
from tests.test_cross_adapter import _form, sqlglot_warnings  # noqa: F401  (fixture re-export)

CONFIG_ENV = "FACTCAT_LIVE_CONFIG"
PRODUCTION_BASENAME = ".factcat.json"

# Dry runs bill nothing; the cap is the fuse for the bootstrap statements
# that do execute, so a mapping pointed at a large table stops at the cap.
LIVE_BYTES_CAP = 10 * 1024 * 1024

# Only the connection and the mapping come from the file. Chart settings
# stay the hermetic form's, so the live shapes are the same shapes CI walks.
MAPPING_KEYS = (
    "project",
    "data_project",
    "location",
    "credentials",
    "table",
    "entity",
    "event_time",
    "event_time_tz",
    "event_time_epoch",
    "event_column",
    "columns",
    "write_project",
    "write_dataset",
)


@dataclass(frozen=True, repr=False)
class Live:
    kind: str
    adapter: Any
    mapping: dict[str, Any] = field(repr=False)

    def form(self, **extra: Any) -> dict[str, Any]:
        base = _form(self.kind)
        base.update({k: self.mapping[k] for k in MAPPING_KEYS if k in self.mapping})
        base.update(extra)
        return base

    def columns(self, *roles: str, numeric: bool = False) -> list[str]:
        """Mapped column names whose type fits ``roles`` (a ``type_sets``
        key each) or, with ``numeric``, a non-JSON measure type. The
        mapped entity, time and event columns are never offered."""
        sets = type_sets(self.kind)
        wanted: frozenset[str] = frozenset()
        for role in roles:
            wanted = wanted | sets[role]
        if numeric:
            wanted = wanted | (sets["of"] - sets["json"])
        taken = {
            str(self.mapping.get(k) or "")
            for k in ("entity", "event_time", "event_column")
        }
        out: list[str] = []
        for col in self.mapping.get("columns") or []:
            if not isinstance(col, dict):
                continue
            name = str(col.get("name") or "")
            kind = str(col.get("type") or "").upper().split("(", 1)[0].strip()
            if name and name not in taken and kind in wanted:
                out.append(name)
        return out


@pytest.fixture()
def live() -> Live:
    raw = os.environ.get(CONFIG_ENV, "").strip()
    if not raw:
        pytest.skip(f"{CONFIG_ENV} not set")
    path = Path(raw).expanduser()
    if path.name == PRODUCTION_BASENAME:
        pytest.fail(
            f"{CONFIG_ENV} must not be the production mapping ({PRODUCTION_BASENAME}); "
            "point it at a sandbox copy with its own write destination"
        )
    if not path.is_file():
        pytest.fail(f"{CONFIG_ENV} is not a file")
    mapping = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        pytest.fail(f"{CONFIG_ENV} must hold a JSON object")
    kind = warehouse_kind(mapping)
    if kind != "bigquery":
        pytest.skip(f"the live tier has a BigQuery fixture only; the mapping is {kind}")
    for key in ("location", "table", "entity", "event_time", "write_project", "write_dataset"):
        if not str(mapping.get(key) or "").strip():
            pytest.fail(f"{CONFIG_ENV} has no {key}")
    # Jobs run in the billing project, as the app connects; data_project only
    # qualifies the table. A file with just data_project still connects.
    project = str(mapping.get("project") or mapping.get("data_project") or "").strip()
    if not project:
        pytest.fail(f"{CONFIG_ENV} has no project")
    credentials = str(mapping.get("credentials") or "").strip() or None
    adapter = BigQueryAdapter(
        project=project,
        location=str(mapping["location"]).strip(),
        credentials=credentials,
        maximum_bytes_billed=LIVE_BYTES_CAP,
    )
    return Live(kind=kind, adapter=adapter, mapping=mapping)
