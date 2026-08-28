"""FastAPI UI: one Events chart on the caller's BigQuery."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from factcat import events_sql
from factcat.warehouses import AdapterError, connect

from .catalog import (
    ENTITY_TYPES,
    EVENT_NAME_TYPES,
    TIME_TYPES,
    bootstrap_project,
    columns_from_form,
    datasets_from_form,
    tables_from_form,
)
from .config import load, save
from .query import connection_from_form, spec_from_form

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="Factcat")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    cfg = load()
    if not cfg.get("project"):
        cfg["project"] = bootstrap_project()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "config": cfg,
            "entity_types": sorted(ENTITY_TYPES),
            "time_types": sorted(TIME_TYPES),
            "event_name_types": sorted(EVENT_NAME_TYPES),
        },
    )


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


@app.post("/api/save")
async def api_save(request: Request) -> JSONResponse:
    form = await request.json()
    save(form)
    return JSONResponse({"ok": True})


@app.post("/api/run")
async def api_run(request: Request) -> JSONResponse:
    form = await request.json()
    try:
        spec = spec_from_form(form)
        conn = connection_from_form(form)
        save(form)
        sql = events_sql(spec, dialect="bigquery")
        warehouse = connect("bigquery", **conn)
        result = warehouse.run(sql)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    rows = [{"bucket": str(r.get("bucket", "")), "value": r.get("value")} for r in result.rows]
    return JSONResponse({"ok": True, "sql": sql, "rows": rows})
