"""FastAPI UI: one Events chart on the caller's BigQuery."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from factcat.warehouses import AdapterError, BytesCapError, connect

from .catalog import (
    ENTITY_TYPES,
    EVENT_NAME_TYPES,
    TIME_TYPES,
    bootstrap_project,
    columns_from_form,
    datasets_from_form,
    tables_from_form,
)
from .config import load, mapping_ready, save
from .query import (
    annotate_incomplete,
    connection_from_form,
    event_values_sql,
    events_sql_from_form,
    job_bytes_cap,
    query_row_limit,
)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="Factcat")


def _page(request: Request, template: str, screen: str, cfg: dict) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {
            "config": cfg,
            "screen": screen,
            "entity_types": sorted(ENTITY_TYPES),
            "time_types": sorted(TIME_TYPES),
            "event_name_types": sorted(EVENT_NAME_TYPES),
        },
    )


@app.get("/")
def index(request: Request):
    cfg = load()
    if not mapping_ready(cfg):
        return RedirectResponse("/setup", status_code=303)
    return _page(request, "index.html", "events", cfg)


@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request) -> HTMLResponse:
    cfg = load()
    if not cfg.get("project"):
        cfg["project"] = bootstrap_project()
    return _page(request, "setup.html", "setup", cfg)


def _catalog_error(exc: Exception) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/datasets")
async def api_datasets(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        datasets = datasets_from_form(form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc)
    return JSONResponse({"ok": True, "datasets": datasets})


@app.post("/api/tables")
async def api_tables(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        payload = tables_from_form(form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc)
    return JSONResponse({"ok": True, **payload})


@app.post("/api/columns")
async def api_columns(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        payload = columns_from_form(form)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc)
    return JSONResponse({"ok": True, **payload})


def _event_value_text(row: dict) -> str | None:
    raw = row.get("fc_value")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


@app.post("/api/event_values")
async def api_event_values(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        sql = event_values_sql(form)
        conn = connection_from_form(form)
        warehouse = connect("bigquery", **conn)
        result = warehouse.run(sql)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _catalog_error(exc)
    seen: set[str] = set()
    for row in result.rows:
        text = _event_value_text(row)
        if text:
            seen.add(text)
    values = sorted(seen, key=str.lower)
    if form.get("catalog") in (True, "true", "on", "1", 1):
        save({"event_names": values})
    return JSONResponse({"ok": True, "sql": sql, "values": values})


@app.post("/api/save")
async def api_save(request: Request) -> JSONResponse:
    form = await request.json()
    save(form)
    return JSONResponse({"ok": True})


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


@app.post("/api/estimate")
async def api_estimate(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        conn = connection_from_form(form)
        sql = events_sql_from_form(form)
        warehouse = connect("bigquery", **conn)
        result = warehouse.run(sql, dry_run=True)
    except BytesCapError as exc:
        return JSONResponse(
            _estimate_payload(
                exc.bytes_processed,
                exc.maximum_bytes_billed if exc.maximum_bytes_billed is not None else job_bytes_cap(form),
                over_cap=True,
            )
        )
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        _estimate_payload(
            result.bytes_processed, conn.get("maximum_bytes_billed"), over_cap=False
        )
    )


@app.post("/api/run")
async def api_run(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        conn = connection_from_form(form)
        save(form)
        sql = events_sql_from_form(form)
        warehouse = connect("bigquery", **conn)
        result = warehouse.run(sql)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    raw_rows = []
    for r in result.rows:
        item = {str(k): v for k, v in r.items()}
        item["bucket"] = str(r.get("bucket", ""))
        item["value"] = r.get("value")
        raw_rows.append(item)
    rows = annotate_incomplete(raw_rows, form)
    limit = query_row_limit(form)
    return JSONResponse({
        "ok": True,
        "sql": sql,
        "rows": rows,
        "truncated": len(rows) >= limit,
        "limit": limit,
    })
