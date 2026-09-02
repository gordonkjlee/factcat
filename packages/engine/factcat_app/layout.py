"""Setup layout diagnostic: relation kind, date prune, clustering, write dest.

Facts only. The Setup page renders copy from the typed payload.
"""

from __future__ import annotations

from typing import Any

from factcat._emit import transpile
from factcat.dialects import splice_placeholders
from factcat.warehouses import AdapterError, CAP_DRY_RUN, capabilities
from factcat.warehouses.bigquery import (
    dry_run_scan,
    get_table_relation,
    partition_avg_bytes,
    test_dataset_iam,
)
from factcat.warehouses.snowflake import schema_write_privileges
from factcat_app.catalog import columns_from_form, form_kind
from factcat_app.query import (
    _ident_column,
    _ident_table,
    _sql_string,
    _time_clauses,
)

_PRUNE_RATIO = 0.5
_PRUNE_DELTA = 20 * 1024 * 1024  # 20 MiB
_GB = 1024**3
_SPOKE_VIEW_MIN = 3


def _combined_table(form: dict[str, Any]) -> str:
    raw = str(form.get("table") or "").strip()
    if raw:
        return raw
    if form_kind(form) == "snowflake":
        parts = [
            str(form.get("database") or "").strip(),
            str(form.get("schema") or "").strip(),
            str(form.get("table_name") or "").strip(),
        ]
        if all(parts):
            return ".".join(parts)
        return ""
    project = (
        str(form.get("data_project") or "").strip()
        or str(form.get("project") or "").strip()
    )
    dataset = str(form.get("dataset") or "").strip()
    table = str(form.get("table_name") or "").strip()
    if dataset and table and project:
        return f"{project}.{dataset}.{table}"
    if dataset and table:
        return f"{dataset}.{table}"
    return ""


def relation_fingerprint(form: dict[str, Any]) -> dict[str, str]:
    return {
        "kind": form_kind(form),
        "table": _combined_table(form),
        "location": str(form.get("location") or "").strip(),
    }


def date_fingerprint(form: dict[str, Any]) -> dict[str, str]:
    return {
        **relation_fingerprint(form),
        "event_time": str(form.get("event_time") or "").strip(),
        "event_time_tz": str(form.get("event_time_tz") or "").strip(),
        "event_time_epoch": str(form.get("event_time_epoch") or "").strip(),
        "reporting_timezone": str(form.get("reporting_timezone") or "").strip(),
        "week_start": str(form.get("week_start") or "").strip(),
    }


def cluster_fingerprint(form: dict[str, Any]) -> dict[str, str]:
    return {
        **relation_fingerprint(form),
        "event_column": str(form.get("event_column") or "").strip(),
        "entity": str(form.get("entity") or "").strip(),
    }


def cluster_probe_fingerprint(form: dict[str, Any]) -> dict[str, str]:
    return {
        **date_fingerprint(form),
        "event_column": str(form.get("event_column") or "").strip(),
        "sample_event": _sample_event_name(form),
    }


def _slot_hit(stored: Any, name: str, fingerprint: dict[str, str]) -> Any:
    if not isinstance(stored, dict):
        return None
    slot = stored.get(name)
    if not isinstance(slot, dict):
        return None
    if slot.get("fingerprint") != fingerprint:
        return None
    return slot.get("payload")


def _slot(fingerprint: dict[str, str], payload: Any) -> dict[str, Any]:
    return {"fingerprint": fingerprint, "payload": payload}


def _leading_cluster(relation: dict[str, Any] | None) -> str:
    fields = list((relation or {}).get("clustering") or [])
    if not fields:
        return ""
    first = str(fields[0]).strip()
    if "(" in first:
        return ""
    return first.strip('"').strip("`")


def _event_name_leading(relation: dict[str, Any] | None, event_column: str) -> bool:
    if not event_column:
        return False
    lead = _leading_cluster(relation)
    return bool(lead) and lead.lower() == event_column.lower()


def _cluster_idents(relation: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    for raw in list((relation or {}).get("clustering") or []):
        text = str(raw).strip().strip('"').strip("`")
        if not text or "(" in text:
            continue
        out.append(text)
    return out


def _ident_in_cluster(relation: dict[str, Any] | None, column: str) -> int:
    """1-based position of a bare cluster key, or 0."""
    if not column:
        return 0
    want = column.lower()
    for i, name in enumerate(_cluster_idents(relation), start=1):
        if name.lower() == want:
            return i
    return 0


def _emit(form: dict[str, Any], sql: str) -> str:
    dialect = form_kind(form)
    return splice_placeholders(transpile(sql, dialect), dialect)


def prune_count_sql(
    form: dict[str, Any], *, window: bool, event_name: str | None = None
) -> str:
    table = _ident_table(_combined_table(form), "table")
    clauses: list[str] = []
    if window:
        event_time = _ident_column(str(form.get("event_time") or ""), "event_time")
        probe = {
            **form,
            "grain": "day",
            "range_mode": "last",
            "range_n": 90,
            "range_unit": "day",
            "exclude_current": False,
            "include_current": True,
        }
        clauses.extend(_time_clauses(probe, event_time))
    if event_name:
        col = _ident_column(str(form.get("event_column") or ""), "event_column")
        clauses.append(f"{col} = {_sql_string(event_name)}")
    if not clauses:
        return _emit(form, f"SELECT COUNT(*) AS fc_n FROM {table}")
    where = " AND ".join(clauses)
    return _emit(form, f"SELECT COUNT(*) AS fc_n FROM {table} WHERE {where}")


def _bq_creds(form: dict[str, Any]) -> dict[str, Any]:
    project = (
        str(form.get("project") or "").strip()
        or str(form.get("data_project") or "").strip()
    )
    if not project:
        raise ValueError("project is required")
    location = str(form.get("location") or "").strip()
    creds = str(form.get("credentials") or "").strip() or None
    return {"project": project, "location": location, "credentials": creds}


def _split_ident(ident: str) -> tuple[str, str, str]:
    parts = ident.split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return "", "", ident


def _enrich_bases(form: dict[str, Any], relation: dict[str, Any]) -> dict[str, Any]:
    """For a BigQuery view, dry-run unfiltered COUNT to list referenced tables."""
    kind = form_kind(form)
    if kind != "bigquery":
        return {**relation, "bases": list(relation.get("bases") or [])}
    if relation.get("kind") not in {"view", "materialized_view"}:
        return {**relation, "bases": []}
    creds = _bq_creds(form)
    mapped = _combined_table(form)
    try:
        scan = dry_run_scan(sql=prune_count_sql(form, window=False), **creds)
    except AdapterError:
        return {**relation, "bases": [], "unfiltered_bytes": None, "bases_error": True}
    bases: list[dict[str, Any]] = []
    seen: set[str] = set()
    extra: list[tuple[str, str, str]] = []
    for ident in scan.get("referenced_tables") or []:
        project, dataset, table = _split_ident(str(ident))
        if not dataset or not table:
            continue
        key = f"{project}.{dataset}.{table}".lower()
        if key in seen:
            continue
        seen.add(key)
        if mapped and table.lower() == str(form.get("table_name") or "").lower():
            continue
        try:
            info = get_table_relation(
                project=project or creds["project"],
                dataset=dataset,
                table=table,
                credentials=creds.get("credentials"),
            )
        except AdapterError:
            info = {
                "name": table,
                "kind": "table",
                "partition": None,
                "clustering": [],
                "require_partition_filter": False,
            }
        info["qualname"] = f"{project}.{dataset}.{table}" if project else f"{dataset}.{table}"
        bases.append(info)
        if info.get("kind") in {"view", "materialized_view"}:
            extra.append((project or creds["project"], dataset, table))
    # One extra hop if a referenced object is itself a view.
    for project, dataset, table in extra[:4]:
        try:
            hop = dry_run_scan(
                sql=_emit(
                    form,
                    f"SELECT COUNT(*) AS fc_n FROM "
                    f"{_ident_table(f'{project}.{dataset}.{table}', 'table')}",
                ),
                **creds,
            )
        except AdapterError:
            continue
        for ident in hop.get("referenced_tables") or []:
            p2, d2, t2 = _split_ident(str(ident))
            if not d2 or not t2:
                continue
            key = f"{p2}.{d2}.{t2}".lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                info = get_table_relation(
                    project=p2 or creds["project"],
                    dataset=d2,
                    table=t2,
                    credentials=creds.get("credentials"),
                )
            except AdapterError:
                continue
            info["qualname"] = f"{p2}.{d2}.{t2}" if p2 else f"{d2}.{t2}"
            bases.append(info)
    out = dict(relation)
    out["bases"] = bases
    out["unfiltered_bytes"] = scan.get("bytes_processed")
    return out


def _partition_avg(form: dict[str, Any], target: dict[str, Any]) -> int | None:
    if form_kind(form) != "bigquery":
        return None
    if not target.get("partition"):
        return None
    creds = _bq_creds(form)
    qual = str(target.get("qualname") or "")
    project, dataset, table = _split_ident(qual)
    if not dataset or not table:
        dataset = str(form.get("dataset") or "").strip()
        table = str(target.get("name") or form.get("table_name") or "").strip()
        project = (
            str(form.get("data_project") or "").strip()
            or creds["project"]
        )
    else:
        project = project or creds["project"]
    try:
        return partition_avg_bytes(
            project=project,
            dataset=dataset,
            table=table,
            location=creds["location"],
            credentials=creds.get("credentials"),
        )
    except (AdapterError, ImportError, ValueError):
        return None


def _probe_bq(form: dict[str, Any], relation: dict[str, Any]) -> dict[str, Any]:
    creds = _bq_creds(form)
    unfiltered = relation.get("unfiltered_bytes")
    require_filter = False
    if unfiltered is None:
        try:
            scan_b = dry_run_scan(sql=prune_count_sql(form, window=False), **creds)
            unfiltered = scan_b.get("bytes_processed")
        except AdapterError as exc:
            message = str(exc).lower()
            if "partition" in message and "filter" in message:
                require_filter = True
                unfiltered = None
            else:
                return {"status": "error", "error": str(exc)}
    try:
        scan_a = dry_run_scan(sql=prune_count_sql(form, window=True), **creds)
    except AdapterError as exc:
        return {"status": "error", "error": str(exc)}
    filtered = scan_a.get("bytes_processed")
    if require_filter and filtered is not None:
        return {
            "status": "ok",
            "verdict": "require_filter",
            "bytes_filtered": filtered,
            "bytes_unfiltered": unfiltered,
        }
    if filtered is None or unfiltered is None:
        return {
            "status": "ok",
            "verdict": "unknown",
            "bytes_filtered": filtered,
            "bytes_unfiltered": unfiltered,
        }
    return {
        "status": "ok",
        "verdict": "prunes" if _bytes_pruned(int(filtered), int(unfiltered)) else "no_prune",
        "bytes_filtered": int(filtered),
        "bytes_unfiltered": int(unfiltered),
    }


def _sample_event_name(form: dict[str, Any]) -> str:
    for key in ("event_values", "event_names"):
        raw = form.get(key)
        if isinstance(raw, list):
            for item in raw:
                text = str(item or "").strip()
                if text:
                    return text
        elif str(raw or "").strip():
            return str(raw).strip()
    return str(form.get("event_value") or "").strip()


def _bytes_pruned(filtered: int, baseline: int) -> bool:
    delta = int(baseline) - int(filtered)
    return int(filtered) < int(baseline) * _PRUNE_RATIO and delta > _PRUNE_DELTA


def _probe_cluster_bq(
    form: dict[str, Any], *, window: bool, baseline_bytes: int | None
) -> dict[str, Any]:
    """Dry-run an event-name equality against a date-window (or full scan).

    BigQuery's estimate often ignores clustering; a drop is a positive
    signal, a non-drop is not proof the table is unclustered.
    """
    name = _sample_event_name(form)
    column = str(form.get("event_column") or "").strip()
    if not name or not column:
        return {"status": "skipped", "verdict": None}
    creds = _bq_creds(form)
    if baseline_bytes is None:
        try:
            scan_b = dry_run_scan(
                sql=prune_count_sql(form, window=window), **creds
            )
            baseline_bytes = scan_b.get("bytes_processed")
        except AdapterError as exc:
            return {"status": "error", "error": str(exc)}
    try:
        scan_a = dry_run_scan(
            sql=prune_count_sql(form, window=window, event_name=name), **creds
        )
    except AdapterError as exc:
        return {"status": "error", "error": str(exc)}
    filtered = scan_a.get("bytes_processed")
    if filtered is None or baseline_bytes is None:
        return {
            "status": "ok",
            "verdict": "unknown",
            "bytes_filtered": filtered,
            "bytes_unfiltered": baseline_bytes,
            "event_name": name,
        }
    return {
        "status": "ok",
        "verdict": "prunes" if _bytes_pruned(int(filtered), int(baseline_bytes)) else "no_prune",
        "bytes_filtered": int(filtered),
        "bytes_unfiltered": int(baseline_bytes),
        "event_name": name,
    }


def _metadata_timestamp_verdict(
    relation: dict[str, Any], event_time: str, *, kind: str = ""
) -> str | None:
    """Match / mismatch / none from advertised partition field. None = silent.

    Snowflake has no partition column; ``partition: None`` is not
    "unpartitioned" in the BigQuery sense.
    """
    kind = (kind or "bigquery").strip().lower() or "bigquery"
    if relation.get("kind") in {"view", "materialized_view"}:
        bases = list(relation.get("bases") or [])
        if not bases:
            return None
        for base in bases:
            part = base.get("partition") or {}
            field = str(part.get("field") or "")
            if field and event_time and field.lower() == event_time.lower():
                return "match"
        if any((b.get("partition") or {}).get("field") for b in bases):
            return "mismatch"
        if any((b.get("partition") or {}).get("ingestion") for b in bases):
            return "ingestion"
        return None
    part = relation.get("partition")
    if not part:
        if kind == "snowflake":
            return None
        return "none"
    if part.get("ingestion"):
        return "ingestion"
    field = str(part.get("field") or "")
    if event_time and field.lower() == event_time.lower():
        return "match"
    if field:
        return "mismatch"
    return "none"


def _grain_type(relation: dict[str, Any], event_time: str) -> str | None:
    candidates = [relation, *(relation.get("bases") or [])]
    for item in candidates:
        part = item.get("partition") or {}
        field = str(part.get("field") or "")
        if event_time and field.lower() == event_time.lower():
            ptype = str(part.get("type") or "").upper()
            return ptype or None
    return None


def _truthy(value: Any) -> bool:
    if value is True or value == 1:
        return True
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _fetch_relation(form: dict[str, Any]) -> tuple[dict[str, Any], str]:
    payload = columns_from_form(form)
    relation = dict(payload.get("relation") or {})
    relation.setdefault("name", str(form.get("table_name") or ""))
    relation.setdefault("kind", "table")
    relation.setdefault("clustering", [])
    relation.setdefault("partition", None)
    location = (
        str(form.get("location") or "").strip()
        or str(payload.get("location") or "").strip()
    )
    if location and not str(form.get("location") or "").strip():
        form = {**form, "location": location}
    if form_kind(form) == "bigquery" and location:
        relation = _enrich_bases(form, relation)
    return relation, location


def _avg_target(relation: dict[str, Any], event_time: str) -> dict[str, Any]:
    if relation.get("kind") in {"view", "materialized_view"}:
        for base in relation.get("bases") or []:
            part = base.get("partition") or {}
            field = str(part.get("field") or "")
            if event_time and field.lower() == event_time.lower():
                return base
    return relation


def _compute_date_facts(
    form: dict[str, Any], relation: dict[str, Any], *, want_avg: bool
) -> dict[str, Any]:
    event_time = str(form.get("event_time") or "").strip()
    kind = form_kind(form)
    meta = (
        _metadata_timestamp_verdict(relation, event_time, kind=kind)
        if event_time
        else None
    )
    probe: dict[str, Any] = {"status": "skipped", "verdict": None}
    silent = event_time and meta is None
    if (
        event_time
        and kind == "bigquery"
        and CAP_DRY_RUN in capabilities(kind)
        and silent
        and str(form.get("location") or "").strip()
    ):
        probe = _probe_bq(form, relation)
    elif event_time and meta == "match":
        probe = {"status": "skipped", "verdict": "prunes"}
    elif event_time and meta in {"mismatch", "ingestion", "none"}:
        probe = {"status": "skipped", "verdict": meta}
    avg = None
    if (
        want_avg
        and event_time
        and (meta == "match" or probe.get("verdict") == "prunes")
    ):
        avg = _partition_avg(form, _avg_target(relation, event_time))
    return {
        "probe": probe,
        "metadata_verdict": meta,
        "grain": _grain_type(relation, event_time),
        "partition_avg_bytes": avg,
    }


def _apply_cluster_probe(
    form: dict[str, Any],
    relation: dict[str, Any],
    cluster_note: dict[str, Any],
    date_pack: dict[str, Any],
    stored: dict[str, Any],
    *,
    force: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    kind = form_kind(form)
    event_column = str(form.get("event_column") or "").strip()
    if (
        cluster_note.get("status") != "unknown"
        or kind != "bigquery"
        or CAP_DRY_RUN not in capabilities(kind)
        or not event_column
        or not _sample_event_name(form)
        or not str(form.get("location") or "").strip()
    ):
        return cluster_note, None
    fp = cluster_probe_fingerprint(form)
    probe = None if force else _slot_hit(stored, "cluster_probe", fp)
    if not isinstance(probe, dict):
        baseline = (date_pack.get("probe") or {}).get("bytes_filtered")
        probe = _probe_cluster_bq(
            form,
            window=bool(str(form.get("event_time") or "").strip()),
            baseline_bytes=int(baseline) if baseline is not None else None,
        )
    verdict = probe.get("verdict")
    if verdict == "prunes":
        cluster_note = {**cluster_note, "status": "prunes", "probe": probe}
    elif verdict == "no_prune":
        cluster_note = {**cluster_note, "status": "no_prune", "probe": probe}
    else:
        cluster_note = {**cluster_note, "probe": probe}
    return cluster_note, _slot(fp, probe)


def assemble_layout(
    form: dict[str, Any],
    stored: dict[str, Any] | None = None,
    *,
    force: bool = False,
    want_avg: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build Setup layout facts, reusing per-check cache slots.

    Relation metadata is keyed by table. Date prune is keyed by timestamp
    knobs. Clustering interpretation is derived from the relation plus the
    mapped event-name / entity columns — no warehouse round-trip.
    """
    stored = stored if isinstance(stored, dict) else {}
    rel_fp = relation_fingerprint(form)
    rel_pack = None if force else _slot_hit(stored, "relation", rel_fp)
    if not isinstance(rel_pack, dict) or "relation" not in rel_pack:
        relation, location = _fetch_relation(form)
        if location and not str(form.get("location") or "").strip():
            form = {**form, "location": location}
        rel_fp = relation_fingerprint(form)
        rel_pack = {"relation": relation, "location": location}
    else:
        relation = dict(rel_pack.get("relation") or {})
        location = str(rel_pack.get("location") or "").strip()
        if location and not str(form.get("location") or "").strip():
            form = {**form, "location": location}

    date_fp = date_fingerprint(form)
    date_pack = None if force else _slot_hit(stored, "date", date_fp)
    if not isinstance(date_pack, dict):
        date_pack = _compute_date_facts(form, relation, want_avg=want_avg)
    elif (
        want_avg
        and date_pack.get("partition_avg_bytes") is None
        and str(form.get("event_time") or "").strip()
        and (
            date_pack.get("metadata_verdict") == "match"
            or (date_pack.get("probe") or {}).get("verdict") == "prunes"
        )
    ):
        avg = _partition_avg(
            form, _avg_target(relation, str(form.get("event_time") or "").strip())
        )
        date_pack = {**date_pack, "partition_avg_bytes": avg}

    kind = form_kind(form)
    cluster_note = _cluster_note(
        relation,
        str(form.get("event_column") or "").strip(),
        str(form.get("entity") or "").strip(),
        kind,
    )
    cluster_note, probe_slot = _apply_cluster_probe(
        form, relation, cluster_note, date_pack, stored, force=force
    )
    payload = {
        "relation": relation,
        "probe": date_pack.get("probe") or {"status": "skipped", "verdict": None},
        "metadata_verdict": date_pack.get("metadata_verdict"),
        "grain": date_pack.get("grain"),
        "partition_avg_bytes": date_pack.get("partition_avg_bytes"),
        "cluster": cluster_note,
    }
    store = {
        "relation": _slot(rel_fp, rel_pack),
        "date": _slot(date_fp, date_pack),
        "cluster": _slot(cluster_fingerprint(form), cluster_note),
        "payload": payload,
    }
    if probe_slot is not None:
        store["cluster_probe"] = probe_slot
    elif isinstance(stored.get("cluster_probe"), dict):
        store["cluster_probe"] = stored["cluster_probe"]
    return payload, store


def layout_from_form(form: dict[str, Any]) -> dict[str, Any]:
    """Typed layout facts for Setup. Copy is rendered on the client."""
    payload, _store = assemble_layout(
        form,
        stored=None,
        force=True,
        want_avg=_truthy(form.get("include_partition_avg")),
    )
    return payload


def _cluster_note(
    relation: dict[str, Any],
    event_column: str,
    entity: str,
    kind: str,
) -> dict[str, Any]:
    bases = list(relation.get("bases") or [])
    mapped_kind = str(relation.get("kind") or "table")
    if mapped_kind in {"view", "materialized_view"} and len(bases) >= _SPOKE_VIEW_MIN:
        if event_column and any(_event_name_leading(b, event_column) for b in bases):
            hit = next(b for b in bases if _event_name_leading(b, event_column))
            return _cluster_ok(hit, event_column, entity, kind)
        note = {"status": "spokes", "n": len(bases)}
        if kind == "bigquery" and entity:
            spoke = next(
                (b for b in bases if _ident_in_cluster(b, entity)),
                None,
            )
            if spoke:
                note["entity_pos"] = _ident_in_cluster(spoke, entity)
                note["entity"] = entity
                note["on"] = spoke.get("name")
            else:
                note["entity_pos"] = 0
                note["entity"] = entity
        return note
    targets = bases or [relation]
    if event_column:
        for item in targets:
            if _event_name_leading(item, event_column):
                return _cluster_ok(item, event_column, entity, kind)
        lead = _leading_cluster(targets[0] if targets else relation)
        if lead:
            return {
                "status": "not_leading",
                "leading": lead,
                "fields": list(
                    (targets[0] if targets else relation).get("clustering") or []
                ),
                "entity_pos": _ident_in_cluster(targets[0] if targets else relation, entity)
                if kind == "bigquery"
                else 0,
                "entity": entity,
            }
        if mapped_kind in {"view", "materialized_view"} and not bases:
            return {"status": "unknown"}
        if kind == "snowflake" and not (relation.get("clustering") or []):
            auto = bool(relation.get("automatic_clustering"))
            return {"status": "no_key", "automatic_clustering": auto}
        return {"status": "missing", "entity": entity, "entity_pos": 0}
    if kind == "snowflake" and not (relation.get("clustering") or []):
        return {
            "status": "no_key",
            "automatic_clustering": bool(relation.get("automatic_clustering")),
        }
    fields = list(relation.get("clustering") or [])
    if fields:
        return _cluster_ok(relation, event_column, entity, kind)
    return {"status": "none"}


def _cluster_ok(
    item: dict[str, Any], event_column: str, entity: str, kind: str
) -> dict[str, Any]:
    fields = list(item.get("clustering") or [])
    note = {
        "status": "ok",
        "leading": _leading_cluster(item),
        "fields": fields,
        "on": item.get("name"),
        "entity": entity,
        "entity_pos": 0,
    }
    if kind == "bigquery" and entity:
        note["entity_pos"] = _ident_in_cluster(item, entity)
    return note


def write_access_from_form(form: dict[str, Any]) -> dict[str, Any]:
    kind = form_kind(form)
    if kind == "snowflake":
        database = str(form.get("write_database") or "").strip()
        schema = str(form.get("write_schema") or "").strip()
        if not database or not schema:
            return {"status": "skipped"}
        from factcat_app.catalog import _sf_auth

        grants = schema_write_privileges(
            database=database, schema=schema, **_sf_auth(form)
        )
        ok = bool(grants.get("create_table"))
        return {
            "status": "ok" if ok else "denied",
            "create_table": grants.get("create_table"),
            "create_materialized_view": grants.get("create_materialized_view"),
            "dest": f"{database}.{schema}",
        }
    project = str(form.get("write_project") or "").strip()
    dataset = str(form.get("write_dataset") or "").strip()
    if not project or not dataset:
        return {"status": "skipped"}
    creds = (form.get("credentials") or "").strip() or None
    granted = test_dataset_iam(
        project=project, dataset=dataset, credentials=creds
    )
    ok = "bigquery.tables.create" in granted
    return {
        "status": "ok" if ok else "denied",
        "create_table": ok,
        "create_materialized_view": ok,
        "dest": f"{project}.{dataset}",
        "granted": granted,
    }
