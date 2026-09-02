"""FastAPI UI: one Events chart on the caller's warehouse."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import markdown
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from factcat.dialects import supports_json_value
from factcat.warehouses import (
    is_missing_relation,
    ADAPTERS,
    CAP_DRY_RUN,
    AdapterError,
    BytesCapError,
    capabilities,
    connect,
    extra_installed,
    extras_status,
)

from .catalog import (
    bootstrap_project,
    catalog_steps_by_kind,
    columns_from_form,
    datasets_from_form,
    roles_from_form,
    schemas_from_form,
    tables_from_form,
    type_sets,
    warehouses_from_form,
)
from .layout import (
    assemble_layout,
    write_access_from_form,
)
from .extras import extra_commands, install_command, run_install
from .config import load, mapping_ready, save, warehouse_kind
from .filters import filter_ui
from .sql_display import apply_sql_keyword_case, sql_chrome, sql_plain
from . import managed as managed_mod
from . import prefs as prefs_mod
from .query import (
    REPORTING_TIMEZONES,
    annotate_incomplete,
    catalog_event_values,
    catalog_lookback_days,
    connection_from_form,
    event_values_sql,
    ensure_epoch,
    events_sql_from_form,
    fill_cyclic_buckets,
    form_kind,
    infer_epoch_from_form,
    job_bytes_cap,
    query_row_limit,
    stored_event_name_cache,
)

APP_DIR = Path(__file__).resolve().parent


def _install_no_store(application: FastAPI) -> None:
    """HTML is rendered per request and must never be cached: a stale tab
    showing last week's template is the recurring local-app failure."""

    @application.middleware("http")
    async def no_store_html(request: Request, call_next):
        response = await call_next(request)
        if str(response.headers.get("content-type", "")).startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response
STATIC_DIR = APP_DIR / "static"
DOCS_DIR = APP_DIR / "guides"
SETUP_DOCS = {kind: DOCS_DIR / f"setup-{kind}.md" for kind in ADAPTERS}
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["sql_chrome"] = sql_chrome
templates.env.filters["sql_plain"] = sql_plain


def setup_docs_html(kind: str = "bigquery") -> str:
    """Packaged markdown for the Setup guide pane. Not fetched from GitHub."""
    path = SETUP_DOCS.get(kind, SETUP_DOCS["bigquery"])
    text = path.read_text(encoding="utf-8")
    return markdown.markdown(text, extensions=["fenced_code", "nl2br", "tables"])

app = FastAPI(title="Factcat")
_install_no_store(app)


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "logo.png", media_type="image/png")


def _page(request: Request, template: str, screen: str, cfg: dict) -> HTMLResponse:
    kind = warehouse_kind(cfg)
    types = type_sets(kind)
    user = prefs_mod.load()
    return templates.TemplateResponse(
        request,
        template,
        {
            "config": cfg,
            "prefs": user,
            "screen": screen,
            "entity_types": sorted(types["entity"]),
            "time_types": sorted(types["event_time"]),
            "event_name_types": sorted(types["event_column"]),
            "property_of_types": sorted(types["of"] - types["json"]),
            "distinct_of_types": sorted(types["of_distinct"] - types["json"]),
            "json_types": sorted(types["json"]),
            "capabilities": sorted(capabilities(kind)),
            "supports_json_value": supports_json_value(kind),
            "type_sets": {
                name: {role: sorted(vals) for role, vals in type_sets(name).items()}
                for name in ADAPTERS
            },
            "filter_ui": filter_ui(user),
            "mapping_ready": mapping_ready(cfg),
        },
    )


@app.get("/")
def index(request: Request):
    cfg = load()
    if not mapping_ready(cfg):
        return RedirectResponse("/setup?events=1", status_code=303)
    return _page(request, "index.html", "events", cfg)


@app.get("/events")
def events(request: Request) -> HTMLResponse:
    """Events report. Always the chart page — unlike ``/``, which sends a
    first run to Setup. Mapping can still be incomplete; Run says so.
    """
    return _page(request, "index.html", "events", load())


@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request) -> HTMLResponse:
    cfg = load()
    kind = warehouse_kind(cfg)
    if kind == "bigquery" and not cfg.get("project"):
        cfg["project"] = bootstrap_project()
    types = type_sets(kind)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "config": cfg,
            "prefs": prefs_mod.load(),
            "screen": "setup",
            "entity_types": sorted(types["entity"]),
            "time_types": sorted(types["event_time"]),
            "event_name_types": sorted(types["event_column"]),
            "setup_docs": setup_docs_html(kind),
            "setup_docs_by_kind": {
                name: setup_docs_html(name) for name in ADAPTERS
            },
            "reporting_timezones": REPORTING_TIMEZONES,
            "capabilities": sorted(capabilities(kind)),
            "capabilities_by_kind": {
                name: sorted(capabilities(name)) for name in ADAPTERS
            },
            "type_sets": {
                name: {role: sorted(vals) for role, vals in type_sets(name).items()}
                for name in ADAPTERS
            },
            "extras": extras_status(),
            "extra_commands": extra_commands(),
            "catalog_steps_by_kind": catalog_steps_by_kind(),
            "mapping_ready": mapping_ready(cfg),
        },
    )


def _catalog_error(exc: Exception, form: dict | None = None) -> JSONResponse:
    payload = {"ok": False, "error": str(exc)}
    if isinstance(exc, ImportError) and form is not None:
        kind = form_kind(form)
        if kind in ADAPTERS and not extra_installed(kind):
            payload["missing_extra"] = kind
            payload["command"] = install_command(kind)
    return JSONResponse(payload, status_code=400)


@app.post("/api/roles")
async def api_roles(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        roles = await run_in_threadpool(roles_from_form, form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    return JSONResponse({"ok": True, "roles": roles})


@app.post("/api/warehouses")
async def api_warehouses(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        payload = await run_in_threadpool(warehouses_from_form, form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    return JSONResponse({"ok": True, **payload})


@app.post("/api/datasets")
async def api_datasets(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        datasets = await run_in_threadpool(datasets_from_form, form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    return JSONResponse({"ok": True, "datasets": datasets})


@app.post("/api/schemas")
async def api_schemas(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        schemas = await run_in_threadpool(schemas_from_form, form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    return JSONResponse({"ok": True, "schemas": schemas})


@app.post("/api/tables")
async def api_tables(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        payload = await run_in_threadpool(tables_from_form, form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    return JSONResponse({"ok": True, **payload})


@app.post("/api/columns")
async def api_columns(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        payload = await run_in_threadpool(columns_from_form, form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    save({"columns": payload.get("columns") or []})
    return JSONResponse({"ok": True, **payload})


def _layout_force(form: dict[str, Any]) -> bool:
    raw = form.get("force")
    if raw is True or raw == 1:
        return True
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _want_layout_avg(form: dict[str, Any]) -> bool:
    raw = form.get("include_partition_avg")
    if raw is True or raw == 1:
        return True
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


@app.post("/api/layout")
async def api_layout(request: Request) -> JSONResponse:
    form = await request.json()
    stored = load().get("layout_cache") or {}
    force = _layout_force(form)
    want_avg = _want_layout_avg(form)
    try:
        def _run() -> tuple:
            return assemble_layout(
                form, stored, force=force, want_avg=want_avg
            )

        payload, store = await run_in_threadpool(_run)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    save({"layout_cache": store})
    return JSONResponse({"ok": True, **payload})


@app.post("/api/write_access")
async def api_write_access(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        payload = await run_in_threadpool(write_access_from_form, form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    if payload.get("status") in ("ok", "denied"):
        # The verdict is what gates an automatic index build on Run
        # (managed._write_ok); Setup re-checks whenever the destination moves.
        save({"write_access_status": payload["status"]})
    return JSONResponse({"ok": True, **payload})


@app.post("/api/infer_epoch")
async def api_infer_epoch(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        unit = await run_in_threadpool(infer_epoch_from_form, form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    if unit:
        save({"event_time_epoch": unit})
    return JSONResponse({"ok": True, "event_time_epoch": unit})


def _event_value_text(row: dict) -> str | None:
    raw = row.get("fc_value")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


@app.post("/api/event_values")
async def api_event_values(request: Request) -> JSONResponse:
    form = await request.json()
    sql = None
    cache_kind = None
    try:
        form = await run_in_threadpool(ensure_epoch, form)
        catalog = form.get("catalog") in (True, "true", "on", "1", 1)
        conn = connection_from_form(form, apply_scan_cap=not catalog)
        warehouse = await run_in_threadpool(connect, form_kind(form), **conn)
        if catalog:
            cfg = load()
            # The config file owns the cache fingerprint. The client's copy is
            # a mirror for status wording; an empty mirror (fresh page) must
            # not force a rebuild.
            if not stored_event_name_cache(form):
                form = {**form, "event_name_cache": cfg.get("event_name_cache") or {}}
            result, cache_kind, meta = await run_in_threadpool(
                catalog_event_values, form, warehouse.run
            )
        else:
            sql = event_values_sql(form)
            result = await run_in_threadpool(warehouse.run, sql)
            meta = None
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    seen: set[str] = set()
    for row in result.rows:
        text = _event_value_text(row)
        if text:
            seen.add(text)
    values = sorted(seen, key=str.lower)
    if catalog:
        days = catalog_lookback_days(form)
        saved = {
            "event_names": values,
            "catalog_lookback_days": 0 if days is None else days,
        }
        # The lookback fallback returns meta None. A degraded load must not
        # destroy the stored fingerprint - that wipe is what locked the page
        # into "Creating event-name cache…" on every visit.
        if meta is not None:
            saved["event_name_cache"] = meta
        if "write_project" in form:
            saved["write_project"] = (form.get("write_project") or "").strip()
        if "write_dataset" in form:
            saved["write_dataset"] = (form.get("write_dataset") or "").strip()
        if "write_schema" in form:
            saved["write_schema"] = (form.get("write_schema") or "").strip()
        if "write_database" in form:
            saved["write_database"] = (form.get("write_database") or "").strip()
        save(saved)
    payload = {"ok": True, "sql": sql, "values": values}
    if cache_kind:
        payload["cache"] = cache_kind
    if meta and meta.get("kind"):
        payload["kind"] = meta["kind"]
    if catalog and cache_kind is None and meta is None:
        dest_set = bool(
            (
                str(form.get("write_project") or "").strip()
                and str(form.get("write_dataset") or "").strip()
            )
            or (
                str(form.get("write_database") or "").strip()
                and str(form.get("write_schema") or "").strip()
            )
        )
        if dest_set:
            payload["fallback"] = "lookback"
    return JSONResponse(payload)


@app.get("/preferences", response_class=HTMLResponse)
def preferences(request: Request) -> HTMLResponse:
    user = prefs_mod.load()
    return templates.TemplateResponse(
        request,
        "preferences.html",
        {
            "config": load(),
            "prefs": user,
            "hour_styles": prefs_mod.HOUR_STYLE_GROUPS,
            "hour_previews": prefs_mod.hour_style_previews(),
            "hour_clock": prefs_mod.hour_clock_of_style(user["hour_style"]),
            "hour_clock_default": prefs_mod.HOUR_CLOCK_DEFAULT,
            "screen": "preferences",
            "mapping_ready": mapping_ready(),
        },
    )


@app.post("/api/save")
async def api_save(request: Request) -> JSONResponse:
    form = await request.json()
    save(form)
    return JSONResponse({"ok": True})


@app.post("/api/preferences")
async def api_preferences(request: Request) -> JSONResponse:
    form = await request.json()
    if not isinstance(form, dict):
        return JSONResponse(
            {"ok": False, "error": "preferences must be a JSON object"},
            status_code=400,
        )
    try:
        prefs_mod.save(form)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "prefs": prefs_mod.load()})


@app.post("/api/install_extra")
async def api_install_extra(request: Request) -> JSONResponse:
    form = await request.json()
    kind = form.get("kind") if isinstance(form, dict) else None
    if not isinstance(kind, str) or kind not in ADAPTERS:
        return JSONResponse(
            {"ok": False, "error": "unknown warehouse extra"},
            status_code=400,
        )
    result = await run_in_threadpool(run_install, kind)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


# Job SQL in warehouse exceptions (indented, line-numbered, or the
# google-cloud "Query Job SQL Follows" trailer). Table pane must not
# show this; the compiled job already lives in the SQL pane.
_SQL_DUMP = re.compile(
    r"-----Query Job SQL Follows-----"
    r"|(?:^|\n)\s*(?:\d+\s*:)?\s*(?:SELECT|WITH)\b"
    r"|SELECT\s+\*\s+FROM",
    re.IGNORECASE,
)
_SQL_ONLY = re.compile(r"^\s*(?:\d+\s*:)?\s*(?:SELECT|WITH)\b", re.IGNORECASE)


def _client_error(exc: BaseException, sql: str | None = None) -> str:
    """Warehouse errors must not dump the job SQL into the table pane."""
    text = str(exc).replace("\r\n", "\n").strip() or "Query failed."
    dump = _SQL_DUMP.search(text)
    if dump:
        prefix = text[: dump.start()].strip()
        text = prefix or "Query failed. See SQL below."
    if "googleapis.com" in text and ": " in text:
        text = text.split(": ", 1)[1].strip()
        dump = _SQL_DUMP.search(text)
        if dump:
            prefix = text[: dump.start()].strip()
            text = prefix or "Query failed. See SQL below."
    if sql:
        compact_err = re.sub(r"\s+", " ", text).strip()
        compact_sql = re.sub(r"\s+", " ", sql)
        if compact_err and compact_err in compact_sql:
            return "Query failed. See SQL below."
    if _SQL_ONLY.match(text) or _SQL_DUMP.search(text):
        return "Query failed. See SQL below."
    lines = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(("location:", "job id:", "job_id:")):
            break
        lines.append(stripped)
        if len(lines) >= 6:
            break
    return "\n".join(lines) if lines else "Query failed. See SQL below."


MANAGED_KEYS = (
    "managed_tables",
    "managed_last_sweep",
    "write_access_status",
    "managed_mode",
    "managed_drop_days",
    "managed_refresh_days",
    "managed_lookback_days",
)


def _with_managed(form: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Events requests carry the chart, not the bookkeeping: the config file
    owns the registry mirror, the sweep clock and the Setup knobs. Merge
    them in for any key the request did not send (Setup's own form does)."""
    out = dict(form)
    for key in MANAGED_KEYS:
        if key not in out or out[key] in (None, ""):
            out[key] = cfg.get(key)
    return out


def _fail(
    exc: BaseException,
    sql: str | None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body = {"ok": False, "error": _client_error(exc, sql), "sql": sql}
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=400)


def _cap_from_error(exc: BytesCapError, form: dict[str, Any]) -> int | None:
    """The cap a rejection was measured against, or the one we asked for."""
    if exc.maximum_bytes_billed is not None:
        return exc.maximum_bytes_billed
    return job_bytes_cap(form)


def _estimate_payload(processed: int | None, cap: int | None, *, over_cap: bool) -> dict:
    return {
        "ok": True,
        "bytes": processed,
        "cap": cap,
        "over_cap": over_cap
        or (
            processed is not None
            and cap is not None
            and processed > cap
        ),
    }


@app.post("/api/sql")
async def api_sql(request: Request) -> JSONResponse:
    """Compile Events SQL from the form. No warehouse round-trip."""
    form = await request.json()
    try:
        form = await run_in_threadpool(ensure_epoch, form)
        sql = events_sql_from_form(form)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    prefs = prefs_mod.load()
    if prefs.get("vocab") == "sql" and prefs.get("sql_case") == "lower":
        sql = apply_sql_keyword_case(sql, form_kind(form), "lower")
    return JSONResponse({"ok": True, "sql": sql})


@app.post("/api/estimate")
async def api_estimate(request: Request) -> JSONResponse:
    form = await request.json()
    sql = None
    try:
        form = await run_in_threadpool(ensure_epoch, form)
        kind = form_kind(form)
        caps = capabilities(kind)
        if CAP_DRY_RUN not in caps:
            return JSONResponse(
                {
                    "ok": True,
                    "bytes": None,
                    "cap": None,
                    "over_cap": False,
                    "supported": False,
                }
            )
        conn = connection_from_form(form)
        form = _with_managed(form, load())
        # An estimate is a free dry run: the plan reads only cached facts
        # (no probe, no bookmark query) and nothing is written.
        plan = managed_mod.build_plan(form, None)
        sql = events_sql_from_form(form, managed=plan)
        warehouse = await run_in_threadpool(connect, kind, **conn)
        result = await run_in_threadpool(warehouse.run, sql, dry_run=True)
        build_bytes = None
        if plan.builds():
            try:
                build_bytes = 0
                for cp in plan.builds():
                    stmt = managed_mod.backfill_select_sql(
                        form, cp.column.key, cp.column.expr
                    )
                    probe = await run_in_threadpool(warehouse.run, stmt, dry_run=True)
                    build_bytes += int(probe.bytes_processed or 0)
            except AdapterError:
                # The chart is still estimable; the build just goes unpriced.
                build_bytes = None
    except BytesCapError as exc:
        return JSONResponse(
            _estimate_payload(
                exc.bytes_processed,
                _cap_from_error(exc, form),
                over_cap=True,
            )
        )
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _fail(exc, sql)
    query_bytes = result.bytes_processed
    total = query_bytes
    if build_bytes is not None and query_bytes is not None:
        total = query_bytes + build_bytes
    payload = _estimate_payload(
        total, conn.get("maximum_bytes_billed"), over_cap=False
    )
    if plan.builds():
        # Only the running copy needs this ("Indexing x… then running"); the
        # chip already includes the build, so there is no line before the run.
        payload["managed_build"] = [cp.column.label for cp in plan.builds()]
    return JSONResponse(payload)


@app.post("/api/run")
async def api_run(request: Request) -> JSONResponse:
    form = await request.json()
    sql = None
    managed_note = ""
    managed_failed = ""
    try:
        form = await run_in_threadpool(ensure_epoch, form)
        conn = connection_from_form(form)
        cfg = load()
        save(form)
        # The config file owns the registry mirror, the sweep clock and the
        # Setup knobs; the request carries the chart, not the bookkeeping.
        form = _with_managed(form, cfg)
        warehouse = await run_in_threadpool(connect, form_kind(form), **conn)
        extra_save: dict[str, Any] = {}
        if managed_mod.index_table(form) is not None:
            if managed_mod.registry_from_form(form).get("columns"):
                # The table's own description is the authority: a dropped
                # table or a new destination must not inherit the mirror's
                # bookmarks - and the sweep below must plan from it too, or a
                # workstation that has not charted for the TTL would drop a
                # column another one uses every day.
                reconciled = await run_in_threadpool(managed_mod.reconcile_registry, form)
                form = {**form, "managed_tables": reconciled}
                extra_save["managed_tables"] = reconciled
            registry, _dropped, ran = await run_in_threadpool(
                managed_mod.sweep, form, warehouse.run
            )
            if ran:
                form = {**form, "managed_tables": registry}
                extra_save["managed_tables"] = registry
                extra_save["managed_last_sweep"] = managed_mod.now_iso()
        plan = await run_in_threadpool(
            managed_mod.build_plan,
            form,
            warehouse.run,
            allow_probe=True,
        )
        if plan.builds():
            registry = await run_in_threadpool(managed_mod.apply_plan, plan, form, warehouse.run)
            extra_save["managed_tables"] = registry
            managed_failed = managed_mod.failure_note(plan)
        sql = events_sql_from_form(form, managed=plan)
        fell_back = False
        try:
            result = await run_in_threadpool(warehouse.run, sql)
        except AdapterError as exc:
            if not (plan.attachable() and is_missing_relation(exc)):
                raise
            # The prepared table vanished between the plan and the query:
            # answer from the full history, forget the stale bookmarks, and
            # say only that — no usage bump, no "prepared" note.
            fell_back = True
            for cp in plan.columns:
                cp.action = "live"
            sql = events_sql_from_form(form)
            result = await run_in_threadpool(warehouse.run, sql)
            managed_note = ""
            managed_failed = (
                "The indexed table was not found, so this run read the full "
                "history; the chart is correct. It is rebuilt on the next run."
            )
            extra_save["managed_tables"] = {"probes": plan.registry.get("probes") or {}}
        if not fell_back:
            if plan.built:
                # The one line after a run that indexed: what later runs cost,
                # from a free dry run of the query that just ran (the index
                # now exists, so this is the real figure), where there is one.
                after = None
                if CAP_DRY_RUN in capabilities(form_kind(form)):
                    try:
                        priced = await run_in_threadpool(warehouse.run, sql, dry_run=True)
                        after = priced.bytes_processed
                    except AdapterError:
                        # BytesCapError is an AdapterError: a cap rejection here
                        # (the cap moved between run and price) only costs the
                        # figure, never the rows already fetched.
                        after = None
                managed_note = managed_mod.built_note(plan, bytes_after=after)
            registry = await run_in_threadpool(managed_mod.bump_usage, plan, form, warehouse.run)
            if plan.attachable():
                extra_save["managed_tables"] = registry
        if extra_save:
            save(extra_save)
    except BytesCapError as exc:
        # Distinguishable from any other 400 so the report can offer the
        # override instead of showing a raw warehouse rejection.
        return _fail(
            exc,
            sql,
            {
                "over_cap": True,
                "bytes": exc.bytes_processed,
                "cap": _cap_from_error(exc, form),
            },
        )
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _fail(exc, sql)
    raw_rows = []
    for r in result.rows:
        item = {str(k): v for k, v in r.items()}
        item["bucket"] = str(r.get("bucket", ""))
        item["value"] = r.get("value")
        raw_rows.append(item)
    rows = fill_cyclic_buckets(annotate_incomplete(raw_rows, form), form)
    limit = query_row_limit(form)
    body: dict[str, Any] = {
        "ok": True,
        "sql": sql,
        "rows": rows,
        "truncated": len(rows) >= limit,
        "limit": limit,
    }
    if managed_failed:
        body["managed_failed"] = managed_failed
    if managed_note:
        body["managed_note"] = managed_note
    return JSONResponse(body)


@app.post("/api/managed")
async def api_managed(request: Request) -> JSONResponse:
    """The Managed tables section: settings, rows, sizes. Metadata calls
    only; the registry mirror in the config file is refreshed from the
    warehouse copy, which is the authority."""
    form = await request.json()
    cfg = load()
    form = {**cfg, **form}
    try:
        payload = await run_in_threadpool(managed_mod.list_payload, form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc, form)
    if payload.get("registry") is not None:
        save({"managed_tables": payload["registry"]})
    return JSONResponse({"ok": True, **payload})


@app.post("/api/managed/action")
async def api_managed_action(request: Request) -> JSONResponse:
    """Drop one indexed column (and the table when it was the last one)."""
    form = await request.json()
    cfg = load()
    action = str(form.get("action") or "").strip().lower()
    merged = {**cfg, **{k: v for k, v in form.items() if k not in ("action", "key")}}
    try:
        conn = connection_from_form(merged)
        warehouse = await run_in_threadpool(connect, form_kind(merged), **conn)
        registry = await run_in_threadpool(
            managed_mod.apply_action, merged, warehouse.run, action=action, key=str(form.get("key") or "")
        )
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        if isinstance(exc, ImportError):
            return _catalog_error(exc, merged)
        return JSONResponse({"ok": False, "error": f"Drop did not run: {exc}"}, status_code=400)
    save({"managed_tables": registry})
    return JSONResponse({"ok": True, "registry": registry})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
