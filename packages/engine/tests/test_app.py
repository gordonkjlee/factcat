"""HTTP surface. Warehouse is mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from factcat.warehouses import AdapterError, BytesCapError, QueryResult
from factcat_app.catalog import column_fits
from factcat_app.config import mapping_ready
from factcat_app.main import APP_DIR, _client_error, app


def _map_cfg(tmp_path, monkeypatch, **extra):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    data = {
        "project": "adc-project",
        "location": "EU",
        "table": "analytics.events",
        "dataset": "analytics",
        "table_name": "events",
        "entity": "account_id",
        "event_time": "occurred_at",
    }
    data.update(extra)
    (tmp_path / "cfg.json").write_text(json.dumps(data), encoding="utf-8")


def test_unmapped_root_redirects_to_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/setup?events=1"


def test_events_tab_loads_when_unmapped(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    res = client.get("/events")
    assert res.status_code == 200
    assert "<h1>Events</h1>" in res.text
    assert 'href="/events"' in res.text
    assert 'id="needs-setup"' in res.text
    assert "Map a table on" in res.text
    assert 'href="/setup"' in res.text
    assert 'id="run" disabled' in res.text
    assert "rail-setup needs-mapping" in res.text
    assert "mapping required" in res.text.lower()
    assert 'id="setup-needed"' not in res.text


def test_snowflake_events_prompts_setup_when_unmapped(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(json.dumps({"kind": "snowflake"}), encoding="utf-8")
    client = TestClient(app)
    res = client.get("/events")
    assert res.status_code == 200
    assert 'id="needs-setup"' in res.text
    assert 'id="run" disabled' in res.text
    assert "rail-setup needs-mapping" in res.text
    assert "BigQuery" not in res.text.split('id="needs-setup"', 1)[1].split("</div>", 1)[0]


def test_setup_renders(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    res = client.get("/setup")
    assert res.status_code == 200
    assert "Project setup" in res.text
    assert "setup-docs" in res.text
    assert "setup-cols" in res.text
    assert "wide events table" in res.text
    assert "Reporting timezone" in res.text
    assert "Timestamp stored as" in res.text
    assert "Entity id" in res.text
    assert 'id="kind"' in res.text
    assert ">Snowflake<" in res.text
    assert 'id="private_key_path"' in res.text
    assert 'id="database"' in res.text
    assert '"endpoint": "schemas"' in res.text
    assert '"endpoint": "roles"' in res.text
    assert '"endpoint": "warehouses"' in res.text
    assert '"/api/" + step.endpoint' in res.text
    assert 'id="role-loading"' in res.text
    assert 'id="warehouse-loading"' in res.text
    assert 'name="dataset" hidden' not in res.text
    assert 'name="database" hidden' not in res.text
    assert 'name="schema" hidden' not in res.text
    assert 'name="table_name" hidden' not in res.text
    assert "Listed after sign-in" not in res.text
    assert "no compute warehouse needed" not in res.text
    assert 'name="dataset" disabled' in res.text
    assert 'name="database" disabled' in res.text
    assert "CATALOG =" in res.text
    assert "bindCatalog" in res.text
    assert "This grain is called" not in res.text
    assert "Entity name (singular)" not in res.text
    assert ">Entity name<" in res.text
    assert "entity_label_other_wrap" in res.text
    assert "layout-checking" in res.text
    assert "Plural" in res.text
    assert "Event name column" in res.text
    assert "Week starts on" in res.text
    assert "Formatting" not in res.text
    assert "Thousand separator" not in res.text
    assert "Decimal separator" not in res.text
    assert 'id="thousand_sep"' not in res.text
    assert 'id="decimal_sep"' not in res.text
    assert "Limits" in res.text
    assert 'href="/preferences"' in res.text
    assert "rail-foot" in res.text
    assert ">Other<" in res.text
    assert 'value="User"' in res.text
    assert "Volume" not in res.text
    assert 'value="user_id"' not in res.text
    assert "/path/to/key.json" not in res.text
    assert "Load datasets" not in res.text
    assert "form.dataset" not in res.text
    assert "Select a dataset" not in res.text
    assert "Select a table" not in res.text
    assert 'getElementById("dataset")' in res.text
    assert 'id="dataset-loading"' in res.text
    assert 'aria-label="Loading"' in res.text
    assert 'value="adc-project"' in res.text
    assert 'href="/setup"' in res.text
    assert "rail-setup" in res.text
    assert "if (catalogProject()) loadDatasets()" not in res.text
    assert "catalogArmed" in res.text
    assert "catalogArmed && ready" in res.text
    assert "function paintStep" in res.text
    assert "persistCatalogLists" in res.text
    assert "catalog-refresh" in res.text
    assert "linkish catalog-refresh" in res.text
    assert "!!loading[step.id]" in res.text
    assert "function invalidateStep" in res.text
    assert "invalidateStep(step.id)" in res.text
    assert 'addEventListener("mousedown"' in res.text
    assert ">Analysis<" not in res.text
    assert "Job scan cap" in res.text
    assert 'id="bytes_cap_gb"' in res.text
    assert "Look back for event names" in res.text
    assert "Event name lookback" not in res.text
    assert 'id="catalog_lookback_days"' in res.text
    assert 'id="catalog-lookback-wrap"' in res.text
    assert "syncManagedChrome" in res.text
    assert "Factcat-managed tables (recommended)" in res.text
    assert "Factcat-managed tables (optional)" not in res.text
    assert 'id="table-layout-hint"' in res.text
    assert 'id="table-cluster-layout"' in res.text
    assert 'id="event-column-checking" class="layout-checking" hidden>Checking whether event-name filters prune…' in res.text
    assert 'id="entity-checking" class="layout-checking" hidden>Checking whether entity filters prune…' in res.text
    assert "Checking clustering…" not in res.text
    assert "Queries still run; entity filters won't prune." in res.text
    assert 'id="event-time-checking" class="layout-checking" hidden>Checking whether date filters prune…' in res.text
    assert "dateBusy && hasTime" in res.text
    assert "clusterBusy && hasEvent" in res.text
    assert "clusterBusy && hasEntity" in res.text
    assert "Pruning is effective on event-name filters" in res.text
    assert "applyLayoutCache" in res.text
    assert "layout_cache" in res.text
    assert "hasCap" in res.text
    assert "CAPABILITIES_BY_KIND" in res.text
    assert "Mapping still saves." in res.text
    assert "Checking clustering on the tables this view reads" not in res.text
    assert "paintClusterPending" not in res.text
    assert 'id="event-column-layout"' in res.text
    assert 'id="entity-layout"' in res.text
    assert "include_partition_avg" in res.text
    assert 'id="event-time-check"' in res.text
    assert 'id="event_time_storage_tz"' in res.text
    assert "event_time_tz_choice" not in res.text
    assert "Wall-clock UTC" not in res.text
    assert "/api/layout" in res.text
    assert "/api/infer_epoch" in res.text
    assert "Unix epoch unit" not in res.text
    assert "/api/write_access" in res.text
    assert 'id="write_project"' in res.text
    assert 'id="write_dataset"' in res.text
    assert 'id="write_database"' in res.text
    assert 'id="write_schema"' in res.text
    assert "Result row limit" in res.text
    assert 'id="query_row_limit"' in res.text
    assert "Save and open Events" not in res.text
    assert "Events opens after this mapping is saved." not in res.text
    assert "Pick the events table and map entity id and timestamp" not in res.text
    assert "Warehouse sign-in does not fill those fields" not in res.text
    assert "Catalog lists load when you open each dropdown" not in res.text
    assert 'id="save"' not in res.text
    assert ">Save<" not in res.text
    assert "scheduleSave" in res.text
    assert "hasOwnProperty.call(body, \"event_column\")" in res.text
    assert "save-toast" in res.text
    assert "showToast" in res.text
    assert "/static/save.js" in res.text
    assert "fcAutosave" in res.text
    assert "async function post(" not in res.text
    assert "/api/save" in res.text
    assert "window.location.href" not in res.text
    assert "Saved" in res.text
    assert "Could not cache event names" not in res.text
    assert "post(\"/api/event_values\"" not in res.text
    assert 'id="setup-needed"' not in res.text
    assert "rail-setup" in res.text
    assert "gcloud config get-value project" in res.text


def test_setup_seeds_write_destination(monkeypatch, tmp_path):
    _map_cfg(
        tmp_path,
        monkeypatch,
        write_project="dest-proj",
        write_dataset="fc_cache",
    )
    client = TestClient(app)
    res = client.get("/setup")
    assert res.status_code == 200
    assert 'value="dest-proj"' in res.text
    assert 'value="fc_cache" selected' in res.text
    assert "seedSelect(document.getElementById(\"write_dataset\")" in res.text


def test_setup_explains_events_redirect(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    res = client.get("/setup?events=1")
    assert res.status_code == 200
    assert "Project setup" in res.text
    assert "Events opens after this mapping is saved." not in res.text


def test_events_renders_when_mapped(monkeypatch, tmp_path):
    _map_cfg(tmp_path, monkeypatch)
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "Volume" in res.text
    assert "Unique Users" in res.text
    assert "Unique User<" not in res.text
    assert "Average per User" in res.text
    assert ">Uniques<" not in res.text
    assert 'optgroup label="Event"' in res.text
    assert 'optgroup label="Property value"' in res.text
    assert 'optgroup label="Property"' not in res.text
    assert ">Sum<" in res.text
    assert 'value="property_average"' in res.text
    assert ">Median<" in res.text
    assert "Distinct per User" in res.text
    assert 'id="of_column"' in res.text
    assert 'id="of_json_key"' in res.text
    assert 'id="breakdown_json_key"' in res.text
    assert "JSON_TYPES" in res.text
    assert 'id="of-wrap"' in res.text
    assert "Pick a column." in res.text
    assert "Only this event" not in res.text
    assert "Event name column" not in res.text
    assert "Date range" in res.text
    assert "Time grain" in res.text
    assert "writeDestReady" in res.text
    assert "function writeDestReady" in res.text
    assert "Day of week" in res.text
    assert "Hour of day" in res.text
    assert "Last 24 hours" in res.text
    assert "results-toolbar" in res.text
    assert "<label>Bucket</label>" not in res.text
    assert "Last 30 days" in res.text
    assert "Last 8 weeks" in res.text
    assert "Last 6 months" in res.text
    assert "Last 3 quarters" in res.text
    assert "last:6:month" in res.text
    assert "last:3:quarter" in res.text
    assert "this:quarter" in res.text
    assert "Include this quarter" in res.text
    assert "rangeIncludeEl.hidden = cyclic" not in res.text
    assert "This week" in res.text
    assert "Last week" in res.text
    assert "Yesterday" in res.text
    assert 'label: "Custom"' in res.text
    assert "Specific dates" in res.text
    assert ">Relative<" in res.text
    assert "event_names: data.values" not in res.text
    assert "Include today" in res.text
    assert 'day: "today"' in res.text
    assert 'week: "this week"' in res.text
    assert 'month: "this month"' in res.text
    assert "RANGE_PRESETS" in res.text
    assert 'id="range_choice"' in res.text
    assert "applyRangeChoice" in res.text
    assert "seedRangeChoice" in res.text
    assert 'value="this">This<' not in res.text
    assert 'value="previous">Previous<' not in res.text
    assert "Exclude current period" not in res.text
    html = res.text
    grain_at = html.find('id="grain"')
    range_at = html.find('id="range_choice"')
    measure_at = html.find('id="measure"')
    of_at = html.find('id="of_column"')
    assert grain_at != -1 and range_at != -1 and grain_at < range_at
    assert measure_at != -1 and of_at != -1
    assert measure_at < of_at < grain_at
    assert "bindCachedList" in html
    assert "cachedEventNames" in html
    assert "fillSelect(eventValueEl, cachedEventNames" not in html
    assert 'id="reporting_timezone"' in html
    assert 'id="event_time_tz"' in html
    assert 'id="event_time_epoch"' in html
    assert "syncChartTitle" not in html
    assert "Exact unique counts" in res.text
    assert 'v === "uniques" || v === "average"' in res.text
    assert "Break down by" in res.text
    assert "Add breakdown" in res.text
    assert "BREAKDOWN_SLOT_CAP = 3" in res.text
    assert "Show (other)" in res.text
    assert "SQL expression" in res.text
    assert "sql_expr_ellipsis" in res.text
    # The Value at / If missing / Fill from controls replaced the hidden
    # breakdown_at input and its "later control" hint.
    assert 'id="breakdown_at"' not in res.text
    assert "Value at" in res.text
    assert "If missing" in res.text
    assert "Fill from" in res.text
    assert "in this date range" not in res.text
    assert "folds the rest into (other)" in res.text
    assert "(null) stays its own group" in res.text
    # The ever anchors hide If missing entirely (fields that do not
    # apply are hidden, not disabled-with-a-note).
    assert "bd-ever-note" not in res.text
    assert "All history is" not in res.text
    assert "bd-value-pair" in res.text
    assert '"bd_at_first": "first ever"' in res.text
    assert '"bd_at_latest": "latest ever"' in res.text
    assert '"bd_fill_charted": "(charted events)"' in res.text
    assert '"bd_fill_any": "(any event)"' in res.text
    # tojson escapes the apostrophe ('); pin up to it.
    assert '"bd_fill_series": "(this series' in res.text
    # Section panels: static heads, one surface; Break down head folds.
    assert 'class="cfg-section" id="events-band"' in res.text
    assert 'class="cfg-section" id="breakdown-band"' in res.text
    assert '"bd_section": "Break down"' in res.text
    # The optgroup label key exists for both vocabs (the sql painter reads
    # the live mapped column and falls back to this key when unmapped).
    assert '"bd_fill_group": "Event names"' in res.text
    assert "__charted__" in res.text
    assert "__series__" in res.text
    assert "IGNORE NULLS" not in res.text
    assert 'id="bd-null-nudge"' in res.text
    assert "Use last known at the event" in res.text
    assert "Exact (off = approx unique count)" not in res.text
    assert 'id="copy-sql"' in res.text
    assert 'id="copy-chart"' in res.text
    assert 'id="waiting-cat"' in res.text
    assert "setChartEmpty" in res.text
    assert 'id="copy-table"' in res.text
    assert "tableErrorText" in res.text
    assert "factcat-error.txt" in res.text
    assert "Copy error" in res.text
    assert "grainHeader" in res.text
    assert "valueHeader" in res.text
    assert "snapshotLastRun" in res.text
    assert "formDrifted" in res.text
    assert "resultKey" in res.text
    assert 'id="table-stale"' in res.text
    assert 'id="chart-stale"' in res.text
    assert "Showing the last run. Run again to apply form changes." in res.text
    assert "draftGrainHeader" in res.text
    assert "syncTableHeaders();" in html
    assert 'form.measure.addEventListener("change", () => {\n  syncExact();\n  syncOfUi();\n  syncTableHeaders();\n});' not in html.replace("\r\n", "\n")
    assert ">Bucket<" not in res.text
    assert 'id="chart-export"' in res.text
    assert 'id="table-export"' in res.text
    assert 'id="sql-export"' in res.text
    assert 'data-chart-export="png2xt"' in res.text
    assert 'data-table-export="tsv"' in res.text
    assert 'data-table-export="md"' in res.text
    assert 'data-table-export="json"' in res.text
    assert 'data-sql-export="sql"' in res.text
    assert 'data-sql-export="md"' in res.text
    assert 'id="export-png"' not in res.text
    assert 'id="export-csv"' not in res.text
    assert "exportSlug" in res.text
    assert ".pane-actions button.icon-btn," in res.text
    assert ".pane-actions .format-pop > summary.icon-btn {" in res.text
    assert ".pane-actions details { margin: 0; }" in res.text
    assert ".row-divider[hidden] + .pane { margin-top: 0.5rem; }" in res.text
    assert "flex: 0 0 0.5rem" not in res.text
    assert 'id="copy-chart" class="icon-btn" disabled' in res.text
    assert 'id="copy-table" class="icon-btn" disabled' in res.text
    assert 'id="copy-sql" class="icon-btn" disabled' in res.text
    assert res.text.count('class="format-pop disabled"') == 3
    assert "setExportEnabled" in res.text
    assert res.headers.get("cache-control") == "no-store"
    assert 'id="chart_type"' in res.text
    assert "Labels" in res.text
    assert 'id="chart-title"' in res.text
    assert 'id="reset-title"' in res.text
    assert "Reset title" in res.text
    assert "ofTitlePart" in res.text
    assert "measureTitle" in res.text
    assert 'label + " " + of' in res.text
    assert 'label.replace(/^Distinct per /, "Distinct " + of + " per ")' in res.text
    assert 'value: "this:week"' in res.text
    assert ">CSV<" in res.text
    assert "All events" not in res.text
    assert "Pick an event." in res.text
    assert "Running…" in res.text
    assert 'id="col-divider"' in res.text
    assert 'id="config-toggle"' in res.text
    assert 'id="row-divider"' in res.text
    assert res.text.count("pane-toggle") >= 3
    assert 'class="pane pane-sql closed"' in res.text
    assert 'class="pane pane-chart closed"' not in res.text
    assert 'class="pane pane-table closed"' not in res.text
    assert "setPaneOpen" in res.text
    assert "bindDivider" in res.text
    assert "position: sticky" in res.text
    assert "col-flyout" in res.text
    assert 'id="config-collapse"' in res.text
    assert 'id="config-default"' in res.text
    assert 'id="config-wide"' in res.text
    assert "Drag to resize" not in res.text
    assert "loadOnStart" not in res.text
    assert 'id="event-names-fallback"' in res.text
    assert "setConfigCollapsed" in res.text
    assert "layout_config_px" in res.text
    assert "layout_chart_px" in res.text
    assert "startRunButtonDots" in res.text
    assert "cancelEstimate();" in res.text
    assert "Querying…" not in res.text
    assert 'id="run-loading"' not in res.text
    assert "chart_axis_x" not in res.text
    assert "chart_axis_y" not in res.text
    assert "X-axis labels" not in res.text
    assert "Y-axis labels" not in res.text
    assert "Dotted point is the current" not in res.text
    assert "Faded bar is the current" not in res.text
    assert '" — partial, " + through' in res.text
    assert "Few points at this grain." in res.text
    assert "setRunningCopy" in res.text
    assert "stepRunDots" in res.text
    assert 'dots.className = "run-dots"' in res.text
    assert 'dots.setAttribute("aria-hidden", "true")' in res.text
    assert ".run-dots span { visibility: hidden; }" in res.text
    assert 'emptyCopy.textContent === "Running…"' not in res.text
    assert 'catKind === "running"' in res.text
    assert 'id="run"' in res.text
    assert 'id="run-estimate"' in res.text
    assert "/api/estimate" in res.text
    assert ">Area<" in res.text
    assert "Format chart" in res.text
    assert "2 decimals" in res.text
    assert "Scientific" in res.text
    assert "Major and minor" in res.text
    assert "n <= 2 ? \"bar\" : \"line\"" in res.text
    assert 'id="thousand_sep"' in res.text
    assert "applySeps" in res.text
    assert "chartFurniture" in res.text
    assert "const furn = chartFurniture()" in res.text
    assert 'cssToken("--fc-ochre"' not in res.text
    palette = res.text.split("const SERIES_COLORS = [")[1].split("];")[0]
    assert palette.count("#") >= 20
    assert palette.strip().startswith('"#C4841D"')
    assert '"#0072B2"' in res.text
    assert '"#009E73"' in res.text
    assert '"#EE8866"' in res.text
    assert '"#3D6F8A"' in res.text
    assert '"#8A8178"' in res.text
    assert '"#7c3aed"' not in res.text
    assert 'name === "(other)" ? MUTED' in res.text
    assert "labels: { color: furn.ink }" in res.text
    assert "border: { color: furn.line }" in res.text
    assert "Show last" not in res.text
    assert "limit-note" in res.text
    assert "Load more" in res.text
    assert 'id="query_row_limit"' in res.text
    assert "Override cap" in res.text
    assert "bytes_cap_override_gb" not in res.text
    assert "GB this run" not in res.text
    assert "icon-btn" in res.text
    assert "estimateKey" in res.text
    assert "syncCostGate" in res.text
    # The row count belongs to the table it counts, not the toolbar.
    assert 'id="table-count"' in res.text
    assert 'id="run-status"' not in res.text
    assert html.find('<h2>Table</h2>') < html.find('id="table-count"')
    # Toggling the override must not cost a dry run: bytes are cap-independent,
    # so the override is not part of the estimate fingerprint.
    assert "reverdictFromLastEstimate" in res.text
    # A failed run must not render in the same slate as the idle copy.
    assert "#chart-empty.fail #empty-copy" in res.text
    # Cost -> consent -> action: Run is last so it stays flush right.
    assert html.find('id="run-estimate-wrap"') < html.find('id="cap-override"') < html.find('id="run"')
    assert 'e.target.id === "exact"' in res.text
    assert 'id="needs-setup"' not in res.text
    assert 'id="run" disabled' not in res.text
    assert "rail-setup needs-mapping" not in res.text
    assert "class=\"sort\"" in res.text or 'className = "sort"' in res.text
    assert html.find('id="exact-wrap"') < html.find('id="exact-hint"') < html.find('id="run"')
    assert "Exact series labels" in res.text
    assert "hasBreakdown" in res.text
    assert "approx top-K" in res.text
    assert "Creating event-name cache" in res.text
    assert "Loading event names seen in the last " in res.text
    assert "Loading event names from all time" in res.text
    assert "This list is a snapshot" in res.text
    assert 'id="refresh-events"' in res.text
    assert ">Refresh event names</button>" in res.text
    assert "Refresh list of events" not in res.text
    assert "Use the arrow to look further back" in res.text
    assert 'id="catalog_lookback_days"' in res.text
    assert 'id="look-further"' not in res.text
    assert "Not listed?" not in res.text
    assert "Can't find an event?" not in res.text
    assert "refresh-more" in res.text
    assert "Look further back for event names" in res.text
    assert "Look back 6 months" in res.text
    assert "Look back 12 months" in res.text
    assert 'id="find-event-panel"' in res.text
    assert "catalog-lookback" not in res.text
    assert 'id="refresh-of"' in res.text
    assert 'id="refresh-columns"' in res.text
    assert "/static/catalog.js" in res.text
    assert "/static/save.js" in res.text
    assert "async function post(" not in res.text
    assert "/api/event_values" in res.text
    assert "<h1>Project setup</h1>" not in res.text
    assert "GCP project that runs" not in res.text
    assert "account_id" in res.text
    assert 'value="user_id"' not in res.text
    assert "pane-chart" in res.text
    assert "pane-table" in res.text
    assert "pane-sql" in res.text
    assert "The cat is waiting." in res.text
    assert "Run to plot." not in res.text
    assert "Run to fill the table." in res.text
    assert "SQL updates as you change the form." in res.text
    assert "/api/sql" in res.text
    assert "<summary>SQL</summary>" not in res.text
    assert 'id="event-cards"' in res.text
    assert "Event series" in res.text
    assert "Add series" in res.text
    assert 'id="add-event-row"' in res.text
    assert "syncSeriesChrome" in res.text
    assert "series-measure" in res.text
    assert "event-series" in res.text
    assert "attachSeriesMeasure" in res.text
    assert "aria-label\", \"Measure\"" in res.text
    assert "event group" not in res.text.lower()
    assert 'id="add-event"' in res.text
    assert "event-card-combine" in res.text
    assert "fc-plus-row" in res.text
    assert "fc-plus" in res.text
    assert "event-group-split" in res.text
    assert "Break down each series" in res.text
    assert "collectSeries" in res.text
    assert "Any of" in res.text
    assert "is any of" in res.text
    assert "starts with" in res.text
    assert "filter-part" in res.text
    assert "Day of week (e.g. Monday)" in res.text
    assert "Start of" in res.text
    assert "Extract" in res.text
    assert "filter-month" in res.text
    assert "filter-week-n" in res.text
    assert "filter-quarter-q" in res.text
    assert "2026 week 21" in res.text
    assert "Hour of day" in res.text
    assert "Day of month (1-31)" in res.text
    assert "Month of year (e.g. May)" in res.text
    assert "Month (e.g. May 2026)" in res.text
    assert "Choose a filter" in res.text
    assert "Year (e.g. 2026)" in res.text
    assert "between (inclusive)" in res.text
    assert "numeric_op_labels" in res.text
    assert "less than" in res.text
    assert "Hour (e.g. 18 May 2026, 14:00)" in res.text
    assert "Day of year (1-366)" in res.text
    assert "Year number" not in res.text
    assert "filter-pills" in res.text
    assert "Add a value" in res.text
    assert "Case sensitive" in res.text
    assert "FILTER_UI" in res.text
    assert "event-card-name" in res.text
    assert "form.entity.value, eventColumnEl.value" in res.text


def test_events_serves_cached_event_names(monkeypatch, tmp_path):
    _map_cfg(
        tmp_path,
        monkeypatch,
        event_column="event_name",
        event_names=["opened", "paid"],
        event_value="paid",
    )
    client = TestClient(app)
    html = client.get("/").text
    assert "cachedEventNames" in html
    assert '"opened"' in html
    assert '"paid"' in html
    assert 'id="event_value-wrap" hidden' not in html
    assert "cachedEventNames" in html
    assert "bindCachedList" in html
    assert "All events" not in html


def test_events_serves_cached_columns_and_breakdown_selection(monkeypatch, tmp_path):
    _map_cfg(
        tmp_path,
        monkeypatch,
        breakdown_column="course_code",
        columns=[
            {"name": "course_code", "type": "STRING"},
            {"name": "country", "type": "STRING"},
        ],
    )
    client = TestClient(app)
    html = client.get("/").text
    assert "cachedColumns" in html
    assert '"course_code"' in html
    assert "paintColumns" in html
    assert "ensureOption" in html
    assert 'id="refresh-of"' in html
    assert 'id="refresh-columns"' in html


def test_run_builds_spec_and_calls_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[{"bucket": "2026-01-05", "value": 2}]
    )
    captured: dict = {}

    def fake_connect(kind, **kw):
        captured["kind"] = kind
        captured.update(kw)
        return warehouse

    monkeypatch.setattr("factcat_app.main.connect", fake_connect)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
            "exact": False,
            "credentials": "/tmp/sa.json",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["rows"] == [{"bucket": "2026-01-05", "value": 2, "incomplete": False}]
    assert body["truncated"] is False
    assert body["limit"] == 1_000_000
    warehouse.run.assert_called_once()
    sql = warehouse.run.call_args.args[0]
    compact = " ".join(sql.split()).upper()
    assert "LIMIT 1000000" in compact
    assert "-- safety cap, not meant to be hit" in sql
    assert "ORDER BY CAST(BUCKET AS DATE) DESC" in compact
    assert "account_id" in sql
    assert "user_id" not in sql
    assert "DATE(CAST(fc_event_ts AS TIMESTAMP), 'UTC')" in sql.replace("`", "")
    assert "CURRENT_DATE('UTC')" in sql


def test_sql_endpoint_compiles_without_warehouse(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/sql",
        json={
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "sum",
            "of_column": "revenue",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "SUM" in body["sql"].upper()
    assert "revenue" in body["sql"]


def test_sql_endpoint_lowercases_keywords_for_sql_vocab(monkeypatch, tmp_path):
    from factcat_app.prefs import save as save_prefs

    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    save_prefs({"vocab": "sql", "sql_case": "lower"})
    client = TestClient(app)
    res = client.post(
        "/api/sql",
        json={
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 200
    sql = res.json()["sql"]
    assert "select" in sql
    assert "from" in sql
    assert "SELECT" not in sql
    assert "account_id" in sql


def test_sql_endpoint_compiles_event_in_and_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/sql",
        json={
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "event_column": "event_name",
            "event_values": ["started", "completed"],
            "filters": [
                {"column": "country", "op": "is", "value": "UK"},
                {"join": "OR", "column": "country", "op": "is", "value": "IE"},
            ],
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "event_name IN ('started', 'completed')" in body["sql"]
    assert "country = 'UK'" in body["sql"]
    assert "country = 'IE'" in body["sql"]


def test_sql_endpoint_overlays_ungrouped_cards(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/sql",
        json={
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "event_column": "event_name",
            "series": [{"event": "started"}, {"event": "completed"}],
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "UNION ALL" in body["sql"]
    assert "'started' AS series" in body["sql"]
    assert "'completed' AS series" in body["sql"]


def test_run_returns_sql_when_warehouse_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.side_effect = AdapterError("Syntax error: unexpected keyword")
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 400
    body = res.json()
    assert body["ok"] is False
    assert "Syntax error" in body["error"]
    assert not body["error"].lstrip().upper().startswith("SELECT")
    assert body["sql"]
    assert "account_id" in body["sql"]
    assert "DATE(CAST(fc_event_ts AS TIMESTAMP), 'UTC')" in body["sql"].replace("`", "")


def test_client_error_drops_indented_select_star():
    assert _client_error(AdapterError("  SELECT * FROM (")) == (
        "Query failed. See SQL below."
    )
    assert "SELECT" not in _client_error(
        AdapterError("Unrecognized name: revenue at [8:3]\n  SELECT * FROM (\n  SELECT 1\n)")
    )
    numbered = _client_error(
        AdapterError("Syntax error at [1:1]\n   1:SELECT * FROM (\n   2:  SELECT 1")
    )
    assert "SELECT" not in numbered
    assert "Syntax error" in numbered
    assert _client_error(
        AdapterError("SELECT * FROM (\n  SELECT 1\n)"),
        sql="SELECT * FROM (\n  SELECT 1\n)",
    ) == "Query failed. See SQL below."


def test_run_strips_sql_dump_from_warehouse_error(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.side_effect = AdapterError(
        "Unrecognized name: revenue at [8:3]\n  SELECT * FROM (\n  SELECT 1\n)"
    )
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
        },
    )
    body = res.json()
    assert body["ok"] is False
    assert "Unrecognized name: revenue" in body["error"]
    assert "SELECT" not in body["error"]
    assert body["sql"]
    assert "SELECT" in body["sql"].upper()


def test_run_property_sum_uses_of_column(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[{"bucket": "2026-01-05", "value": 12.5}]
    )
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "sum",
            "of_column": "revenue",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 200
    sql = warehouse.run.call_args.args[0].upper()
    assert "SUM(" in sql.replace(" ", "") or "SUM(" in sql
    assert "REVENUE" in sql
    assert "APPROX_COUNT_DISTINCT" not in sql


def test_run_passes_breakdown_series(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[{"bucket": "2026-01-05", "country": "US", "value": 2}]
    )
    monkeypatch.setattr(
        "factcat_app.main.connect",
        lambda kind, **kw: warehouse,
    )
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "total",
            "grain": "day",
            "lookback_days": 30,
            "breakdown_column": "country",
            "breakdown_at": "rows",
            "top_n": 8,
            "include_other": True,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["rows"][0]["country"] == "US"
    sql = warehouse.run.call_args.args[0]
    assert "country" in sql
    assert "(other)" in sql


def test_run_passes_breakdown_value_semantics(monkeypatch, tmp_path):
    """A slot with Value at + If missing + Fill from reaches the engine:
    the generated SQL carries the carried-stream CTEs and the stamp
    predicate, and never IGNORE NULLS."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[{"bucket": "2026-01-05", "plan": "pro", "value": 2}]
    )
    monkeypatch.setattr(
        "factcat_app.main.connect",
        lambda kind, **kw: warehouse,
    )
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "event_column": "event_name",
            "event_values": ["login"],
            "measure": "total",
            "grain": "day",
            "lookback_days": 30,
            "breakdowns": [
                {
                    "breakdown_column": "plan",
                    "value_at": "event",
                    "if_missing": "fill",
                    "fill_from_event": "subscription_started",
                }
            ],
            "top_n": 8,
            "include_other": True,
        },
    )
    assert res.status_code == 200
    sql = warehouse.run.call_args.args[0]
    assert "fc_values" in sql
    assert "subscription_started" in sql
    assert "IGNORE NULLS" not in sql.upper()


def test_estimate_is_dry_run(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    captured: dict = {}

    def fake_run(sql, *, dry_run=False):
        captured["sql"] = sql
        captured["dry_run"] = dry_run
        return QueryResult(rows=[{"bucket": "x", "value": 1}], bytes_processed=1500)

    warehouse.run.side_effect = fake_run
    monkeypatch.setattr(
        "factcat_app.main.connect", lambda kind, **kw: warehouse
    )
    client = TestClient(app)
    res = client.post(
        "/api/estimate",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["bytes"] == 1500
    assert body["over_cap"] is False
    assert captured["dry_run"] is True
    assert "account_id" in captured["sql"]


def test_estimate_over_cap_still_returns_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.side_effect = BytesCapError(
        "too big",
        bytes_processed=20 * 1024**3,
        maximum_bytes_billed=10 * 1024**3,
    )
    monkeypatch.setattr(
        "factcat_app.main.connect", lambda kind, **kw: warehouse
    )
    client = TestClient(app)
    res = client.post(
        "/api/estimate",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["over_cap"] is True
    assert body["bytes"] == 20 * 1024**3


def _cap_rejected_run(monkeypatch, tmp_path, exc):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.side_effect = exc
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    return TestClient(app).post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
        },
    )


def test_run_cap_rejection_is_distinguishable(monkeypatch, tmp_path):
    res = _cap_rejected_run(
        monkeypatch,
        tmp_path,
        BytesCapError(
            "too big",
            bytes_processed=39277559808,
            maximum_bytes_billed=10 * 1024**3,
        ),
    )
    assert res.status_code == 400
    body = res.json()
    assert body["ok"] is False
    assert body["over_cap"] is True
    assert body["bytes"] == 39277559808
    assert body["cap"] == 10 * 1024**3


def test_run_cap_rejection_without_figures_still_flags(monkeypatch, tmp_path):
    """No figures in the message: the flag survives, the cap falls back."""
    res = _cap_rejected_run(monkeypatch, tmp_path, BytesCapError("too big"))
    body = res.json()
    assert body["over_cap"] is True
    assert body["bytes"] is None
    assert body["cap"] == 10 * 1024**3


def test_run_other_failures_are_not_flagged_over_cap(monkeypatch, tmp_path):
    res = _cap_rejected_run(monkeypatch, tmp_path, AdapterError("syntax error"))
    body = res.json()
    assert body["ok"] is False
    assert "over_cap" not in body


def test_run_flags_truncated_when_row_count_hits_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[
            {"bucket": "2026-01-01", "value": 1},
            {"bucket": "2026-01-02", "value": 2},
            {"bucket": "2026-01-03", "value": 3},
        ]
    )
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "query_row_limit": 3,
        },
    )
    body = res.json()
    assert body["ok"] is True
    assert body["truncated"] is True
    assert body["limit"] == 3
    sql = warehouse.run.call_args.args[0]
    assert "LIMIT 3" in " ".join(sql.split()).upper()


def test_run_limit_override_does_not_persist_to_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=[{"bucket": "2026-01-01", "value": 1}])
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "query_row_limit": 5000,
            "query_row_limit_run": 10000,
        },
    )
    body = res.json()
    assert body["ok"] is True
    assert body["limit"] == 10000
    sql = warehouse.run.call_args.args[0]
    assert "LIMIT 10000" in " ".join(sql.split()).upper()
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert saved["query_row_limit"] == 5000
    assert "query_row_limit_run" not in saved


def test_run_rejects_empty_entity(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 400
    assert "entity" in res.json()["error"].lower()


def test_save_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/save",
        json={"project": "acme", "location": "EU", "entity": "account_id"},
    )
    assert res.status_code == 200
    html = client.get("/").text
    assert "acme" in html
    assert "account_id" in html


def test_blank_credentials_omitted_from_connect(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=[])
    captured: dict = {}

    def fake_connect(kind, **kw):
        captured.update(kw)
        captured["kind"] = kind
        return warehouse

    monkeypatch.setattr("factcat_app.main.connect", fake_connect)
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
            "credentials": "",
        },
    )
    assert res.status_code == 200
    assert captured["kind"] == "bigquery"
    assert "credentials" not in captured


def test_run_rejects_lookback_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 0,
        },
    )
    assert res.status_code == 400
    assert "lookback" in res.json()["error"].lower()


def test_run_rejects_missing_project(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/run",
        json={
            "project": "",
            "location": "EU",
            "table": "analytics.events",
            "entity": "account_id",
            "event_time": "occurred_at",
            "measure": "uniques",
            "grain": "day",
            "lookback_days": 30,
        },
    )
    assert res.status_code == 400
    assert "project" in res.json()["error"].lower()


def test_save_drops_unknown_keys_and_nulls(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    client.post(
        "/api/save",
        json={
            "project": "acme",
            "location": "EU",
            "credentials": None,
            "extra": "drop-me",
        },
    )
    html = client.get("/").text
    assert "acme" in html
    assert "drop-me" not in html
    assert 'value="None"' not in html


def test_template_ships_with_package():
    templates = Path(APP_DIR) / "templates"
    assert (templates / "index.html").is_file()
    assert (templates / "setup.html").is_file()
    assert (templates / "base.html").is_file()
    assert (templates / "preferences.html").is_file()
    static = Path(APP_DIR) / "static"
    assert (static / "logo.png").is_file()
    assert (static / "catalog.js").is_file()
    catalog_js = (static / "catalog.js").read_text(encoding="utf-8")
    assert "bindCachedList" in catalog_js
    assert 'classList.toggle("refresh-busy", on)' in catalog_js
    assert (static / "save.js").is_file()
    save_js = (static / "save.js").read_text(encoding="utf-8")
    assert "async function post(" in save_js
    assert "function showToast(" in save_js
    assert "function fcAutosave(" in save_js
    assert "SAVE_PENDING_MS = 1000" in save_js
    assert "savingSince" in save_js
    assert not (static / "logo.svg").exists()
    assert not (static / "favicon.ico").exists()
    assert (static / "waiting.jpg").is_file()
    assert (static / "waiting-blink.jpg").is_file()
    assert (static / "waiting-glance.jpg").is_file()
    assert (static / "waiting-glance-mid.jpg").is_file()
    assert (static / "empty-sniff.jpg").is_file()
    assert (static / "unimpressed.jpg").is_file()
    assert (static / "settled.jpg").is_file()
    assert (static / "running-reach.jpg").is_file()
    assert (static / "running-mid.jpg").is_file()
    assert (static / "running-almost.jpg").is_file()
    assert (static / "running-contact.jpg").is_file()
    assert (static / "cat-business.jpg").is_file()
    assert (static / "cat-analyst.jpg").is_file()
    assert (Path(APP_DIR) / "guides" / "setup-bigquery.md").is_file()
    assert (Path(APP_DIR) / "guides" / "setup-snowflake.md").is_file()


def test_favicon_and_mark_are_served(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    ico = client.get("/favicon.ico")
    assert ico.status_code == 200
    assert ico.content[:8] == b"\x89PNG\r\n\x1a\n"
    mark = client.get("/static/logo.png")
    assert mark.status_code == 200
    assert mark.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/static/logo.svg").status_code == 404
    catalog = client.get("/static/catalog.js")
    assert catalog.status_code == 200
    assert b"bindCachedList" in catalog.content
    save_js = client.get("/static/save.js")
    assert save_js.status_code == 200
    assert b"fcAutosave" in save_js.content


def test_chrome_uses_tokens_and_empty_state(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    setup = client.get("/setup").text
    assert "--fc-ochre: #C4841D" in setup
    assert 'html[data-theme="dark"]' in setup
    assert "/static/logo.png" in setup
    assert 'class="cog"' in setup
    assert ".rail-item.active" in setup
    assert "background: var(--fc-ochre)" in setup
    assert "purrfect" not in setup.lower()
    _map_cfg(tmp_path, monkeypatch)
    events = client.get("/").text
    assert "--fc-ochre: #C4841D" in events
    assert 'html[data-theme="dark"]' in events
    assert "/static/logo.png" in events
    assert 'class="cog"' in events
    assert "/static/waiting.jpg" in events
    assert "/static/waiting-blink.jpg" in events
    assert "/static/waiting-glance.jpg" in events
    assert "/static/empty-sniff.jpg" in events
    assert "/static/settled.jpg" in events
    assert "/static/unimpressed.jpg" in events
    assert "prefers-reduced-motion" in events
    assert "animation-duration: 1.2s" in events
    assert ".refresh-busy { display: none !important; }" in events
    assert "startRunCat" in events
    assert "startIdleCat" in events
    assert "img.hidden = false" in events
    assert "kind !== \"wait\" && kind !== \"empty\"" not in events
    assert "The cat is waiting." in events
    assert "purrfect" not in events.lower()
    assert "Fc</a>" not in events


CHART_POSES = (
    "waiting.jpg",
    "waiting-blink.jpg",
    "waiting-glance.jpg",
    "waiting-glance-mid.jpg",
    "empty-sniff.jpg",
    "unimpressed.jpg",
    "settled.jpg",
    "running-reach.jpg",
    "running-mid.jpg",
    "running-almost.jpg",
    "running-contact.jpg",
)


def test_chart_poses_are_served(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    static = Path(APP_DIR) / "static"
    for name in CHART_POSES:
        path = static / name
        assert path.is_file(), name
        res = client.get("/static/" + name)
        assert res.status_code == 200, name
        assert res.content[:2] == b"\xff\xd8"


def test_brand_pack_has_chart_pose_masters():
    root = Path(APP_DIR).resolve().parents[2]
    brand = root / "brand" / "mascot"
    assert (root / "brand" / "README.md").is_file()
    for name in CHART_POSES:
        assert (brand / name).is_file(), name


def test_readme_keeps_slogan_and_points_at_the_mark():
    readme = Path(APP_DIR).resolve().parents[2] / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "An open-source alternative to Amplitude and Mixpanel" in text
    assert "packages/engine/factcat_app/static/waiting.jpg" in text
    assert "one wide events table" in text
    assert "setup-bigquery.md" in text
    assert "setup-snowflake.md" in text
    assert "factcat[snowflake]" in text
    assert "Preferences" in text
    assert "~/.factcat/preferences.json" in text


def test_setup_fills_project_from_gcloud_when_adc_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.catalog.adc_quota_project", lambda: "")
    monkeypatch.setattr("factcat_app.catalog.gcloud_config_project", lambda: "cli-project")
    client = TestClient(app)
    res = client.get("/setup")
    assert res.status_code == 200
    assert 'value="cli-project"' in res.text


def test_snowflake_setup_does_not_fill_gcp_project(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(json.dumps({"kind": "snowflake"}), encoding="utf-8")
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    res = client.get("/setup")
    assert res.status_code == 200
    assert 'value="adc-project"' not in res.text
    assert 'id="setup-needed"' not in res.text
    assert "rail-setup active needs-mapping" in res.text


def test_bootstrap_project_prefers_adc_then_gcloud(monkeypatch):
    from factcat_app.catalog import bootstrap_project

    monkeypatch.setattr("factcat_app.catalog.adc_quota_project", lambda: "adc-project")
    monkeypatch.setattr("factcat_app.catalog.gcloud_config_project", lambda: "cli-project")
    assert bootstrap_project() == "adc-project"
    monkeypatch.setattr("factcat_app.catalog.adc_quota_project", lambda: "")
    assert bootstrap_project() == "cli-project"
    monkeypatch.setattr("factcat_app.catalog.gcloud_config_project", lambda: "")
    assert bootstrap_project() == ""


def test_mapped_setup_hides_needed_callout(monkeypatch, tmp_path):
    _map_cfg(tmp_path, monkeypatch)
    client = TestClient(app)
    res = client.get("/setup")
    assert res.status_code == 200
    assert 'id="setup-needed"' not in res.text
    assert "rail-setup active needs-mapping" not in res.text
    assert "rail-setup active" in res.text
    assert 'value="analytics" selected' in res.text
    assert 'value="events" selected' in res.text
    assert 'value="account_id" selected' in res.text
    assert 'value="occurred_at" selected' in res.text


def test_mapping_ready_requires_table_entity_and_time():
    base = {
        "project": "p",
        "location": "EU",
        "table": "analytics.events",
        "entity": "account_id",
        "event_time": "occurred_at",
    }
    assert mapping_ready(base)
    for key in ("table", "entity", "event_time"):
        missing = dict(base)
        missing[key] = ""
        assert not mapping_ready(missing)


def test_mapping_ready_snowflake_does_not_need_gcp_project():
    base = {
        "kind": "snowflake",
        "account": "xy12345",
        "user": "ANALYST",
        "warehouse": "COMPUTE_WH",
        "database": "ANALYTICS",
        "schema": "MARTS",
        "private_key_path": "rsa_key.p8",
        "table": "ANALYTICS.MARTS.EVENTS",
        "entity": "account_id",
        "event_time": "occurred_at",
    }
    assert mapping_ready(base)
    assert not mapping_ready({**base, "project": "", "location": "", "account": ""})
    browser = dict(base)
    browser["snowflake_auth"] = "externalbrowser"
    browser["private_key_path"] = ""
    assert mapping_ready(browser)


def test_estimate_snowflake_does_not_execute(monkeypatch, tmp_path):
    _map_cfg(
        tmp_path,
        monkeypatch,
        kind="snowflake",
        account="xy12345",
        user="ANALYST",
        warehouse="COMPUTE_WH",
        database="ANALYTICS",
        schema="MARTS",
        private_key_path="rsa_key.p8",
        table="ANALYTICS.MARTS.EVENTS",
    )
    ran = {"n": 0}

    def boom(*_a, **_k):
        ran["n"] += 1
        raise AssertionError("must not connect")

    monkeypatch.setattr("factcat_app.main.connect", boom)
    client = TestClient(app)
    res = client.post(
        "/api/estimate",
        json={
            "kind": "snowflake",
            "table": "ANALYTICS.MARTS.EVENTS",
            "entity": "account_id",
            "event_time": "occurred_at",
            "account": "xy12345",
            "user": "ANALYST",
            "warehouse": "COMPUTE_WH",
            "database": "ANALYTICS",
            "schema": "MARTS",
            "private_key_path": "rsa_key.p8",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["supported"] is False
    assert body["bytes"] is None
    assert ran["n"] == 0


def test_save_overlays_without_resetting_lookback(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    client.post(
        "/api/save",
        json={"project": "acme", "lookback_days": 90, "measure": "total"},
    )
    client.post("/api/save", json={"location": "EU"})
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert saved["project"] == "acme"
    assert saved["location"] == "EU"
    assert saved["lookback_days"] == 90
    assert saved["measure"] == "total"


def test_snowflake_roles_and_warehouses_from_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.catalog.sf_list_roles",
        lambda **kw: ["ANALYST", "PUBLIC"],
    )
    monkeypatch.setattr(
        "factcat_app.catalog.sf_list_warehouses",
        lambda **kw: {"warehouses": ["COMPUTE_WH", "LOAD_WH"], "default": "COMPUTE_WH"},
    )
    client = TestClient(app)
    roles = client.post(
        "/api/roles",
        json={"kind": "snowflake", "account": "xy", "user": "ANALYST"},
    )
    assert roles.status_code == 200
    assert roles.json()["roles"] == ["ANALYST", "PUBLIC"]
    houses = client.post(
        "/api/warehouses",
        json={"kind": "snowflake", "account": "xy", "user": "ANALYST", "role": "ANALYST"},
    )
    assert houses.status_code == 200
    assert houses.json()["default"] == "COMPUTE_WH"
    assert "COMPUTE_WH" in houses.json()["warehouses"]
    bq = client.post("/api/roles", json={"kind": "bigquery", "project": "p"})
    assert bq.status_code == 400


def test_datasets_lists_from_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.catalog.list_datasets",
        lambda **kw: [{"id": "3_entity"}, {"id": "analytics"}],
    )
    client = TestClient(app)
    res = client.post("/api/datasets", json={"project": "p"})
    assert res.status_code == 200
    assert res.json()["datasets"][0]["id"] == "3_entity"


def test_tables_return_location(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.catalog.list_tables",
        lambda **kw: {"location": "EU", "tables": ["customer_events"]},
    )
    client = TestClient(app)
    res = client.post(
        "/api/tables", json={"project": "p", "dataset": "3_entity"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["location"] == "EU"
    assert body["tables"] == ["customer_events"] or any(
        (t == "customer_events" or (isinstance(t, dict) and t.get("id") == "customer_events"))
        for t in body["tables"]
    )


def test_layout_endpoint_returns_typed_facts(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.layout._partition_avg", lambda *a, **k: 3 * 1024**3
    )
    monkeypatch.setattr(
        "factcat_app.layout.columns_from_form",
        lambda form: {
            "columns": [{"name": "occurred_at", "type": "TIMESTAMP"}],
            "relation": {
                "name": "events",
                "kind": "table",
                "partition": {
                    "field": "occurred_at",
                    "type": "DAY",
                    "ingestion": False,
                },
                "clustering": ["event_name", "user_id"],
                "require_partition_filter": False,
            },
        },
    )
    client = TestClient(app)
    res = client.post(
        "/api/layout",
        json={
            "kind": "bigquery",
            "project": "p",
            "location": "EU",
            "dataset": "analytics",
            "table_name": "events",
            "table": "p.analytics.events",
            "event_time": "occurred_at",
            "event_column": "event_name",
            "entity": "user_id",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["metadata_verdict"] == "match"
    assert body["grain"] == "DAY"
    assert body["cluster"]["status"] == "ok"
    assert body["cluster"]["entity_pos"] == 2
    assert body["partition_avg_bytes"] is None
    res_avg = client.post(
        "/api/layout",
        json={
            "kind": "bigquery",
            "project": "p",
            "location": "EU",
            "dataset": "analytics",
            "table_name": "events",
            "table": "p.analytics.events",
            "event_time": "occurred_at",
            "event_column": "event_name",
            "entity": "user_id",
            "include_partition_avg": True,
        },
    )
    assert res_avg.json()["partition_avg_bytes"] == 3 * 1024**3


def test_layout_endpoint_reuses_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    calls = {"n": 0}

    def columns(form):
        calls["n"] += 1
        return {
            "columns": [{"name": "occurred_at", "type": "TIMESTAMP"}],
            "relation": {
                "name": "events",
                "kind": "table",
                "partition": {
                    "field": "occurred_at",
                    "type": "DAY",
                    "ingestion": False,
                },
                "clustering": ["event_name"],
                "require_partition_filter": False,
            },
        }

    monkeypatch.setattr("factcat_app.layout.columns_from_form", columns)
    monkeypatch.setattr("factcat_app.layout._partition_avg", lambda *a, **k: None)
    client = TestClient(app)
    body = {
        "kind": "bigquery",
        "project": "p",
        "location": "EU",
        "dataset": "analytics",
        "table_name": "events",
        "table": "p.analytics.events",
        "event_time": "occurred_at",
        "event_column": "event_name",
        "entity": "user_id",
    }
    assert client.post("/api/layout", json=body).json()["ok"] is True
    assert calls["n"] == 1
    assert client.post("/api/layout", json=body).json()["ok"] is True
    assert calls["n"] == 1
    assert client.post("/api/layout", json={**body, "force": True}).json()["ok"] is True
    assert calls["n"] == 2
    assert client.post("/api/layout", json={**body, "entity": "other_id"}).json()["ok"] is True
    assert calls["n"] == 2
    assert client.post("/api/layout", json={**body, "event_time": "created_at"}).json()["ok"] is True
    assert calls["n"] == 2
    assert client.post(
        "/api/layout",
        json={**body, "table_name": "other", "table": "p.analytics.other"},
    ).json()["ok"] is True
    assert calls["n"] == 3


def test_write_access_skipped_when_dest_blank(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post("/api/write_access", json={"kind": "bigquery", "project": "p"})
    assert res.status_code == 200
    assert res.json()["status"] == "skipped"


def test_columns_are_sorted_alphabetically(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.catalog.list_columns",
        lambda **kw: {
            "location": "EU",
            "columns": [
                {"name": "zeta", "type": "STRING"},
                {"name": "alpha", "type": "STRING"},
            ],
        },
    )
    client = TestClient(app)
    res = client.post(
        "/api/columns",
        json={"project": "p", "dataset": "analytics", "table_name": "events"},
    )
    names = [c["name"] for c in res.json()["columns"]]
    assert names == ["alpha", "zeta"]
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert [c["name"] for c in saved["columns"]] == ["alpha", "zeta"]


def test_event_values_run_distinct_and_sort(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[
            {"fc_value": "paid"},
            {"fc_value": None},
            {"fc_value": "opened"},
            {"fc_value": "  "},
        ]
    )
    captured: dict = {}

    def fake_connect(kind, **kw):
        captured["kind"] = kind
        captured.update(kw)
        return warehouse

    monkeypatch.setattr("factcat_app.main.connect", fake_connect)
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "event_column": "event_name",
            "event_time": "occurred_at",
            "lookback_days": 7,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["values"] == ["opened", "paid"]
    sql = warehouse.run.call_args.args[0].upper()
    assert "DISTINCT" in sql
    assert "ORDER BY" in sql
    assert captured["kind"] == "bigquery"
    assert captured["project"] == "p"
    assert captured["location"] == "EU"
    assert captured["maximum_bytes_billed"] == 10 * 1024**3


def test_catalog_event_values_writes_event_names(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(
        json.dumps({"event_names": ["stale"]}), encoding="utf-8"
    )
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(
        rows=[{"fc_value": "opened"}, {"fc_value": "paid"}]
    )
    captured: dict = {}

    def fake_connect(kind, **kw):
        captured.update(kw)
        return warehouse

    monkeypatch.setattr("factcat_app.main.connect", fake_connect)
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "event_column": "event_name",
            "event_time": "occurred_at",
            "catalog": True,
        },
    )
    assert res.status_code == 200
    assert res.json()["values"] == ["opened", "paid"]
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert saved["event_names"] == ["opened", "paid"]
    assert saved.get("catalog_lookback_days") == 90
    assert "maximum_bytes_billed" not in captured


def _catalog_write_body():
    return {
        "project": "p",
        "location": "EU",
        "table": "analytics.events",
        "event_column": "event_name",
        "event_time": "occurred_at",
        "catalog": True,
        "write_project": "dest-proj",
        "write_dataset": "analytics",
    }


def test_event_values_write_cache_reads_existing(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    meta = {
        "v": 2,
        "table": "analytics.events",
        "event_column": "event_name",
        "event_time": "occurred_at",
        "kind": "materialized_view",
    }
    (tmp_path / "cfg.json").write_text(
        json.dumps({"event_name_cache": meta}), encoding="utf-8"
    )
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=[{"fc_value": "paid"}])
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={**_catalog_write_body(), "event_name_cache": meta},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["values"] == ["paid"]
    assert body["cache"] == "cached"
    assert body["kind"] == "materialized_view"
    assert warehouse.run.call_count == 1
    sql = warehouse.run.call_args.args[0].upper()
    assert "FC_EVENT_NAMES" in sql
    assert "CREATE" not in sql


def test_event_values_write_cache_builds_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()
    warehouse.run.side_effect = [
        AdapterError("not found"),
        QueryResult(rows=[]),
        QueryResult(rows=[{"fc_value": "paid"}]),
    ]
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post("/api/event_values", json=_catalog_write_body())
    assert res.status_code == 200
    body = res.json()
    assert body["values"] == ["paid"]
    assert body["cache"] == "materialized_view"
    assert warehouse.run.call_count == 3
    ddl = warehouse.run.call_args_list[1].args[0].upper()
    assert "CREATE OR REPLACE MATERIALIZED VIEW" in ddl
    assert "GROUP BY" in ddl
    read_sql = warehouse.run.call_args_list[2].args[0].upper()
    assert "FC_EVENT_NAMES" in read_sql
    assert "CREATE" not in read_sql


def test_event_values_write_cache_falls_back_to_table(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()

    def fake_run(sql, **_kw):
        upper = sql.upper()
        if "CREATE OR REPLACE MATERIALIZED VIEW" in upper:
            raise AdapterError("Source must be a table")
        if "CREATE OR REPLACE TABLE" in upper:
            return QueryResult(rows=[])
        if "FROM" in upper and "FC_EVENT_NAMES" in upper and "CREATE" not in upper:
            if fake_run.reads == 0:
                fake_run.reads += 1
                raise AdapterError("not found")
            return QueryResult(rows=[{"fc_value": "opened"}])
        raise AssertionError(sql)

    fake_run.reads = 0
    warehouse.run.side_effect = fake_run
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post("/api/event_values", json=_catalog_write_body())
    assert res.status_code == 200
    body = res.json()
    assert body["values"] == ["opened"]
    assert body["cache"] == "table"


def test_event_values_write_cache_permission_does_not_create(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    meta = {
        "v": 2,
        "table": "analytics.events",
        "event_column": "event_name",
        "event_time": "occurred_at",
        "kind": "materialized_view",
    }
    (tmp_path / "cfg.json").write_text(
        json.dumps({"event_name_cache": meta}), encoding="utf-8"
    )
    warehouse = MagicMock()
    warehouse.run.side_effect = AdapterError("Access Denied")
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={**_catalog_write_body(), "event_name_cache": meta},
    )
    assert res.status_code == 400
    assert "CREATE" not in warehouse.run.call_args.args[0].upper()
    assert warehouse.run.call_count == 1


def test_event_values_write_cache_falls_back_to_distinct(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    warehouse = MagicMock()

    def fake_run(sql, **_kw):
        upper = sql.upper()
        if "CREATE OR REPLACE" in upper:
            raise AdapterError("no create privilege")
        if "FC_EVENT_NAMES" in upper:
            raise AdapterError("not found")
        assert "DISTINCT" in upper
        return QueryResult(rows=[{"fc_value": "paid"}])

    warehouse.run.side_effect = fake_run
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post("/api/event_values", json=_catalog_write_body())
    assert res.status_code == 200
    body = res.json()
    assert body["values"] == ["paid"]
    assert "cache" not in body
    assert body.get("fallback") == "lookback"


def test_event_values_lookback_fallback_keeps_stored_meta(monkeypatch, tmp_path):
    """A degraded load must not destroy the cache fingerprint.

    The wipe locked Events into re-querying with "Creating event-name
    cache…" on every visit.
    """
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    meta = {
        "v": 2,
        "table": "analytics.events",
        "event_column": "event_name",
        "event_time": "occurred_at",
        "kind": "table",
    }
    (tmp_path / "cfg.json").write_text(
        json.dumps({"event_name_cache": meta}), encoding="utf-8"
    )
    warehouse = MagicMock()

    def fake_run(sql, **_kw):
        upper = sql.upper()
        if "CREATE" in upper:
            raise AdapterError("permission denied")
        if "FC_EVENT_NAMES" in upper:
            raise AdapterError("not found")
        return QueryResult(rows=[{"fc_value": "opened"}])

    warehouse.run.side_effect = fake_run
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={**_catalog_write_body(), "event_name_cache": {}},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["values"] == ["opened"]
    assert body.get("fallback") == "lookback"
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert saved["event_name_cache"] == meta


def test_event_values_empty_client_meta_uses_stored(monkeypatch, tmp_path):
    """A fresh page sends an empty meta mirror; the config's fingerprint
    must still count as a match - no rebuild, one cheap read."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    meta = {
        "v": 2,
        "table": "analytics.events",
        "event_column": "event_name",
        "event_time": "occurred_at",
        "kind": "materialized_view",
    }
    (tmp_path / "cfg.json").write_text(
        json.dumps({"event_name_cache": meta}), encoding="utf-8"
    )
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=[{"fc_value": "paid"}])
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: warehouse)
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={**_catalog_write_body(), "event_name_cache": {}},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["cache"] == "cached"
    assert warehouse.run.call_count == 1
    assert "CREATE" not in warehouse.run.call_args.args[0].upper()


def test_non_catalog_event_values_do_not_write_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(
        json.dumps({"event_names": ["keep"]}), encoding="utf-8"
    )
    warehouse = MagicMock()
    warehouse.run.return_value = QueryResult(rows=[{"fc_value": "paid"}])
    monkeypatch.setattr(
        "factcat_app.main.connect", lambda kind, **kw: warehouse
    )
    client = TestClient(app)
    res = client.post(
        "/api/event_values",
        json={
            "project": "p",
            "location": "EU",
            "table": "analytics.events",
            "event_column": "event_name",
            "event_time": "occurred_at",
        },
    )
    assert res.status_code == 200
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert saved["event_names"] == ["keep"]


def test_preferences_renders(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "adc-project")
    client = TestClient(app)
    res = client.get("/preferences")
    assert res.status_code == 200
    assert "<h1>User Preferences</h1>" in res.text
    assert "Follows you" not in res.text
    assert "Thousand separator" in res.text
    assert "Decimal separator" in res.text
    assert 'id="vocab"' in res.text
    assert "I'm a business user" in res.text
    assert "I'm an analyst familiar with SQL" in res.text
    assert "SQL letter case" in res.text
    assert "Uppercase (COUNT(*), WHERE)" in res.text
    assert "Lowercase (count(*), where)" in res.text
    assert 'id="sql-options-wrap"' in res.text
    assert "Not equal" in res.text
    assert "!=" in res.text
    assert "Factcat will tailor its wording for you." in res.text
    assert "/static/cat-business.jpg" in res.text
    assert "/static/cat-analyst.jpg" in res.text
    assert "Wording" in res.text
    assert "Vocabulary" not in res.text
    assert ">Plain<" not in res.text
    assert "Stored operators" not in res.text
    assert "Warehouse EXTRACT" not in res.text
    assert "Monday" in res.text
    assert ">Mon<" in res.text
    assert "January" in res.text
    assert ">Jan<" in res.text
    assert "Day of month" in res.text
    assert ">1–31<" in res.text
    assert ">01–31<" in res.text
    assert "Time of day" in res.text
    assert "12-hour" in res.text
    assert "24-hour" in res.text
    assert "hour-clock-btn" in res.text
    assert ">3pm<" in res.text or ">3pm</span>" in res.text
    assert "03:00" in res.text
    assert "15:00" in res.text
    assert "3:00pm" not in res.text
    assert "0h" in res.text
    assert 'name="hour_style"' in res.text
    assert "hour-ticks" in res.text
    assert 'id="theme-btn"' in res.text
    assert "the palette is not applied yet" not in res.text
    assert "<h1>Project setup</h1>" not in res.text
    assert 'href="/setup"' in res.text
    assert "data-theme=" in res.text
    assert 'id="save"' not in res.text
    assert ">Save<" not in res.text
    assert "/static/save.js" in res.text
    assert "save-toast" in res.text
    assert "scheduleSave" in res.text
    assert "fcAutosave" in res.text
    assert "/api/preferences" in res.text
    assert 'body.pad_day = body.pad_day === "true"' in res.text
    assert "gate: () => syncNumExample()" in res.text
    assert '"data-vocab"' in res.text


def test_preferences_save_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/preferences",
        json={
            "thousand_sep": "period",
            "decimal_sep": "comma",
            "vocab": "sql",
            "sql_case": "lower",
            "sql_neq": "!=",
            "weekday_style": "short",
            "month_style": "short",
            "pad_calendar": True,
            "theme": "dark",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["prefs"]["vocab"] == "sql"
    assert body["prefs"]["sql_case"] == "lower"
    assert body["prefs"]["sql_neq"] == "!="
    assert body["prefs"]["decimal_sep"] == "comma"
    stored = json.loads((tmp_path / "preferences.json").read_text(encoding="utf-8"))
    assert stored["theme"] == "dark"
    html = client.get("/preferences").text
    assert "selected" in html
    assert 'data-theme="dark"' in html
    events = client.get("/setup").text
    assert 'id="thousand_sep"' not in events


def test_preferences_rejects_same_separators(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post(
        "/api/preferences",
        json={"thousand_sep": "comma", "decimal_sep": "comma"},
    )
    assert res.status_code == 400
    assert "differ" in res.json()["error"]


def test_events_sql_vocab(monkeypatch, tmp_path):
    from factcat_app.prefs import save as save_prefs

    _map_cfg(tmp_path, monkeypatch)
    save_prefs({"vocab": "sql", "weekday_style": "short", "pad_calendar": True})
    html = TestClient(app).get("/").text
    assert "GROUP BY" in html
    assert '"vocab": "sql"' in html
    assert "Break down by" not in html
    assert "is any of" not in html
    assert '"label": "IN"' in html
    assert r"LIKE \u0027%s%\u0027" in html
    assert r"LIKE \u0027s%\u0027" in html
    assert '"label": "= (empty)"' in html
    assert "LIKE prefix" not in html
    assert '"combine": "`OR`"' in html
    assert '"combine": "Combine"' not in html
    assert '"add_breakdown": "`GROUP BY`"' in html
    assert "fc-sql" in html
    assert 'data-vocab="sql"' in html
    assert "fc-plus" in html
    assert "breakdown-slot-head" in html
    assert '"any_of": "`OR`"' in html
    assert '"event_or": "`OR`"' in html
    assert "fc-or" in html
    assert "Add breakdown" not in html
    assert "fc-plus-row" in html
    # Value-semantics chrome folds per the CHROME table; nothing names a
    # SQL construct the emitter does not produce.
    assert '"bd_at_event": "each row"' in html
    assert '"bd_at_first": "first non-null ever"' in html
    assert '"bd_at_latest": "last non-null ever"' in html
    assert '"bd_fill_charted": "(charted events)"' in html
    assert '"bd_fill_series": "(this series)"' in html
    assert '"bd_fill_any": "(any event)"' in html
    assert '"bd_section": "`GROUP BY`"' in html
    assert '"bd_fill_group": "Event names"' in html
    assert "IGNORE NULLS" not in html
    assert "COUNT(*)" in html
    assert "COUNT(DISTINCT id)" in html
    assert "SUM(x)" in html
    assert "AVERAGE(x)" in html
    assert "MEDIAN(x)" in html
    assert "AVG(COUNT(DISTINCT x))" in html
    assert 'optgroup label="Property value"' in html
    assert '"of": "of"' in html
    assert "Hour of day" in html
    assert "Day of month (01-31)" in html
    assert ">Mon<" in html or '"Mon"' in html


def test_events_sql_vocab_lower(monkeypatch, tmp_path):
    from factcat_app.prefs import save as save_prefs

    _map_cfg(tmp_path, monkeypatch)
    save_prefs({"vocab": "sql", "sql_case": "lower"})
    html = TestClient(app).get("/").text
    assert '"vocab": "sql"' in html
    assert '"add_filter": "`where`"' in html
    assert '"combine": "`or`"' in html
    assert "count(*)" in html
    assert "count(distinct id)" in html
    assert "sum(x)" in html
    assert "avg(count(distinct x))" in html
    assert "`group by` each series" in html
    assert '"add_filter": "`WHERE`"' not in html


def test_events_sql_neq_bang(monkeypatch, tmp_path):
    from factcat_app.prefs import save as save_prefs

    _map_cfg(tmp_path, monkeypatch)
    save_prefs({"vocab": "sql", "sql_neq": "!="})
    html = TestClient(app).get("/").text
    assert '"label": "!="' in html
    assert '"label": "!= (empty)"' in html
    assert r"\u003c\u003e (empty)" not in html


def test_save_strips_separators_from_project(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    client.post(
        "/api/save",
        json={
            "project": "acme",
            "thousand_sep": "space",
            "decimal_sep": "comma",
        },
    )
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert "thousand_sep" not in saved
    assert "decimal_sep" not in saved
    assert saved["project"] == "acme"


def test_columns_list_names(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.catalog.list_columns",
        lambda **kw: {
            "location": "EU",
            "columns": [
                {"name": "customer_care_id", "type": "STRING"},
                {"name": "event_datetime", "type": "DATETIME"},
            ],
        },
    )
    client = TestClient(app)
    res = client.post(
        "/api/columns",
        json={"project": "p", "dataset": "3_entity", "table_name": "customer_events"},
    )
    assert res.status_code == 200
    names = [c["name"] for c in res.json()["columns"]]
    assert names == ["customer_care_id", "event_datetime"]
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert [c["name"] for c in saved["columns"]] == [
        "customer_care_id",
        "event_datetime",
    ]


def test_datasets_require_project(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    res = client.post("/api/datasets", json={"project": ""})
    assert res.status_code == 400


def test_entity_column_types_are_ids_not_timestamps():
    assert column_fits("STRING", "entity")
    assert column_fits("integer", "entity")
    assert column_fits("INT64", "entity")
    assert column_fits("NUMERIC", "entity")
    assert not column_fits("TIMESTAMP", "entity")
    assert not column_fits("FLOAT", "entity")
    assert not column_fits("BOOL", "entity")
    assert not column_fits("RECORD", "entity")
    assert column_fits("JSON", "of")
    assert column_fits("JSON", "of_distinct")
    assert not column_fits("JSON", "entity")


def test_event_time_column_types_are_temporal():
    assert column_fits("TIMESTAMP", "event_time")
    assert column_fits("DATETIME", "event_time")
    assert not column_fits("DATE", "event_time")
    assert not column_fits("STRING", "event_time")
    assert not column_fits("TIME", "event_time")
    assert column_fits("INT64", "event_time")
    assert column_fits("NUMBER", "event_time", kind="snowflake")


def test_property_of_types_allow_float_not_string():
    assert column_fits("FLOAT64", "of")
    assert column_fits("NUMERIC", "of")
    assert not column_fits("STRING", "of")
    assert not column_fits("TIMESTAMP", "of")
    assert column_fits("STRING", "of_distinct")
    assert column_fits("FLOAT64", "of_distinct")
    assert not column_fits("BOOL", "of_distinct")


def test_event_name_column_is_string():
    assert column_fits("STRING", "event_column")
    assert not column_fits("INT64", "event_column")
    assert not column_fits("TIMESTAMP", "event_column")


def test_layout_state_roundtrips(monkeypatch, tmp_path):
    _map_cfg(tmp_path, monkeypatch)
    client = TestClient(app)
    res = client.post(
        "/api/save",
        json={
            "pane_sql_open": True,
            "pane_chart_open": False,
            "layout_config_px": 300,
            "layout_config_collapsed": True,
            "layout_chart_px": 260,
        },
    )
    assert res.status_code == 200
    stored = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert stored["pane_sql_open"] is True
    assert stored["pane_chart_open"] is False
    assert stored["layout_config_px"] == 300
    assert stored["layout_config_collapsed"] is True
    assert stored["layout_chart_px"] == 260
    html = client.get("/events").text
    assert "workspace-form config-collapsed" in html
    assert "--fc-config-col: 300px" in html
    assert "--fc-chart-px: 260px" in html
    assert 'class="pane pane-chart closed"' in html
    assert 'class="pane pane-sql closed"' not in html


def test_html_is_never_cached_but_static_is(monkeypatch, tmp_path):
    """A stale tab showing last week's template is the recurring local-app
    failure; HTML must be no-store while static files stay cacheable."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "p")
    client = TestClient(app)
    for path in ("/events", "/setup", "/preferences"):
        assert client.get(path).headers.get("cache-control") == "no-store", path
    assert client.get("/static/save.js").headers.get("cache-control") != "no-store"


def test_blocking_endpoints_use_threadpool():
    """Warehouse round-trips must not block uvicorn's event loop.

    Every endpoint that talks to a warehouse (or runs pip) awaits its
    blocking work in the threadpool, so a slow estimate cannot queue a run
    behind it. Handlers are async; the blocking call goes through
    run_in_threadpool.
    """
    import inspect

    from factcat_app import main as main_mod

    blocking = (
        main_mod.api_roles,
        main_mod.api_warehouses,
        main_mod.api_datasets,
        main_mod.api_schemas,
        main_mod.api_tables,
        main_mod.api_columns,
        main_mod.api_layout,
        main_mod.api_write_access,
        main_mod.api_infer_epoch,
        main_mod.api_event_values,
        main_mod.api_install_extra,
        main_mod.api_sql,
        main_mod.api_estimate,
        main_mod.api_managed,
        main_mod.api_managed_action,
        main_mod.api_run,
    )
    for fn in blocking:
        assert "run_in_threadpool" in inspect.getsource(fn), fn.__name__


# ---------------------------------------------------------------------------
# Factcat-managed tables (item 12): build on Run, estimate stays a dry run,
# the Setup list and actions, and the chrome both pages carry.


def _managed_body(**extra):
    body = {
        "project": "p",
        "location": "EU",
        "table": "analytics.events",
        "entity": "account_id",
        "event_time": "occurred_at",
        "event_column": "event_name",
        "event_values": ["login"],
        "measure": "total",
        "grain": "day",
        "lookback_days": 30,
        "write_project": "dest-proj",
        "write_dataset": "analytics_fc",
        "breakdowns": [
            {
                "breakdown_column": "plan",
                "value_at": "event",
                "if_missing": "fill",
                "fill_from_event": "subscription_started",
            }
        ],
        "top_n": 8,
        "include_other": True,
    }
    body.update(extra)
    return body


class _ManagedWarehouse:
    """Answers the probe (sparse), bookmarks, and every other statement."""

    def __init__(self, *, fail_on=()):
        self.calls: list[tuple[str, bool]] = []
        self.fail_on = fail_on

    def run(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        self.calls.append((sql, dry_run))
        up = sql.upper()
        for needle in self.fail_on:
            if needle.upper() in up and not dry_run:
                raise AdapterError(f"boom: {needle}")
        if dry_run:
            return QueryResult(rows=[], bytes_processed=1024 ** 3)
        if "FC_PRESENT" in up:
            return QueryResult(rows=[{"fc_rows": 1000, "fc_present": 10}])
        if "FC_BOOKMARK" in up:
            from datetime import datetime, timezone
            return QueryResult(rows=[{"fc_event_name": "subscription_started", "fc_bookmark": datetime(2026, 9, 1, tzinfo=timezone.utc), "fc_rows": 5}])
        if up.startswith("SELECT") and "FC_COLUMN_INDEX" not in up:
            return QueryResult(rows=[{"bucket": "2026-01-05", "plan": "pro", "value": 2}])
        return QueryResult(rows=[])


def test_run_builds_the_index_first_then_queries_through_it(monkeypatch, tmp_path):
    """First Run on a sparse column: probe, CREATE, INSERT, bookmarks,
    comment, then the chart query reads fc_column_index. Sequential, one
    warehouse. Mutation: query before build → the chart SQL comes first."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=_managed_body())
    assert res.status_code == 200, res.text
    body = res.json()
    ups = [c[0].upper() for c in wh.calls if not c[1]]
    order = [
        next(i for i, u in enumerate(ups) if "FC_PRESENT" in u),
        next(i for i, u in enumerate(ups) if u.startswith("CREATE TABLE IF NOT EXISTS")),
        next(i for i, u in enumerate(ups) if u.startswith("INSERT INTO")),
        next(i for i, u in enumerate(ups) if "MAX(FC_AT)" in u),
        next(i for i, u in enumerate(ups) if "FC_VALUES" in u and "FC_COLUMN_INDEX" in u),
    ]
    assert order == sorted(order), ups
    assert "fc_column_index" in body["sql"]
    assert body.get("managed_note", "").startswith("Indexed `plan` \u00b7 later runs ~ ")
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert "plan" in saved["managed_tables"]["columns"]
    assert saved["managed_last_sweep"]


def test_run_falls_back_live_when_the_build_fails(monkeypatch, tmp_path):
    """A failed INSERT turns the column live for this run: the chart still
    answers from the full history and the run row says why."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    wh = _ManagedWarehouse(fail_on=("INSERT INTO",))
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=_managed_body())
    assert res.status_code == 200, res.text
    body = res.json()
    assert "fc_column_index" not in body["sql"]
    assert body["managed_failed"].startswith("Could not index `plan`")
    assert "chart is correct" in body["managed_failed"]


def test_run_respects_mode_off_and_no_destination(monkeypatch, tmp_path):
    """Mode lives in the config file (Setup writes it); the Events request
    never carries it. Off must still close the gate on Run."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(json.dumps({"managed_mode": "off"}), encoding="utf-8")
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=_managed_body())
    assert res.status_code == 200
    assert not any(c[0].upper().startswith("CREATE TABLE") for c in wh.calls)
    assert not any("FC_PRESENT" in c[0].upper() for c in wh.calls)
    wh2 = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh2)
    res2 = client.post("/api/run", json=_managed_body(write_project="", write_dataset=""))
    assert res2.status_code == 200
    assert not any("FC_PRESENT" in c[0].upper() for c in wh2.calls)


def test_estimate_never_probes_or_writes(monkeypatch, tmp_path):
    """The estimate is a free dry run: no probe, no CREATE, no INSERT; with
    no cached probe the column simply estimates live."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/estimate", json=_managed_body())
    assert res.status_code == 200, res.text
    assert all(c[1] for c in wh.calls)  # every call was a dry run
    assert not any("FC_PRESENT" in c[0].upper() for c in wh.calls)
    # unprobed: the chip already covers a possible build; the line says "may"
    # nothing is said before the run: the chip already includes a build
    assert "managed_note" not in res.json()
    assert "managed_build" not in res.json()


def test_estimate_prices_the_build_once_the_probe_is_cached(monkeypatch, tmp_path):
    """The registry mirror lives in the config file; the estimate must read
    it from there (the request never carries it)."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    mirror = {"v": 1, "columns": {}, "probes": {"plan": {"density": 0.01, "at": "2026-09-02T10:00:00+00:00"}}}
    (tmp_path / "cfg.json").write_text(json.dumps({"managed_tables": mirror}), encoding="utf-8")
    res = client.post("/api/estimate", json=_managed_body())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["managed_build"] == ["plan"]  # feeds the running copy only
    assert "managed_note" not in body
    assert body["bytes"] == 2 * 1024 ** 3  # query + build, both dry-run
    assert all(c[1] for c in wh.calls)


def test_managed_list_and_actions(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    registry = {
        "v": 1, "fp": managed_mod.config_fingerprint(_managed_body()),
        "columns": {"plan": {"expr": "plan", "label": "plan", "built_at": "2026-09-01T00:00:00+00:00",
                             "refreshed_at": "2026-09-01T00:00:00+00:00", "last_used_at": "2026-09-01T00:00:00+00:00",
                             "bookmark": "2026-09-01T00:00:00+00:00", "use_count": 2, "pinned": False, "overrides": {}}},
    }
    monkeypatch.setattr(
        managed_mod, "_stats",
        lambda form, table: {"name": table, "kind": "table", "bytes": 84 * 1024 ** 2, "rows": 1000,
                             "description": json.dumps(registry) if table == "fc_column_index" else ""},
    )
    client = TestClient(app)
    res = client.post("/api/managed", json=_managed_body())
    assert res.status_code == 200, res.text
    body = res.json()
    assert [c["key"] for c in body["columns"]] == ["plan"]
    assert body["columns"][0]["stale"] is False
    assert any(t["name"] == "fc_column_index" and t["bytes"] == 84 * 1024 ** 2 for t in body["tables"])
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    gone = client.post("/api/managed/action", json={**_managed_body(), "action": "pin", "key": "plan", "value": True})
    assert gone.status_code == 400
    assert gone.json()["error"] == "Drop did not run: action must be drop. The table is unchanged."
    drop = client.post("/api/managed/action", json={**_managed_body(), "action": "drop", "key": "plan"})
    assert drop.status_code == 200, drop.text
    assert "plan" not in drop.json()["registry"]["columns"]
    # it was the only column, so the table goes whole: no per-column DELETE
    assert any(c[0].upper().startswith("DROP TABLE") for c in wh.calls)
    assert not any(c[0].upper().startswith("DELETE FROM") for c in wh.calls)
    bad = client.post("/api/managed/action", json={**_managed_body(), "action": "explode", "key": "plan"})
    assert bad.status_code == 400


def test_setup_and_events_carry_the_managed_chrome(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    client = TestClient(app)
    setup = client.get("/setup").text
    for needle in ("id=\"managed-wrap\"", "name=\"managed_mode\"", "managed_drop_days", "managed_refresh_days", "managed_lookback_days", "id=\"managed-list\""):
        assert needle in setup, needle
    # withdrawn at the owner's review: manual index, pins, overrides, per-row refresh/rebuild
    for gone in ("Index a column now", "managed-add", "data-act=\"pin\"", "data-ovr", "data-act=\"rebuild\"", "data-act=\"refresh\""):
        assert gone not in setup, gone
    # the stale row's consequence is Mode-dependent: both sentences exist and the
    # Off one names what to do
    assert "until the next run that uses it rebuilds it." in setup
    assert "set Mode to Automatic and the next run that uses it rebuilds it." in setup
    assert "physical" not in setup.lower()  # withdrawn field stays out
    events = client.get("/events").text
    assert "id=\"run-note\"" in events
    # the owner's word on Events is "Indexing"; the mechanism words stay out
    running = events.split("function runningPrefix")[1][:400]
    assert '"Indexing "' in running
    for word in ("relation", "watermark", "prepar"):
        assert word not in running.lower()
    # the line sits on its own toolbar line, right-aligned and capped
    assert "#run-note { flex: 1 1 100%; display: flex; justify-content: flex-end;" in events
    assert "#run-note > span { max-width: 32rem; text-align: right;" in events
    # One convention for backticked labels: the note and both running-copy
    # branches render through the same helper (never literal backticks).
    assert events.count("appendMarked(") >= 3
    # A read failure pauses the row actions until the registry reads again;
    # an action failure states its verdict and leaves them live.
    status_js = setup.split("function managedSetStatus")[1][:900]
    assert "pause !== false" in status_js
    # The section shows the moment the destination is set; only the list waits
    # for /api/managed. Hidden until the round trip returned read as "missing".
    load_js = setup.split("async function loadManaged")[1]
    load_js = load_js[:load_js.index("await post(\"/api/managed\"")]
    assert load_js.rstrip().endswith("try {") or "renderManaged();" in load_js.split("managedSetLoading(true);")[1]
    assert "renderManaged();" in load_js.split("managedSetLoading(true);")[1]
    assert "if (managedListLoading) return;" in setup.split("function renderManaged")[1]
    assert 'managedSetStatus((err && err.message) || "The action did not run.", true, false)' in setup
    assert "appendMarked(box, text" in events.split("function setRunNote")[1][:260]
    assert events.split("function setRunningCopy")[1][:700].count("appendMarked(copy") == 2


def test_estimate_prices_the_build_as_a_select_and_survives_a_missing_table(monkeypatch, tmp_path):
    """The build price is a dry run of the SELECT, never an INSERT into a
    table that may not exist; and if pricing fails the chart is still
    estimated."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))

    class _W(_ManagedWarehouse):
        def run(self, sql, *, dry_run=False):
            if dry_run and sql.upper().startswith("INSERT"):
                raise AdapterError("Not found: Table fc_column_index")
            return super().run(sql, dry_run=dry_run)

    wh = _W()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    mirror = {"v": 1, "columns": {}, "probes": {"plan": {"density": 0.01, "at": "2026-09-02T10:00:00+00:00"}}}
    (tmp_path / "cfg.json").write_text(json.dumps({"managed_tables": mirror}), encoding="utf-8")
    res = client.post("/api/estimate", json=_managed_body())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["managed_build"] == ["plan"]
    assert not any(c[0].upper().startswith("INSERT") for c in wh.calls)
    assert body["bytes"] == 2 * 1024 ** 3


def test_run_reconciles_the_mirror_with_the_table(monkeypatch, tmp_path):
    """A mirror that says 'attach' while the table is gone must not attach:
    the run plans from the table's own registry (absent here), probes, and
    builds instead of reading an empty relation."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    body = _managed_body()
    stale_mirror = {
        "v": 1, "fp": managed_mod.config_fingerprint({**body, "kind": "bigquery"}),
        "columns": {"plan": {"expr": "plan", "label": "plan", "built_at": "2026-09-01T00:00:00+00:00",
                             "refreshed_at": "2026-09-01T00:00:00+00:00", "last_used_at": "2026-09-01T00:00:00+00:00",
                             "bookmark": "2026-09-01T00:00:00+00:00", "use_count": 1, "pinned": False, "overrides": {}}},
    }
    (tmp_path / "cfg.json").write_text(json.dumps({"managed_tables": stale_mirror}), encoding="utf-8")
    monkeypatch.setattr(managed_mod, "authoritative_registry", lambda form: ({}, None))
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=body)
    assert res.status_code == 200, res.text
    ups = [c[0].upper() for c in wh.calls if not c[1]]
    assert any(u.startswith("CREATE TABLE IF NOT EXISTS") for u in ups)  # rebuilt, not attached blind
    assert "fc_column_index" in res.json()["sql"]


def test_run_falls_back_live_when_the_attached_table_vanishes(monkeypatch, tmp_path):
    """The plan attaches, the query hits a missing relation: rerun live,
    report it once, forget the bookmarks, no usage write, no built note."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    body = _managed_body()
    reg = {
        "v": 1, "fp": managed_mod.config_fingerprint({**body, "kind": "bigquery"}),
        "columns": {"plan": {"expr": "plan", "label": "plan", "built_at": "2026-09-01T00:00:00+00:00",
                             "refreshed_at": "2026-09-01T00:00:00+00:00", "last_used_at": "2026-09-01T00:00:00+00:00",
                             "bookmark": "2026-09-01T00:00:00+00:00", "use_count": 1, "pinned": False, "overrides": {}}},
        "probes": {"plan": {"density": 0.01, "at": "2026-09-02T10:00:00+00:00"}},
    }
    (tmp_path / "cfg.json").write_text(json.dumps({"managed_tables": reg}), encoding="utf-8")
    # the description still says the column is there (stale metadata), but the query finds no table
    monkeypatch.setattr(managed_mod, "authoritative_registry", lambda form: (reg, {"bytes": 1}))

    class _W(_ManagedWarehouse):
        def run(self, sql, *, dry_run=False):
            if not dry_run and "fc_column_index" in sql and sql.upper().startswith("SELECT"):
                self.calls.append((sql, dry_run))
                raise AdapterError("Not found: Table dest-proj:analytics_fc.fc_column_index")
            return super().run(sql, dry_run=dry_run)

    wh = _W()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=body)
    assert res.status_code == 200, res.text
    out = res.json()
    assert "fc_column_index" not in out["sql"]
    assert out["managed_failed"].startswith("The indexed table was not found")
    assert "managed_note" not in out
    ups = [c[0].upper() for c in wh.calls if not c[1]]
    assert not any(u.startswith("ALTER TABLE") or u.startswith("COMMENT ON") for u in ups)
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert "columns" not in saved["managed_tables"]
    assert saved["managed_tables"]["probes"]["plan"]["density"] == 0.01


def test_estimate_attaches_an_existing_index_from_the_config_mirror(monkeypatch, tmp_path):
    """With an index on file the estimate prices the spliced query, not the
    full history — otherwise the cost gate blocks a run that is now cheap."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    body = _managed_body()
    reg = {
        "v": 1, "fp": managed_mod.config_fingerprint({**body, "kind": "bigquery"}),
        "columns": {"plan": {"expr": "plan", "label": "plan", "built_at": "2026-09-01T00:00:00+00:00",
                             "refreshed_at": "2026-09-01T00:00:00+00:00", "last_used_at": "2026-09-01T00:00:00+00:00",
                             "bookmark": "2026-09-01T00:00:00+00:00", "use_count": 1, "pinned": False, "overrides": {}}},
    }
    (tmp_path / "cfg.json").write_text(json.dumps({"managed_tables": reg}), encoding="utf-8")
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/estimate", json=body)
    assert res.status_code == 200, res.text
    dry = [c[0] for c in wh.calls if c[1]]
    assert any("fc_column_index" in s for s in dry)
    assert "managed_note" not in res.json()


def test_sweep_plans_from_the_table_registry_not_the_mirror(monkeypatch, tmp_path):
    """Two workstations, one index. A's mirror says the column is 90 days
    unused; the table's own description (B charts daily) says an hour ago.
    A's Run must drop nothing. Mutation: sweep before reconcile."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    body = _managed_body()
    now = datetime.now(timezone.utc)

    def registry(last_used):
        return {
            "v": 1, "fp": managed_mod.config_fingerprint({**body, "kind": "bigquery"}),
            "columns": {"plan": {"expr": "plan", "label": "plan", "built_at": (now - timedelta(days=100)).isoformat(),
                                 "refreshed_at": (now - timedelta(hours=1)).isoformat(), "last_used_at": last_used.isoformat(),
                                 "bookmark": (now - timedelta(hours=1)).isoformat(), "use_count": 9, "pinned": False, "overrides": {}}},
            "probes": {"plan": {"density": 0.01, "at": now.isoformat()}},
        }

    (tmp_path / "cfg.json").write_text(json.dumps({"managed_tables": registry(now - timedelta(days=90))}), encoding="utf-8")
    monkeypatch.setattr(managed_mod, "authoritative_registry", lambda form: (registry(now - timedelta(hours=1)), {"bytes": 1}))
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=body)
    assert res.status_code == 200, res.text
    ups = [c[0].upper() for c in wh.calls if not c[1]]
    assert not any(u.startswith("DELETE FROM") or u.startswith("DROP TABLE") for u in ups)
    # control: when the table's own registry agrees the column is unused, the sweep does drop it
    (tmp_path / "cfg.json").write_text(json.dumps({"managed_tables": registry(now - timedelta(days=90))}), encoding="utf-8")
    monkeypatch.setattr(managed_mod, "authoritative_registry", lambda form: (registry(now - timedelta(days=90)), {"bytes": 1}))
    wh2 = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh2)
    res2 = client.post("/api/run", json=body)
    assert res2.status_code == 200, res2.text
    assert any(c[0].upper().startswith("DELETE FROM") for c in wh2.calls if not c[1])


def test_write_access_verdict_is_saved_for_the_run_gate(monkeypatch, tmp_path):
    """The Setup check's verdict is what stops an automatic build on a denied
    destination (managed._write_ok); it must be persisted, not just painted."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.write_access_from_form", lambda form: {"status": "denied", "granted": []})
    client = TestClient(app)
    res = client.post("/api/write_access", json={"kind": "bigquery", "project": "p", "write_project": "p", "write_dataset": "d"})
    assert res.status_code == 200, res.text
    assert json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))["write_access_status"] == "denied"
    monkeypatch.setattr("factcat_app.main.write_access_from_form", lambda form: {"status": "skipped"})
    client.post("/api/write_access", json={"kind": "bigquery"})
    assert json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))["write_access_status"] == "denied"
    monkeypatch.setattr("factcat_app.main.write_access_from_form", lambda form: {"status": "ok", "granted": ["bigquery.tables.create"]})
    client.post("/api/write_access", json={"kind": "bigquery", "project": "p", "write_project": "p", "write_dataset": "d"})
    assert json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))["write_access_status"] == "ok"


def test_managed_drop_runs_under_the_scan_cap(monkeypatch, tmp_path):
    """Drop is a billed DELETE on BigQuery: the action connection keeps the
    scan cap. Mutation: connection_from_form(merged, apply_scan_cap=False)."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    body = _managed_body(bytes_cap_gb=2)
    reg = {
        "v": 1, "fp": managed_mod.config_fingerprint({**body, "kind": "bigquery"}),
        "columns": {"plan": {"expr": "plan", "label": "plan", "built_at": "2026-09-01T00:00:00+00:00",
                             "refreshed_at": "2026-09-01T00:00:00+00:00", "last_used_at": "2026-09-01T00:00:00+00:00",
                             "bookmark": "2026-09-01T00:00:00+00:00", "use_count": 1}},
    }
    monkeypatch.setattr(managed_mod, "authoritative_registry", lambda form: (reg, {"bytes": 1}))
    seen = {}

    def fake_connect(kind, **kw):
        seen.update(kw)
        return _ManagedWarehouse()

    monkeypatch.setattr("factcat_app.main.connect", fake_connect)
    client = TestClient(app)
    res = client.post("/api/managed/action", json={**body, "action": "drop", "key": "plan"})
    assert res.status_code == 200, res.text
    assert seen.get("maximum_bytes_billed") == 2 * 1024 ** 3


class _CensusWarehouse(_ManagedWarehouse):
    """Answers the name-grain census with `rows` per event name."""

    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.census_rows = rows

    def run(self, sql, *, dry_run: bool = False):
        if "SUM(fc_rows)" in sql or "SUM(FC_ROWS)" in sql.upper():
            self.calls.append((sql, dry_run))
            return QueryResult(rows=[
                {"fc_value": name, "fc_rows": n, "fc_first": "2026-01-01T00:00:00+00:00",
                 "fc_last": "2026-09-01T00:00:00+00:00"}
                for name, n in self.census_rows.items()
            ])
        return super().run(sql, dry_run=dry_run)


def _census_registry(body, *, refreshed_days_ago, snapshot_rows):
    from datetime import datetime, timedelta, timezone
    from factcat_app import managed as managed_mod

    now = datetime.now(timezone.utc)
    then = (now - timedelta(days=refreshed_days_ago)).isoformat()
    return {
        "v": 1, "fp": managed_mod.config_fingerprint({**body, "kind": "bigquery"}),
        "columns": {"plan": {
            "expr": "plan", "label": "plan", "built_at": then, "refreshed_at": then,
            "last_used_at": (now - timedelta(hours=1)).isoformat(), "bookmark": then,
            "use_count": 5,
            "names": {"subscription_started": {"rows": snapshot_rows,
                                               "first": "2026-01-01T00:00:00+00:00",
                                               "last": "2026-09-01T00:00:00+00:00"}},
        }},
        "probes": {"plan": {"density": 0.01, "at": now.isoformat()}},
    }


def test_run_reads_the_census_and_repairs_a_name_whose_rows_shrank(monkeypatch, tmp_path):
    """The self-repair answers "what if history changed": a name whose row
    count fell is deleted from the index and backfilled whole. It is gated on
    the event-name census, whose fingerprint lives in the config file — the
    Events request never carries it, so the run path must merge it.
    Mutation: drop "event_name_cache" from MANAGED_KEYS and no census is read
    and no repair fires (the state this test was written to catch)."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    body = _managed_body()
    reg = _census_registry(body, refreshed_days_ago=8, snapshot_rows=1000)
    (tmp_path / "cfg.json").write_text(json.dumps({
        "managed_tables": reg,
        "event_name_cache": {"v": 2, "kind": "view", "fp": "x"},
    }), encoding="utf-8")
    monkeypatch.setattr(managed_mod, "authoritative_registry", lambda form: (reg, {"bytes": 1}))
    wh = _CensusWarehouse({"subscription_started": 400})
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=body)
    assert res.status_code == 200, res.text
    ups = [c[0] for c in wh.calls if not c[1]]
    assert any("SUM(fc_rows)" in s or "SUM(FC_ROWS)" in s.upper() for s in ups), "census never read"
    deletes = [s for s in ups if s.upper().strip().startswith("DELETE FROM")]
    assert deletes, "a shrunk name was not repaired"
    assert "subscription_started" in deletes[0]
    assert any(s.upper().startswith("INSERT") for s in ups)


def test_run_leaves_the_index_alone_when_the_census_matches(monkeypatch, tmp_path):
    """Control for the repair: same age, same wiring, unchanged row count —
    the refresh appends and never deletes."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    body = _managed_body()
    reg = _census_registry(body, refreshed_days_ago=8, snapshot_rows=1000)
    (tmp_path / "cfg.json").write_text(json.dumps({
        "managed_tables": reg,
        "event_name_cache": {"v": 2, "kind": "view", "fp": "x"},
    }), encoding="utf-8")
    monkeypatch.setattr(managed_mod, "authoritative_registry", lambda form: (reg, {"bytes": 1}))
    wh = _CensusWarehouse({"subscription_started": 1000})
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=body)
    assert res.status_code == 200, res.text
    ups = [c[0] for c in wh.calls if not c[1]]
    assert any("SUM(fc_rows)" in s or "SUM(FC_ROWS)" in s.upper() for s in ups)
    assert not [s for s in ups if s.upper().strip().startswith("DELETE FROM")]


def test_run_backfills_a_new_event_name_whole(monkeypatch, tmp_path):
    """The silent-wrong-answer case. A name whose history is backfilled into
    the source lands BEHIND the watermark: the live tail starts after the
    bookmark, and refresh_sql deliberately skips names the index has never
    seen, so neither side of the splice carries it. Only the census's
    new-name backfill closes that, and it is gated on the same config key.
    Mutation: drop "event_name_cache" from MANAGED_KEYS."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    body = _managed_body()
    reg = _census_registry(body, refreshed_days_ago=8, snapshot_rows=1000)
    (tmp_path / "cfg.json").write_text(json.dumps({
        "managed_tables": reg,
        "event_name_cache": {"v": 2, "kind": "view", "fp": "x"},
    }), encoding="utf-8")
    monkeypatch.setattr(managed_mod, "authoritative_registry", lambda form: (reg, {"bytes": 1}))
    # the census knows a name the index has never seen, with history
    wh = _CensusWarehouse({"subscription_started": 1000, "plan_backfilled": 5000})
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=body)
    assert res.status_code == 200, res.text
    ups = [c[0] for c in wh.calls if not c[1]]
    inserts = [s for s in ups if s.upper().startswith("INSERT")]
    assert any("plan_backfilled" in s for s in inserts), "a new name was never backfilled whole"
    # and it is a whole-history backfill, not a bookmarked append
    whole = [s for s in inserts if "plan_backfilled" in s]
    assert not any("fc_bookmark" in s.lower() for s in whole)


def test_a_refused_column_saves_its_probe(monkeypatch, tmp_path):
    """A density probe costs PROBE_DAYS of one column. A column the gate
    refuses produces no build and nothing attachable, so its probe used to
    be thrown away and re-run on every chart, forever. Mutation: drop the
    probe-persistence branch in api_run."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))

    class _Dense(_ManagedWarehouse):
        def run(self, sql, *, dry_run: bool = False):
            if "FC_PRESENT" in sql.upper():
                self.calls.append((sql, dry_run))
                return QueryResult(rows=[{"fc_rows": 1000, "fc_present": 900}])
            return super().run(sql, dry_run=dry_run)

    wh = _Dense()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=_managed_body())
    assert res.status_code == 200, res.text
    assert not any(c[0].upper().startswith("CREATE TABLE") for c in wh.calls)  # refused
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    probe = (saved.get("managed_tables") or {}).get("probes", {}).get("plan")
    assert probe and abs(probe["density"] - 0.9) < 1e-9, "the probe was not saved"
    # a second run reads the cache instead of probing again
    wh2 = _Dense()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh2)
    res2 = client.post("/api/run", json=_managed_body())
    assert res2.status_code == 200, res2.text
    assert not any("FC_PRESENT" in c[0].upper() for c in wh2.calls), "re-probed a refused column"


def test_drop_writes_the_registry_before_it_deletes_the_rows(monkeypatch, tmp_path):
    """The description is the authority: if it still lists a column whose
    rows are gone, a later run attaches to an empty column and reads only
    the live tail - silently wrong. So the registry is written first, and a
    failed DELETE leaves a recoverable state, not a lying one.
    Mutation: put the DELETE back before the comment write."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat_app import managed as managed_mod

    body = _managed_body()
    fp = managed_mod.config_fingerprint({**body, "kind": "bigquery"})
    entry = {"expr": "plan", "label": "plan", "built_at": "2026-09-01T00:00:00+00:00",
             "refreshed_at": "2026-09-01T00:00:00+00:00", "last_used_at": "2026-09-01T00:00:00+00:00",
             "bookmark": "2026-09-01T00:00:00+00:00", "use_count": 1}
    reg = {"v": 1, "fp": fp, "columns": {"plan": dict(entry), "tier": {**entry, "expr": "tier", "label": "tier"}}}
    monkeypatch.setattr(managed_mod, "authoritative_registry", lambda form: (reg, {"bytes": 1}))
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/managed/action", json={**body, "action": "drop", "key": "plan"})
    assert res.status_code == 200, res.text
    order = [c[0].upper().strip() for c in wh.calls if not c[1]]
    comment = next(i for i, s_ in enumerate(order) if s_.startswith("ALTER TABLE") or s_.startswith("COMMENT ON"))
    delete = next(i for i, s_ in enumerate(order) if s_.startswith("DELETE FROM"))
    assert comment < delete, "rows were deleted before the registry that describes them"
    assert "plan" not in res.json()["registry"]["columns"]
    assert "tier" in res.json()["registry"]["columns"]


def test_the_mapped_column_types_reach_the_run(monkeypatch, tmp_path):
    """is_text_column reads the mapping, which lives in the config file; the
    chart request carries none. Without the merge the guard was inert and a
    non-text column was INSERTed into a text fc_value on every run.
    Mutation: drop "columns" from MANAGED_KEYS."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    (tmp_path / "cfg.json").write_text(json.dumps({
        "columns": [{"name": "plan", "type": "INT64"}],
    }), encoding="utf-8")
    wh = _ManagedWarehouse()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=_managed_body())
    assert res.status_code == 200, res.text
    ups = [c[0].upper() for c in wh.calls if not c[1]]
    assert not any(u.startswith("CREATE TABLE") or u.startswith("INSERT") for u in ups), \
        "a non-text column was indexed anyway"
    assert not any("FC_PRESENT" in u for u in ups), "spent a probe on a column it cannot index"


def test_a_build_is_recorded_even_when_the_chart_then_fails(monkeypatch, tmp_path):
    """The registry is persisted the moment it changes. Batched behind the
    chart query, a cap rejection would lose the record of an index that
    exists, and the next run would append a second copy of the history.
    Mutation: remove the save() after apply_plan."""
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    from factcat.warehouses import BytesCapError

    class _ChartFails(_ManagedWarehouse):
        def run(self, sql, *, dry_run: bool = False):
            up = sql.upper().lstrip()
            # the chart is a WITH ... SELECT, not a bare SELECT
            is_chart = (
                "FC_COLUMN_INDEX" in up
                and "FC_BOOKMARK" not in up
                and not up.startswith(("CREATE", "INSERT", "DELETE", "ALTER", "DROP"))
            )
            if not dry_run and is_chart:
                self.calls.append((sql, dry_run))
                raise BytesCapError("over the cap", bytes_processed=99, maximum_bytes_billed=1)
            return super().run(sql, dry_run=dry_run)

    wh = _ChartFails()
    monkeypatch.setattr("factcat_app.main.connect", lambda kind, **kw: wh)
    client = TestClient(app)
    res = client.post("/api/run", json=_managed_body())
    assert res.status_code == 400 and res.json()["over_cap"] is True
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    cols = (saved.get("managed_tables") or {}).get("columns") or {}
    assert "plan" in cols, "the index was built and then forgotten"
