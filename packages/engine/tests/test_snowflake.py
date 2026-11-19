"""Snowflake adapter: mock the official client, no network."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from factcat.warehouses import AdapterError, DryRunNotSupported, connect
from factcat.warehouses.snowflake import (
    SnowflakeAdapter,
    _load_snowflake,
    WarehouseNotUsable,
    can_run_statement,
    list_roles,
    list_warehouses,
)


def _adapter(**extra):
    kwargs = dict(
        account="xy12345",
        user="ANALYST",
        warehouse="COMPUTE_WH",
        database="ANALYTICS",
        schema="MARTS",
        private_key_path="rsa_key.p8",
    )
    kwargs.update(extra)
    return SnowflakeAdapter(**kwargs)


@pytest.fixture()
def snowflake_stack(monkeypatch, tmp_path):
    key = tmp_path / "rsa_key.p8"
    key.write_text("-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n", encoding="utf-8")
    connector = MagicMock()
    ctx = MagicMock()
    cur = MagicMock()
    cur.description = [("N",)]
    cur.fetchall.return_value = [(1,)]
    cur.sfqid = "sf-job-1"
    ctx.cursor.return_value = cur
    connector.connect.return_value = ctx
    monkeypatch.setattr(
        "factcat.warehouses.snowflake._load_snowflake", lambda: connector
    )
    return SimpleNamespace(connector=connector, ctx=ctx, cur=cur, key=str(key))


def test_dry_run_raises_without_executing(snowflake_stack):
    adapter = _adapter(private_key_path=snowflake_stack.key)
    with pytest.raises(DryRunNotSupported):
        adapter.run("SELECT 1", dry_run=True)
    snowflake_stack.connector.connect.assert_not_called()


def test_run_returns_dicts(snowflake_stack):
    """Keys come back as written, not as Snowflake reports them.

    The driver hands over `N` for `SELECT 1 AS n`, because Snowflake resolves
    unquoted identifiers upper-cased. Confirmed against a real account on
    2026-09-06: `cur.description[0][0]` for `SELECT 1 AS fc_rows` was `'FC_ROWS'`. Every consumer reads by the name it
    generated - `row.get("bucket")`, `row.get("fc_bookmark")` - so an
    unfolded key is None, `int(None or 0)` is a silent zero, and charts render
    empty with no error anywhere. Every identifier this project generates is
    unquoted, so lowering cannot collide.

    Mutation: hand back `col[0]` unchanged.
    """
    adapter = _adapter(private_key_path=snowflake_stack.key)
    result = adapter.run("SELECT 1 AS n")
    snowflake_stack.cur.execute.assert_called_once_with("SELECT 1 AS n")
    assert result.rows == [{"n": 1}]
    assert result.bytes_processed is None
    assert result.bytes_billed is None
    assert result.job_id == "sf-job-1"
    snowflake_stack.cur.close.assert_called()
    snowflake_stack.ctx.close.assert_called()


def test_connect_kwargs_use_jwt_and_key_file(snowflake_stack):
    adapter = _adapter(private_key_path=snowflake_stack.key)
    adapter.run("SELECT 1")
    kwargs = snowflake_stack.connector.connect.call_args.kwargs
    assert kwargs["authenticator"] == "SNOWFLAKE_JWT"
    assert kwargs["private_key_file"] == snowflake_stack.key
    assert kwargs["account"] == "xy12345"
    assert "password" not in kwargs
    assert "project" not in kwargs


def test_connect_kwargs_set_statement_timeout(snowflake_stack):
    """The connection timeout is also the server-side statement ceiling.

    `login_timeout` and `network_timeout` only make the client stop
    waiting; the warehouse keeps running the statement and keeps billing.
    `STATEMENT_TIMEOUT_IN_SECONDS` is the only spend bound Snowflake gives
    us, so it must ride on every session with the same number.

    Mutation: drop the `session_parameters` key.
    """
    adapter = _adapter(private_key_path=snowflake_stack.key, timeout=90)
    adapter.run("SELECT 1")
    kwargs = snowflake_stack.connector.connect.call_args.kwargs
    assert kwargs["session_parameters"] == {"STATEMENT_TIMEOUT_IN_SECONDS": 90}
    assert kwargs["login_timeout"] == 90
    assert kwargs["network_timeout"] == 90


def test_missing_key_file_is_adapter_error(tmp_path):
    adapter = _adapter(private_key_path=str(tmp_path / "missing.p8"))
    with pytest.raises(AdapterError, match="not found"):
        adapter.run("SELECT 1")


def test_connect_factory_uses_snowflake_fields(snowflake_stack):
    adapter = connect(
        "snowflake",
        account="xy12345",
        user="ANALYST",
        warehouse="COMPUTE_WH",
        database="ANALYTICS",
        schema="MARTS",
        private_key_path=snowflake_stack.key,
    )
    adapter.run("SELECT 1")
    assert snowflake_stack.connector.connect.called


def test_required_fields():
    with pytest.raises(ValueError, match="account"):
        _adapter(account="")
    with pytest.raises(ValueError, match="warehouse"):
        _adapter(warehouse="")


def test_externalbrowser_does_not_need_a_key(snowflake_stack):
    adapter = _adapter(
        private_key_path="",
        authenticator="externalbrowser",
    )
    adapter.run("SELECT 1")
    kwargs = snowflake_stack.connector.connect.call_args.kwargs
    assert kwargs["authenticator"] == "externalbrowser"
    assert kwargs["client_store_temporary_credential"] is True
    assert "private_key_file" not in kwargs


def test_key_pair_still_requires_a_file():
    with pytest.raises(ValueError, match="private_key_path"):
        _adapter(private_key_path="", authenticator="key_pair")


def test_list_warehouses_does_not_need_compute_warehouse(snowflake_stack):
    snowflake_stack.cur.description = [("name",), ("is_default",)]
    snowflake_stack.cur.fetchall.return_value = [
        ("LOAD_WH", "N"),
        ("COMPUTE_WH", "Y"),
    ]
    payload = list_warehouses(
        account="xy12345",
        user="ANALYST",
        private_key_path=snowflake_stack.key,
        role="ANALYST",
    )
    kwargs = snowflake_stack.connector.connect.call_args.kwargs
    assert "warehouse" not in kwargs
    assert kwargs["role"] == "ANALYST"
    snowflake_stack.cur.execute.assert_called_with("SHOW WAREHOUSES")
    assert payload["warehouses"] == ["COMPUTE_WH", "LOAD_WH"]
    assert payload["default"] == "COMPUTE_WH"


def test_list_roles_uses_grants_to_user(snowflake_stack):
    snowflake_stack.cur.description = [("role",)]
    snowflake_stack.cur.fetchall.return_value = [("PUBLIC",), ("ANALYST",)]
    names = list_roles(
        account="xy12345",
        user="ANALYST",
        private_key_path=snowflake_stack.key,
    )
    kwargs = snowflake_stack.connector.connect.call_args.kwargs
    assert "warehouse" not in kwargs
    assert "role" not in kwargs
    sql = snowflake_stack.cur.execute.call_args[0][0]
    assert sql.startswith("SHOW GRANTS TO USER")
    assert "ANALYST" in sql
    assert names == ["ANALYST", "PUBLIC"]


def test_missing_extra_names_install(monkeypatch, tmp_path):
    def boom(name: str):
        raise ImportError("simulated missing extra")

    monkeypatch.setattr(
        "factcat.warehouses.snowflake.importlib.import_module", boom
    )
    with pytest.raises(ImportError, match=r"factcat\[snowflake\]"):
        _load_snowflake()
    key = tmp_path / "rsa_key.p8"
    key.write_text("-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n", encoding="utf-8")
    adapter = _adapter(private_key_path=str(key))
    with pytest.raises(ImportError, match=r"factcat\[snowflake\]"):
        adapter.run("SELECT 1")


def test_can_run_statement_selects_one_on_the_chosen_warehouse_and_role(snowflake_stack):
    """The zero-byte analogue of BigQuery's jobs.create probe."""
    snowflake_stack.cur.description = [("fc_role",), ("fc_wh",)]
    snowflake_stack.cur.fetchall.return_value = [("ANALYST", "COMPUTE_WH")]
    got = can_run_statement(
        account="xy12345",
        user="ANALYST",
        warehouse="COMPUTE_WH",
        private_key_path=snowflake_stack.key,
        role="ANALYST",
    )
    kwargs = snowflake_stack.connector.connect.call_args.kwargs
    assert kwargs["warehouse"] == "COMPUTE_WH" and kwargs["role"] == "ANALYST"
    sqls = [c[0][0] for c in snowflake_stack.cur.execute.call_args_list]
    assert sqls[0] == "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 1"
    assert sqls[1].startswith("SELECT CURRENT_ROLE()")
    assert got == {"role": "ANALYST", "warehouse": "COMPUTE_WH"}


def test_can_run_statement_refusal_is_an_adapter_error(snowflake_stack):
    snowflake_stack.cur.execute.side_effect = RuntimeError("No active warehouse selected")
    with pytest.raises(AdapterError, match="No active warehouse"):
        can_run_statement(
            account="xy12345",
            user="ANALYST",
            warehouse="COMPUTE_WH",
            private_key_path=snowflake_stack.key,
        )
    assert snowflake_stack.ctx.close.called


def test_can_run_statement_needs_a_warehouse(snowflake_stack):
    with pytest.raises(ValueError, match="warehouse"):
        can_run_statement(account="xy12345", user="U", private_key_path=snowflake_stack.key)


def test_can_run_statement_refuses_a_session_with_no_warehouse(snowflake_stack):
    """A role without USAGE still connects; the warehouse just never binds.

    Snowflake opens the session with ``CURRENT_WAREHOUSE()`` NULL and still
    answers ``CURRENT_ROLE()``, so a probe that only checks the statement ran
    would say ok for a role that cannot run a single query.

    Mutation: drop the warehouse read-back; this goes green-to-red.
    """
    snowflake_stack.cur.description = [("FC_ROLE",), ("FC_WH",)]
    snowflake_stack.cur.fetchall.return_value = [("ANALYST", None)]
    with pytest.raises(WarehouseNotUsable, match="no USAGE on warehouse COMPUTE_WH"):
        can_run_statement(
            account="xy12345",
            user="ANALYST",
            warehouse="COMPUTE_WH",
            private_key_path=snowflake_stack.key,
            role="ANALYST",
        )


def test_can_run_statement_refuses_a_fallback_to_another_warehouse(snowflake_stack):
    """A user DEFAULT_WAREHOUSE that binds instead is not the chosen one."""
    snowflake_stack.cur.description = [("FC_ROLE",), ("FC_WH",)]
    snowflake_stack.cur.fetchall.return_value = [("ANALYST", "DEFAULT_WH")]
    with pytest.raises(WarehouseNotUsable, match="warehouse DEFAULT_WH"):
        can_run_statement(
            account="xy12345",
            user="ANALYST",
            warehouse="COMPUTE_WH",
            private_key_path=snowflake_stack.key,
        )


def test_can_run_statement_matches_a_quoted_or_lower_case_pick(snowflake_stack):
    snowflake_stack.cur.description = [("FC_ROLE",), ("FC_WH",)]
    snowflake_stack.cur.fetchall.return_value = [("ANALYST", "COMPUTE_WH")]
    for picked in ('"COMPUTE_WH"', "compute_wh"):
        got = can_run_statement(
            account="xy12345",
            user="ANALYST",
            warehouse=picked,
            private_key_path=snowflake_stack.key,
        )
        assert got["warehouse"] == "COMPUTE_WH"
