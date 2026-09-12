"""The root README and the PyPI page make the same claims.

The PyPI long description is the README beside pyproject.toml; the root README
is the long document. They ship on different pages and have drifted before.

Mutation: delete the Snowflake "experimental" caveat from the engine README
-> red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
READMES = {"root": ENGINE.parents[1] / "README.md", "pypi": ENGINE / "README.md"}


@pytest.fixture(params=sorted(READMES), ids=sorted(READMES))
def readme(request: pytest.FixtureRequest) -> str:
    return READMES[request.param].read_text(encoding="utf-8")


def test_snowflake_is_experimental_wherever_it_is_named(readme: str):
    assert "Snowflake" in readme
    assert "experimental" in readme


def test_managed_tables_are_disclosed(readme: str):
    assert "fc_column_index" in readme or "managed tables" in readme.lower()


def test_pypi_page_names_the_launch_path():
    text = READMES["pypi"].read_text(encoding="utf-8")
    assert "\nfactcat\n" in text  # the console script, as the command to run
    assert "http://127.0.0.1:8000" in text
