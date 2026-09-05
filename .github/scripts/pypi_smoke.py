"""Install factcat==VERSION from PyPI and smoke-test it.

Success is an install plus imports, retried until a deadline. A one-shot GET
of the JSON API is not enough: that endpoint can return 200 and then fail on
the next curl, which is how a successful upload was reported as a failed
release.

``--installer pip`` installs into a throwaway venv; ``--installer uv`` goes
through ``uv tool``, the isolated install the README offers, so the console
script is exercised the way that path puts it on PATH.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable

SMOKE = r"""
import importlib.metadata
from factcat import EventsSpec, RetentionSpec, events_sql, retention_sql
from factcat_app.query import spec_from_form

spec = RetentionSpec(
    table="payments",
    entity="subscription_id",
    entity_time="sub_start",
    event_time="paid_at",
    period_days=35,
    n_periods=2,
    retained="status = 'collected' AND within_period_offset <= 5",
)
sql = retention_sql(spec, dialect="snowflake")
assert "factcat_period_grid" in sql, "period grid missing from generated SQL"

events = events_sql(
    EventsSpec(
        table="events",
        entity="subscription_id",
        event_time="occurred_at",
        measure="uniques",
    ),
    dialect="bigquery",
)
assert "APPROX_COUNT_DISTINCT" in events.upper()

form = spec_from_form({
    "table": "analytics.events",
    "entity": "subscription_id",
    "event_time": "occurred_at",
    "measure": "uniques",
    "grain": "day",
    "lookback_days": 7,
})
assert form.entity == "subscription_id"

eps = importlib.metadata.entry_points()
group = eps.select(group="console_scripts") if hasattr(eps, "select") else eps.get("console_scripts", [])
assert any(ep.name == "factcat" for ep in group), "factcat console script missing"
print("Smoke test passed: factcat from PyPI generates SQL and has the factcat script.")
"""


def _venv_python(venv: str) -> str:
    if os.name == "nt":
        return os.path.join(venv, "Scripts", "python.exe")
    return os.path.join(venv, "bin", "python")


INSTALLERS = ("pip", "uv")


def requirement(version: str, extras: str = "") -> str:
    """``factcat[all]==0.5.1`` or ``factcat==0.5.1``."""
    return f"factcat[{extras}]=={version}" if extras else f"factcat=={version}"


def install_plan(
    version: str,
    extras: str = "",
    installer: str = "pip",
    *,
    python: str = sys.executable,
) -> list[list[str]]:
    """The argv lists to run, in order: install, then the smoke.

    Pure so the tests can pin the exact commands without a network. ``python``
    is the interpreter of the venv the pip leg installs into; uv manages its
    own environment and ignores it.
    """
    req = requirement(version, extras)
    if installer == "pip":
        return [
            [python, "-m", "pip", "install", req],
            [python, "-c", SMOKE],
        ]
    if installer == "uv":
        return [
            ["uv", "tool", "install", req],
            ["uv", "tool", "run", "--from", req, "python", "-c", SMOKE],
        ]
    raise ValueError(f"installer must be one of {INSTALLERS}, got {installer!r}")


def _run(argv: list[str], what: str) -> None:
    done = subprocess.run(argv, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(done.stderr or done.stdout or f"{what} failed")


def install_and_smoke(
    version: str,
    *,
    extras: str = "",
    installer: str = "pip",
    python: str = sys.executable,
) -> None:
    """Install ``factcat==version`` the way ``installer`` would, run the smoke. Raises RuntimeError."""
    if installer != "pip":
        for argv in install_plan(version, extras, installer):
            _run(argv, argv[0])
        return
    tmp = tempfile.mkdtemp(prefix="factcat-smoke-")
    try:
        venv = os.path.join(tmp, "venv")
        _run([python, "-m", "venv", venv], "venv")
        for argv in install_plan(version, extras, installer, python=_venv_python(venv)):
            _run(argv, "pip install" if "install" in argv else "smoke")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def wait_until_installable(
    version: str,
    *,
    extras: str = "",
    installer: str = "pip",
    deadline_s: int = 300,
    sleep_s: int = 20,
    attempt: Callable[[], None] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Retry ``attempt`` (default: install_and_smoke) until it succeeds or time is up."""
    do = attempt or (
        lambda: install_and_smoke(version, extras=extras, installer=installer)
    )
    deadline = now() + deadline_s
    last_err = "not attempted"
    while True:
        try:
            do()
            return
        except Exception as exc:
            last_err = str(exc).strip() or type(exc).__name__
        remaining = deadline - now()
        if remaining <= 0:
            break
        sleeper(min(sleep_s, remaining))
    raise SystemExit(
        f"factcat {version} was not installable from PyPI within {deadline_s}s: {last_err}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--extras", default="", help="comma-separated extras, e.g. all")
    parser.add_argument("--installer", choices=INSTALLERS, default="pip")
    parser.add_argument("--deadline-s", type=int, default=300)
    parser.add_argument("--sleep-s", type=int, default=20)
    args = parser.parse_args(argv)
    wait_until_installable(
        args.version,
        extras=args.extras,
        installer=args.installer,
        deadline_s=args.deadline_s,
        sleep_s=args.sleep_s,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
