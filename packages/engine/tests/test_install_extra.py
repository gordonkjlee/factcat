"""First-run warehouse extra: probe, Setup card, pip only on Install."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from factcat.warehouses import ADAPTERS, extra_installed, extra_requirement, extras_status
from factcat_app.extras import install_argv, install_command, run_install
from factcat_app.main import app


def _adapter_class(kind: str):
    target = ADAPTERS[kind]
    module_name, _, class_name = target.partition(":")
    return getattr(importlib.import_module(module_name), class_name)


@pytest.fixture()
def py_pi_install(monkeypatch):
    # Pin BOTH branches install_argv depends on. Without the find_spec pin
    # these tests pass or fail on whether the interpreter running the suite
    # happens to carry pip - and a uv tool env, the case this module exists
    # for, does not.
    monkeypatch.setattr("factcat_app.extras.editable_origin", lambda: None)
    monkeypatch.setattr(
        "factcat_app.extras.importlib.util.find_spec", lambda name: object()
    )


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_extra_requirement_named_after_kind(kind):
    assert extra_requirement(kind) == f"factcat[{kind}]"


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_extra_installed_respects_probe(kind, monkeypatch):
    cls = _adapter_class(kind)
    monkeypatch.setattr(cls, "driver_available", classmethod(lambda cls: False))
    assert extra_installed(kind) is False
    monkeypatch.setattr(cls, "driver_available", classmethod(lambda cls: True))
    assert extra_installed(kind) is True


@pytest.mark.parametrize("kind", list(ADAPTERS))
def test_driver_available_declared(kind):
    cls = _adapter_class(kind)
    assert callable(getattr(cls, "driver_available", None))


def test_extras_status_walks_adapters():
    assert set(extras_status()) == set(ADAPTERS)


def test_install_argv_is_this_interpreter(py_pi_install):
    argv = install_argv("snowflake")
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "pip", "install"]
    assert argv[-1] == "factcat[snowflake]"
    assert "-e" not in argv


def test_an_interpreter_without_pip_installs_through_uv(monkeypatch):
    """An isolated install (`uv tool`, `pipx`) need not carry pip.

    Assuming `python -m pip` gave those users a Setup button that fails with
    "No module named pip" and printed a command they cannot run - the one
    install front-end the app suggests, unusable by the install method it is
    most likely to have been installed with.

    Mutation: hard-code the pip argv again.
    """
    fake_uv = "/opt/bin/uv"
    monkeypatch.setattr("factcat_app.extras.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr(
        "factcat_app.extras.shutil.which", lambda name: fake_uv if name == "uv" else None
    )
    monkeypatch.setattr("factcat_app.extras.editable_origin", lambda: None)
    argv = install_argv("snowflake")
    assert argv[:2] == [fake_uv, "pip"]
    assert argv[2:5] == ["install", "--python", sys.executable]
    assert argv[-1] == "factcat[snowflake]"
    assert "-m" not in argv, "it must not route through the missing pip module"


def test_a_source_checkout_installs_editable_through_uv(monkeypatch, tmp_path):
    """The two branches compose: `-e <checkout>[kind]` has to land after
    `--python <exe>`, not before it. Only append order enforces that."""
    origin = tmp_path / "packages" / "engine"
    origin.mkdir(parents=True)
    fake_uv = "/opt/bin/uv"
    monkeypatch.setattr("factcat_app.extras.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr(
        "factcat_app.extras.shutil.which", lambda name: fake_uv if name == "uv" else None
    )
    monkeypatch.setattr("factcat_app.extras.editable_origin", lambda: origin)
    argv = install_argv("bigquery")
    assert argv[:5] == [fake_uv, "pip", "install", "--python", sys.executable]
    assert argv[5:] == ["-e", f"{origin}[bigquery]"]


def test_a_broken_pip_spec_does_not_take_the_page_down(monkeypatch):
    """`find_spec` raises rather than answering when `sys.modules` holds a
    stale entry. This runs while Setup renders, so it must degrade to another
    installer instead of 500ing the page."""
    def boom(name):
        raise ValueError("pip.__spec__ is None")

    monkeypatch.setattr("factcat_app.extras.importlib.util.find_spec", boom)
    monkeypatch.setattr("factcat_app.extras.shutil.which", lambda name: "/opt/bin/uv")
    monkeypatch.setattr("factcat_app.extras.editable_origin", lambda: None)
    assert install_argv("bigquery")[0] == "/opt/bin/uv"


def test_no_pip_and_no_uv_still_names_a_command(monkeypatch):
    """Nothing to install with is not a crash: the page still shows a command
    the reader can run themselves."""
    monkeypatch.setattr("factcat_app.extras.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("factcat_app.extras.shutil.which", lambda name: None)
    monkeypatch.setattr("factcat_app.extras.editable_origin", lambda: None)
    argv = install_argv("bigquery")
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "pip", "install"]
    assert argv[-1] == "factcat[bigquery]"
    assert "factcat[bigquery]" in install_command("bigquery")


def test_unknown_kind_does_not_build_argv():
    with pytest.raises(ValueError, match="unknown warehouse extra"):
        install_argv("os.system")


def test_editable_install_uses_checkout(monkeypatch, tmp_path):
    origin = tmp_path / "packages" / "engine"
    origin.mkdir(parents=True)
    monkeypatch.setattr("factcat_app.extras.editable_origin", lambda: origin)
    argv = install_argv("bigquery")
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "pip", "install"]
    assert argv[-2] == "-e"
    assert argv[-1] == f"{origin}[bigquery]"
    assert "factcat[bigquery]" not in argv


def test_launch_command_is_factcat():
    from pathlib import Path

    engine = Path(__file__).resolve().parents[1]
    pyproject = (engine / "pyproject.toml").read_text(encoding="utf-8")
    assert 'factcat = "factcat_app.__main__:main"' in pyproject
    assert "factcat-app" not in pyproject
    readme = engine.parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "\nfactcat\n" in text
    assert "factcat-app" not in text


def test_origin_follows_imported_package(monkeypatch, tmp_path):
    engine = tmp_path / "engine"
    pkg = engine / "factcat"
    pkg.mkdir(parents=True)
    (engine / "pyproject.toml").write_text('name = "factcat"\n', encoding="utf-8")
    init = pkg / "__init__.py"
    init.write_text("", encoding="utf-8")
    monkeypatch.setattr("factcat.__file__", str(init))
    from factcat_app.extras import editable_origin

    assert editable_origin() == engine.resolve()


def test_origin_ignores_dist_info_other_worktree(monkeypatch, tmp_path):
    running = tmp_path / "factcat" / "packages" / "engine"
    other = tmp_path / "factcat-other" / "packages" / "engine"
    pkg = running / "factcat"
    pkg.mkdir(parents=True)
    other.mkdir(parents=True)
    (running / "pyproject.toml").write_text('name = "factcat"\n', encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr("factcat.__file__", str(pkg / "__init__.py"))
    from factcat_app.extras import editable_origin

    assert editable_origin() == running.resolve()
    assert "factcat-other" not in str(editable_origin())


def test_origin_none_in_site_packages(monkeypatch, tmp_path):
    site = tmp_path / "site-packages" / "factcat"
    site.mkdir(parents=True)
    init = site / "__init__.py"
    init.write_text("", encoding="utf-8")
    monkeypatch.setattr("factcat.__file__", str(init))
    from factcat_app.extras import editable_origin

    assert editable_origin() is None


def test_get_setup_does_not_pip(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "")
    calls: list = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("GET /setup must not pip")

    monkeypatch.setattr("factcat_app.extras.subprocess.run", boom)
    client = TestClient(app)
    res = client.get("/setup")
    assert res.status_code == 200
    assert calls == []
    assert 'id="extra-card"' in res.text
    assert 'id="extra-install"' in res.text
    assert "/api/install_extra" in res.text
    assert ">Install<" in res.text
    assert res.text.find('id="extra-card"') < res.text.find('class="setup-cols"')
    assert res.text.find('id="extra-card"') < res.text.find('id="f"')
    for kind in ADAPTERS:
        assert f"factcat[{kind}]" in res.text or f"[{kind}]" in res.text


def test_setup_card_copy_when_extra_missing(monkeypatch, tmp_path, py_pi_install):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.main.bootstrap_project", lambda: "")
    monkeypatch.setattr(
        "factcat.warehouses.extra_installed",
        lambda kind: kind != "snowflake",
    )
    client = TestClient(app)
    res = client.get("/setup")
    assert res.status_code == 200
    assert "factcat[snowflake]" in res.text
    assert '"snowflake":false' in res.text.replace(" ", "")


def test_post_unknown_kind_does_not_pip(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    calls: list = []
    monkeypatch.setattr(
        "factcat_app.extras.subprocess.run",
        lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    client = TestClient(app)
    res = client.post("/api/install_extra", json={"kind": "os.system"})
    assert res.status_code == 400
    assert res.json()["ok"] is False
    assert calls == []
    assert "os.system" not in json.dumps(res.json())


def test_post_install_success(monkeypatch, tmp_path, py_pi_install):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    present = {kind: False for kind in ADAPTERS}
    monkeypatch.setattr(
        "factcat_app.extras.extra_installed",
        lambda kind: present[kind],
    )
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs.get("shell") is False
        spec = argv[-1]
        kind = spec[spec.index("[") + 1 : spec.index("]")]
        present[kind] = True
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("factcat_app.extras.subprocess.run", fake_run)
    client = TestClient(app)
    res = client.post("/api/install_extra", json={"kind": "snowflake"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == sys.executable
    assert calls[0][1:4] == ["-m", "pip", "install"]
    assert calls[0][-1] == "factcat[snowflake]"
    assert "factcat[snowflake]" in body["command"]


def test_post_install_locked_does_not_retry(monkeypatch, tmp_path, py_pi_install):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.extras.extra_installed", lambda kind: False)
    calls: list[int] = []

    def fake_run(argv, **kwargs):
        calls.append(1)
        assert "--break-system-packages" not in argv
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="error: externally-managed-environment\n",
        )

    monkeypatch.setattr("factcat_app.extras.subprocess.run", fake_run)
    client = TestClient(app)
    res = client.post("/api/install_extra", json={"kind": "bigquery"})
    assert res.status_code == 400
    body = res.json()
    assert body["ok"] is False
    assert "externally-managed-environment" in body["error"]
    assert "factcat[bigquery]" in body["command"]
    assert len(calls) == 1


def test_already_installed_skips_pip(monkeypatch, tmp_path, py_pi_install):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr("factcat_app.extras.extra_installed", lambda kind: True)
    calls: list = []
    monkeypatch.setattr(
        "factcat_app.extras.subprocess.run",
        lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    client = TestClient(app)
    res = client.post("/api/install_extra", json={"kind": "bigquery"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert calls == []


def test_catalog_import_error_includes_missing_extra(monkeypatch, tmp_path, py_pi_install):
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(
        "factcat_app.main.datasets_from_form",
        lambda form: (_ for _ in ()).throw(
            ImportError("Install it with: pip install factcat[snowflake]")
        ),
    )
    monkeypatch.setattr("factcat_app.main.extra_installed", lambda kind: False)
    client = TestClient(app)
    res = client.post("/api/datasets", json={"kind": "snowflake"})
    assert res.status_code == 400
    body = res.json()
    assert body["missing_extra"] == "snowflake"
    assert "factcat[snowflake]" in body["command"]


def test_run_install_timeout(monkeypatch, py_pi_install):
    monkeypatch.setattr("factcat_app.extras.extra_installed", lambda kind: False)

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr("factcat_app.extras.subprocess.run", boom)
    result = run_install("snowflake")
    assert result["ok"] is False
    assert "timed out" in str(result["error"])
    assert "factcat[snowflake]" in result["command"]


def test_install_command_is_argv_joined(py_pi_install):
    argv = install_argv("bigquery")
    assert install_command("bigquery")
    assert "pip" in install_command("bigquery")
    assert argv[-1] in install_command("bigquery")


def test_pip_ok_but_driver_still_missing(monkeypatch, py_pi_install):
    monkeypatch.setattr("factcat_app.extras.extra_installed", lambda kind: False)
    monkeypatch.setattr(
        "factcat_app.extras.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    result = run_install("snowflake")
    assert result["ok"] is False
    assert "still does not import" in str(result["error"])
