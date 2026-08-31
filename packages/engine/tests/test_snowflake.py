"""Snowflake adapter: mock the official client, no network."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from factcat.warehouses import AdapterError, DryRunNotSupported, connect
from factcat.warehouses.snowflake import SnowflakeAdapter, _load_snowflake


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
    adapter = _adapter(private_key_path=snowflake_stack.key)
    result = adapter.run("SELECT 1 AS n")
    snowflake_stack.cur.execute.assert_called_once_with("SELECT 1 AS n")
    assert result.rows == [{"N": 1}]
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
