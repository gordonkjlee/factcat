"""Unit tests for the PyPI smoke waiter. No network."""

from __future__ import annotations

import pytest

from pypi_smoke import SMOKE, install_plan, wait_until_installable


def test_succeeds_on_first_attempt():
    calls = {"n": 0}

    def attempt():
        calls["n"] += 1

    sleeps: list[float] = []
    wait_until_installable(
        "0.4.1",
        attempt=attempt,
        sleeper=sleeps.append,
        now=lambda: 0.0,
        deadline_s=300,
    )
    assert calls["n"] == 1
    assert sleeps == []


def test_retries_then_succeeds():
    calls = {"n": 0}
    clock = {"t": 0.0}

    def attempt():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("404")

    def now():
        return clock["t"]

    def sleeper(seconds):
        clock["t"] += seconds

    wait_until_installable(
        "0.4.1",
        attempt=attempt,
        now=now,
        sleeper=sleeper,
        deadline_s=300,
        sleep_s=20,
    )
    assert calls["n"] == 3


def test_times_out_with_last_error():
    clock = {"t": 0.0}

    def attempt():
        raise RuntimeError("Could not find a version that satisfies")

    def now():
        return clock["t"]

    def sleeper(seconds):
        clock["t"] += seconds

    with pytest.raises(SystemExit, match="Could not find a version that satisfies"):
        wait_until_installable(
            "0.4.0",
            attempt=attempt,
            now=now,
            sleeper=sleeper,
            deadline_s=40,
            sleep_s=20,
        )


def test_json_flap_is_irrelevant_if_install_works():
    """A JSON GET that later 404s must not fail the waiter; install is the signal."""

    def attempt():
        return None

    wait_until_installable("0.4.1", attempt=attempt, now=lambda: 0.0, sleeper=lambda s: None)


def test_install_plan_pip_installs_extras_then_smokes():
    """The pip leg installs factcat[EXTRAS]==V into the given interpreter, then runs SMOKE there."""
    plan = install_plan("0.5.1", "all", "pip", python="/venv/bin/python")
    assert plan == [
        ["/venv/bin/python", "-m", "pip", "install", "factcat[all]==0.5.1"],
        ["/venv/bin/python", "-c", SMOKE],
    ]
    assert install_plan("0.5.1", "", "pip", python="py")[0][-1] == "factcat==0.5.1"


def test_install_plan_uv_uses_tool_install_and_run():
    """The uv leg pins the same version on both the install and the run, and ignores the venv python."""
    plan = install_plan("0.5.1", "", "uv", python="/venv/bin/python")
    assert plan == [
        ["uv", "tool", "install", "factcat==0.5.1"],
        ["uv", "tool", "run", "--from", "factcat==0.5.1", "python", "-c", SMOKE],
    ]
    with pytest.raises(ValueError, match="installer"):
        install_plan("0.5.1", "", "conda")
