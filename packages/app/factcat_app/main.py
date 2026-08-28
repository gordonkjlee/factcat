"""FastAPI UI: one Events chart on the caller's BigQuery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from factcat import events_sql
from factcat.warehouses import connect

from .config import load, save
from .query import connection_from_form, spec_from_form

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="Factcat")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"config": load()})


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
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    rows = [{"bucket": str(r.get("bucket", "")), "value": r.get("value")} for r in result.rows]
    return JSONResponse({"ok": True, "sql": sql, "rows": rows})
