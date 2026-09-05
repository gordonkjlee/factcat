"""What build is this process serving?

A preview served from one worktree while the reviewer's tab holds the page
from another (or from ten minutes ago) is the recurring failure this module
exists to end: the owner tested a fix for an hour against a tab that had
never reloaded, and neither of us could tell, because nothing the page shows
identifies the code behind it. `Cache-Control: no-store` does not help —
it stops a cached copy being *served*, not an open tab from being *old*.

The id is a hash of the source the server actually imported: the tracked
git head when there is one, plus the path and CONTENT of every Python,
template and static file. Content, not mtime: `git checkout --` rewrites
the timestamp of a file it restores byte for byte, so an mtime hash made
every mutation test look like a new build. Identical code must give an
identical id, or the check cries wolf and gets ignored — which is the only
way a check like this fails. It moves on a commit, an edit and a different
worktree, which are the ways the answer actually goes wrong. Computed once
at import: it describes the code this process is running, so recomputing it
per request would describe something else.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .config import config_path

APP_DIR = Path(__file__).resolve().parent
ENGINE_DIR = APP_DIR.parent
_SUFFIXES = {".py", ".html", ".css", ".js", ".md"}


def _git_head() -> str:
    """The head of OUR checkout, or "" - never a repo that merely happens to
    be an ancestor. `pip install factcat` under someone's project directory
    would otherwise pick up their head and change the id on every commit
    they make, so the staleness bar would fire on tabs that are not stale."""
    repo = ENGINE_DIR.parents[1]
    if not (repo / ".git").exists():
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _fingerprint() -> str:
    h = hashlib.sha1()
    head = _git_head()
    h.update(head.encode())
    for base in (APP_DIR, ENGINE_DIR / "factcat"):
        for path in sorted(base.rglob("*")):
            if path.suffix not in _SUFFIXES or "__pycache__" in path.parts:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            h.update(str(path.relative_to(ENGINE_DIR)).encode())
            h.update(hashlib.sha1(data).digest())
    short = h.hexdigest()[:10]
    return f"{head}+{short}" if head else short


#: Identifies the code this process imported. Compare a page's copy with the
#: server's to know whether the tab is current; compare a running server's
#: with a fresh computation to know whether the process is.
BUILD_ID = _fingerprint()
#: Which tree it came from, as an opaque id. Two previews must be
#: distinguishable, but the answer is published by an unauthenticated
#: endpoint, and the absolute path is the owner's directory layout - free
#: reconnaissance if the app is ever bound off localhost. A hash separates
#: worktrees without describing the machine.
BUILD_ROOT_ID = hashlib.sha1(str(ENGINE_DIR).encode()).hexdigest()[:8]


def build_info() -> dict[str, str]:
    # The mapping's basename, never its path: a preview must be able to say
    # DEV or PROD, and the endpoint is unauthenticated.
    return {"build": BUILD_ID, "root_id": BUILD_ROOT_ID, "config": config_path().name}


def root_id_for(path: str | Path) -> str:
    """The same id for a path a caller holds, so a tool can ask "is that my
    tree?" without the server ever sending one."""
    return hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:8]
