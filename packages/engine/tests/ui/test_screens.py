"""Main-flow screenshots for the ui-ux-review gate. Warehouse is mocked.

Run: python -m pytest -m ui tests/ui (needs `pip install playwright` and
`python -m playwright install chromium`). PNGs land in test-results/ui/.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

OUT = Path("test-results/ui")
SCREENS = {"setup": "/setup", "events": "/events"}
WIDTHS = (390, 1440)


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    cfg = tmp_path_factory.mktemp("cfg") / "cfg.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    mp.setenv("FACTCAT_CONFIG", str(cfg))
    mp.setattr("factcat_app.main.bootstrap_project", lambda: "synthetic-project")
    mp.setattr("factcat_app.catalog.list_datasets", lambda **_: ["analytics"])
    mp.setattr("factcat_app.catalog.list_tables", lambda **_: ["events"])
    from factcat_app.main import app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)
    mp.undo()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("name", SCREENS)
def test_screen(base_url, name, width, scheme):
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base_url + SCREENS[name])
        page.wait_for_load_state("load")
        page.wait_for_timeout(500)
        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        page.screenshot(path=str(OUT / f"{name}-{width}-{scheme}.png"), full_page=True)
        browser.close()
    assert errors == [], errors
    assert not overflow, "horizontal overflow"
