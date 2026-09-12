"""Shared fixtures.

The payments table is the fixture for the whole suite because it is the shape
the library exists for: a subscription business whose retention definition no
product analytics tool can express.
"""

from __future__ import annotations

import logging

import duckdb
import pytest


@pytest.fixture(autouse=True)
def isolate_user_state(tmp_path, monkeypatch):
    """No test resolves the real mapping or the real preferences.

    `config_path()` falls back to a CWD-relative `.factcat.json`, which in this
    directory is a production mapping with managed tables on automatic; the
    per-test setenv lines in test_app.py were the only thing standing between
    the suite and it. Mutation: drop the FACTCAT_CONFIG line and
    test_no_test_can_resolve_the_real_mapping goes red.
    """
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "factcat.json"))


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture()
def sqlglot_warnings():
    logger = logging.getLogger("sqlglot")
    handler = _Capture()
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)



# subscription_id, user_id, sub_start, paid_at, status
PAYMENTS = [
    # S1 pays on time in every period.
    ("S1", "U1", "2026-01-01", "2026-01-01", "collected"),
    ("S1", "U1", "2026-01-01", "2026-02-05", "collected"),  # day 35, offset 0
    ("S1", "U1", "2026-01-01", "2026-03-12", "collected"),  # day 70, offset 0
    # S2 pays period 1 three days late - INSIDE the 5-day dunning window.
    ("S2", "U1", "2026-01-01", "2026-01-01", "collected"),
    ("S2", "U1", "2026-01-01", "2026-02-08", "collected"),  # day 38, offset 3
    # S3 pays period 1 six days late - OUTSIDE the window. Churned.
    ("S3", "U2", "2026-01-15", "2026-01-15", "collected"),
    ("S3", "U2", "2026-01-15", "2026-02-25", "collected"),  # day 41, offset 6
    # S4 is the February cohort, with one collected payment.
    ("S4", "U3", "2026-02-01", "2026-02-01", "collected"),
    # A failed payment must never count as retention.
    ("S4", "U3", "2026-02-01", "2026-03-08", "failed"),  # day 35, offset 0
]

DUNNING_DAYS = 5
RETAINED = f"status = 'collected' AND within_period_offset <= {DUNNING_DAYS}"


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE payments ("
        "  subscription_id VARCHAR,"
        "  user_id VARCHAR,"
        "  sub_start DATE,"
        "  paid_at DATE,"
        "  status VARCHAR"
        ")"
    )
    connection.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, ?)", PAYMENTS)
    return connection
