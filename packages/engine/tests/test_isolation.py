"""The suite can never touch the real mapping.

`config_path()` falls back to a CWD-relative `.factcat.json`, and the
directory pytest runs from has held a production mapping with managed tables
on automatic. The autouse fixture in conftest.py is the only structural guard.

Mutation: remove the FACTCAT_CONFIG line from `isolate_user_state` and the
first test goes red (the path resolves under the working directory, not
tmp_path).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from factcat_app.config import config_path
from factcat_app.main import app


def test_no_test_can_resolve_the_real_mapping(tmp_path):
    resolved = config_path()
    assert resolved.parent == tmp_path.resolve()
    assert resolved.name == "factcat.json"
    assert not resolved.exists()


def test_build_info_names_the_mapping_by_basename_only():
    info = TestClient(app).get("/api/build").json()
    assert info["config"] == Path(os.environ["FACTCAT_CONFIG"]).name
    assert "/" not in info["config"] and "\\" not in info["config"]
