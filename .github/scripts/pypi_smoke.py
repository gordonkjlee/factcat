"""Install factcat==VERSION from PyPI and smoke-test it.

Success is ``pip install`` plus imports in a throwaway venv, retried until a
deadline. A one-shot GET of the JSON API is not enough: that endpoint can
return 200 and then fail on the next curl, which is how a successful upload
was reported as a failed release.
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


def install_and_smoke(version: str, *, python: str = sys.executable) -> None:
    """Create a venv, install ``factcat==version``, run the smoke. Raises RuntimeError."""
    tmp = tempfile.mkdtemp(prefix="factcat-smoke-")
    try:
        venv = os.path.join(tmp, "venv")
        created = subprocess.run(
            [python, "-m", "venv", venv],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            raise RuntimeError(created.stderr or created.stdout or "venv failed")
        py = _venv_python(venv)
        installed = subprocess.run(
            [py, "-m", "pip", "install", f"factcat=={version}"],
            capture_output=True,
            text=True,
        )
        if installed.returncode != 0:
            raise RuntimeError(installed.stderr or installed.stdout or "pip install failed")
        smoked = subprocess.run(
            [py, "-c", SMOKE],
            capture_output=True,
            text=True,
        )
        if smoked.returncode != 0:
            raise RuntimeError(smoked.stderr or smoked.stdout or "smoke failed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def wait_until_installable(
    version: str,
    *,
    deadline_s: int = 300,
    sleep_s: int = 20,
    attempt: Callable[[], None] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Retry ``attempt`` (default: install_and_smoke) until it succeeds or time is up."""
    do = attempt or (lambda: install_and_smoke(version))
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
    parser.add_argument("--deadline-s", type=int, default=300)
    parser.add_argument("--sleep-s", type=int, default=20)
    args = parser.parse_args(argv)
    wait_until_installable(
        args.version, deadline_s=args.deadline_s, sleep_s=args.sleep_s
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
