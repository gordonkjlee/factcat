"""FastAPI UI: one Events chart on the caller's BigQuery."""

from __future__ import annotations

import re
from pathlib import Path

import markdown
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from factcat.warehouses import AdapterError, BytesCapError, connect

from .catalog import (
    DISTINCT_OF_TYPES,
    ENTITY_TYPES,
    EVENT_NAME_TYPES,
    JSON_TYPES,
    PROPERTY_OF_TYPES,
    TIME_TYPES,
    bootstrap_project,
    columns_from_form,
    datasets_from_form,
    tables_from_form,
)
from .config import load, mapping_ready, save
from .query import (
    REPORTING_TIMEZONES,
    annotate_incomplete,
    connection_from_form,
    event_values_sql,
    events_sql_from_form,
    job_bytes_cap,
    query_row_limit,
)

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DOCS_DIR = APP_DIR / "guides"
SETUP_DOCS = {
    "bigquery": DOCS_DIR / "setup-bigquery.md",
}
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def setup_docs_html(kind: str = "bigquery") -> str:
    """Packaged markdown for the Setup guide pane. Not fetched from GitHub."""
    path = SETUP_DOCS.get(kind, SETUP_DOCS["bigquery"])
    text = path.read_text(encoding="utf-8")
    return markdown.markdown(text, extensions=["fenced_code", "nl2br", "tables"])

app = FastAPI(title="Factcat")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "logo.png", media_type="image/png")


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
            "property_of_types": sorted(PROPERTY_OF_TYPES),
            "distinct_of_types": sorted(DISTINCT_OF_TYPES),
            "json_types": sorted(JSON_TYPES),
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
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "config": cfg,
            "screen": "setup",
            "entity_types": sorted(ENTITY_TYPES),
            "time_types": sorted(TIME_TYPES),
            "event_name_types": sorted(EVENT_NAME_TYPES),
            "setup_docs": setup_docs_html("bigquery"),
            "reporting_timezones": REPORTING_TIMEZONES,
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
    save({"columns": payload.get("columns") or []})
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


def _fail(exc: BaseException, sql: str | None) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": _client_error(exc, sql), "sql": sql},
        status_code=400,
    )


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
        sql = events_sql_from_form(form)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "sql": sql})


@app.post("/api/estimate")
async def api_estimate(request: Request) -> JSONResponse:
    form = await request.json()
    sql = None
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
        return _fail(exc, sql)
    return JSONResponse(
        _estimate_payload(
            result.bytes_processed, conn.get("maximum_bytes_billed"), over_cap=False
        )
    )


@app.post("/api/run")
async def api_run(request: Request) -> JSONResponse:
    form = await request.json()
    sql = None
    try:
        sql = events_sql_from_form(form)
        conn = connection_from_form(form)
        save(form)
        warehouse = connect("bigquery", **conn)
        result = warehouse.run(sql)
    except BytesCapError as exc:
        return _fail(exc, sql)
    except (ValueError, AdapterError, LookupError, ImportError) as exc:
        return _fail(exc, sql)
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


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
