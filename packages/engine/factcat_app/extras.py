"""Install a warehouse extra into this interpreter. Never from connect()."""

from __future__ import annotations

import importlib
import importlib.util
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from factcat.warehouses import ADAPTERS, extra_installed, extra_requirement

PIP_TIMEOUT = 600.0


def extra_commands() -> dict[str, str]:
    return {kind: install_command(kind) for kind in ADAPTERS}


def editable_origin() -> Path | None:
    """Engine root of the *imported* ``factcat``, if this is a source tree.

    Dist-info ``direct_url.json`` can name a different worktree when several
    checkouts share a venv. ``factcat.__file__`` is the code that is running.
    A wheel in site-packages has no sibling ``pyproject.toml``.
    """
    import factcat

    root = Path(factcat.__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    if 'name = "factcat"' not in text and "name = 'factcat'" not in text:
        return None
    return root


def _installer() -> list[str]:
    """The install front-end that can reach this interpreter. An isolated
    install (``uv tool``, ``pipx``) need not carry pip."""
    try:
        has_pip = importlib.util.find_spec("pip") is not None
    except (ImportError, ValueError):
        # A stale sys.modules entry raises rather than answering. This runs
        # while Setup renders, so a raise here would be a dead page instead
        # of a failed install.
        has_pip = False
    if has_pip:
        return [sys.executable, "-m", "pip", "install"]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable]
    return [sys.executable, "-m", "pip", "install"]


def install_argv(kind: str) -> list[str]:
    """Argv to install this kind's extra into this interpreter. Not a shell."""
    if kind not in ADAPTERS:
        raise ValueError(f"unknown warehouse extra: {kind!r}")
    argv = list(_installer())
    origin = editable_origin()
    if origin is not None:
        argv.extend(["-e", f"{origin}[{kind}]"])
    else:
        argv.append(extra_requirement(kind))
    return argv


def install_command(kind: str) -> str:
    return shlex.join(install_argv(kind))


def _forget_failed_imports() -> None:
    for name, mod in list(sys.modules.items()):
        if mod is None:
            del sys.modules[name]


def run_install(kind: str) -> dict[str, str | bool]:
    """Install ``factcat[<kind>]`` into this interpreter after an explicit yes.

    Does not retry with ``--break-system-packages``. Does not install for an
    unknown kind.
    """
    argv = install_argv(kind)
    command = shlex.join(argv)
    if extra_installed(kind):
        return {"ok": True, "command": command}
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PIP_TIMEOUT,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "command": command, "error": str(exc)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "command": command, "error": "the install timed out"}
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "the install failed").strip()
        return {"ok": False, "command": command, "error": err[-4000:]}
    importlib.invalidate_caches()
    _forget_failed_imports()
    if not extra_installed(kind):
        return {
            "ok": False,
            "command": command,
            "error": "the install succeeded but the driver still does not import",
        }
    return {"ok": True, "command": command}
