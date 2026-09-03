"""What build is this process serving?

A preview served from one worktree while the reviewer's tab holds the page
from another (or from ten minutes ago) is the recurring failure this module
exists to end: the owner tested a fix for an hour against a tab that had
never reloaded, and neither of us could tell, because nothing the page shows
identifies the code behind it. `Cache-Control: no-store` does not help —
it stops a cached copy being *served*, not an open tab from being *old*.

The id is a hash of the source the server actually imported: the tracked
git head when there is one, plus every Python, template and static file's
path, size and mtime. That moves on a commit, a checkout, an edit, and a
different worktree — the four ways the answer goes wrong. Computed once at
import: it describes the code this process is running, which is the whole
point, so it must not be recomputed on request.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ENGINE_DIR = APP_DIR.parent
_SUFFIXES = {".py", ".html", ".css", ".js", ".md"}


def _git_head(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _fingerprint() -> str:
    h = hashlib.sha1()
    head = _git_head(ENGINE_DIR)
    h.update(head.encode())
    for base in (APP_DIR, ENGINE_DIR / "factcat"):
        for path in sorted(base.rglob("*")):
            if path.suffix not in _SUFFIXES or "__pycache__" in path.parts:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            h.update(str(path.relative_to(ENGINE_DIR)).encode())
            h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
    short = h.hexdigest()[:10]
    return f"{head}+{short}" if head else short


#: Identifies the code this process imported. Compare a page's copy with the
#: server's to know whether the tab is current; compare a running server's
#: with a fresh computation to know whether the process is.
BUILD_ID = _fingerprint()
#: Which tree it came from, so two previews are never confused for one.
BUILD_ROOT = str(ENGINE_DIR)


def build_info() -> dict[str, str]:
    return {"build": BUILD_ID, "root": BUILD_ROOT, "pid": str(os.getpid())}
