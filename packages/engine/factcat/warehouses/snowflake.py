"""Snowflake execute adapter.

Pushes SQL into the caller's Snowflake account. Account, role, warehouse,
and key-pair auth live on this class, not on ``Adapter``. Official
``snowflake-connector-python`` client. Extra: ``pip install factcat[snowflake]``.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any, ClassVar

from . import AdapterError, DryRunNotSupported, QueryResult

DEFAULT_TIMEOUT = 600.0
_EXTRA = "pip install factcat[snowflake]"
PASSPHRASE_ENV = "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"

# information_schema / DESCRIBE type names. NUMBER(38,0) is normalised
# to NUMBER before membership. VARIANT is not STRING.
ENTITY_TYPES = frozenset(
    {
        "VARCHAR",
        "STRING",
        "TEXT",
        "CHAR",
        "CHARACTER",
        "NUMBER",
        "DECIMAL",
        "NUMERIC",
        "INT",
        "INTEGER",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "BYTEINT",
    }
)
# Instants: UTC storage (LTZ) or UTC + offset (TZ). Session TIMEZONE is
# display only. TIMESTAMP_NTZ / DATETIME are wall-clock with no zone.
INSTANT_TIME_TYPES = frozenset({"TIMESTAMP_TZ", "TIMESTAMP_LTZ"})
WALLCLOCK_TIME_TYPES = frozenset({"TIMESTAMP_NTZ", "TIMESTAMP", "DATETIME"})
TIME_TYPES = INSTANT_TIME_TYPES | WALLCLOCK_TIME_TYPES
UNIX_TIME_TYPES = frozenset(
    {
        "NUMBER",
        "INT",
        "INTEGER",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "BYTEINT",
    }
)
EVENT_NAME_TYPES = frozenset({"VARCHAR", "STRING", "TEXT", "CHAR", "CHARACTER"})
PROPERTY_OF_TYPES = frozenset(
    {
        "NUMBER",
        "DECIMAL",
        "NUMERIC",
        "INT",
        "INTEGER",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "BYTEINT",
        "FLOAT",
        "FLOAT4",
        "FLOAT8",
        "DOUBLE",
        "DOUBLE PRECISION",
        "REAL",
    }
)
DISTINCT_OF_TYPES = PROPERTY_OF_TYPES | EVENT_NAME_TYPES
JSON_TYPES = frozenset({"VARIANT", "OBJECT"})


def _load_snowflake() -> Any:
    try:
        return importlib.import_module("snowflake.connector")
    except ImportError as exc:
        raise ImportError(
            "Snowflake execute adapter requires snowflake-connector-python. "
            f"Install it with: {_EXTRA}"
        ) from exc


def _require_sql(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql must be a non-empty string")
    return sql


def _require_ident(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_get(row: Any, *keys: str) -> Any:
    if isinstance(row, dict):
        lower = {str(k).lower(): v for k, v in row.items()}
        for key in keys:
            if key.lower() in lower:
                return lower[key.lower()]
        return None
    return None


def _normalise_type(field_type: str) -> str:
    raw = (field_type or "").strip().upper()
    if "(" in raw:
        raw = raw.split("(", 1)[0].strip()
    return raw


def passphrase_from_env() -> str | None:
    raw = os.environ.get(PASSPHRASE_ENV, "").strip()
    return raw or None


def _connect_kwargs(
    *,
    account: str,
    user: str,
    warehouse: str | None,
    database: str | None,
    schema: str | None,
    private_key_path: str,
    role: str | None,
    private_key: object | None,
    private_key_passphrase: str | None,
    timeout: float,
    authenticator: str = "key_pair",
) -> dict[str, Any]:
    method = (authenticator or "key_pair").strip().lower()
    kwargs: dict[str, Any] = {
        "account": account,
        "user": user,
        "login_timeout": int(timeout),
        "network_timeout": int(timeout),
    }
    if warehouse:
        kwargs["warehouse"] = warehouse
    if database:
        kwargs["database"] = database
    if schema:
        kwargs["schema"] = schema
    if role:
        kwargs["role"] = role
    if method == "externalbrowser":
        kwargs["authenticator"] = "externalbrowser"
        kwargs["client_store_temporary_credential"] = True
        return kwargs
    kwargs["authenticator"] = "SNOWFLAKE_JWT"
    passphrase = private_key_passphrase if private_key_passphrase is not None else passphrase_from_env()
    if private_key is not None:
        kwargs["private_key"] = private_key
    else:
        path = _require_ident(private_key_path, "private_key_path")
        if not os.path.isfile(path):
            raise AdapterError(f"private key file not found: {path}")
        kwargs["private_key_file"] = path
        if passphrase:
            kwargs["private_key_file_pwd"] = passphrase
    return kwargs


def _open_connection(**kwargs: Any) -> Any:
    connector = _load_snowflake()
    try:
        return connector.connect(**kwargs)
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError(str(exc) or "Snowflake connection failed") from exc


@dataclass(frozen=True)
class SnowflakeAdapter:
    """Run SQL in the caller's Snowflake account.

    Sign-in is a key-pair file (JWT) or browser SSO (``externalbrowser``).
    SSO tokens are cached by the connector (``client_store_temporary_credential``)
    in the OS keyring, not in ``.factcat.json``. Encrypted key passphrase is
    ``private_key_passphrase`` or ``SNOWFLAKE_PRIVATE_KEY_PASSPHRASE``.
    """

    account: str
    user: str
    warehouse: str
    database: str
    schema: str
    private_key_path: str = ""
    role: str | None = None
    private_key: object | None = None
    private_key_passphrase: str | None = None
    authenticator: str = "key_pair"
    timeout: float = DEFAULT_TIMEOUT
    dialect: ClassVar[str] = "snowflake"
    capabilities: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def driver_available(cls) -> bool:
        try:
            _load_snowflake()
        except ImportError:
            return False
        return True

    def __post_init__(self) -> None:
        _require_ident(self.account, "account")
        _require_ident(self.user, "user")
        _require_ident(self.warehouse, "warehouse")
        _require_ident(self.database, "database")
        _require_ident(self.schema, "schema")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        method = (self.authenticator or "key_pair").strip().lower()
        if method not in {"key_pair", "externalbrowser"}:
            raise ValueError("authenticator must be key_pair or externalbrowser")
        if method == "key_pair" and self.private_key is None:
            _require_ident(self.private_key_path, "private_key_path")

    def run(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        sql = _require_sql(sql)
        if dry_run:
            raise DryRunNotSupported(
                "Snowflake cannot estimate cost without executing"
            )
        ctx = None
        cur = None
        try:
            ctx = _open_connection(**self._connect_kwargs())
            cur = ctx.cursor()
            cur.execute(sql)
            columns = [col[0] for col in (cur.description or [])]
            raw_rows = cur.fetchall() or []
            rows = [dict(zip(columns, row)) for row in raw_rows]
            job_id = getattr(cur, "sfqid", None)
        except (AdapterError, ImportError, DryRunNotSupported):
            raise
        except Exception as exc:
            raise AdapterError(str(exc) or "Snowflake query failed") from exc
        finally:
            if cur is not None:
                cur.close()
            if ctx is not None:
                ctx.close()
        return QueryResult(rows=rows, job_id=job_id)

    def _connect_kwargs(self) -> dict[str, Any]:
        return _connect_kwargs(
            account=self.account,
            user=self.user,
            warehouse=self.warehouse,
            database=self.database,
            schema=self.schema,
            private_key_path=self.private_key_path,
            role=self.role,
            private_key=self.private_key,
            private_key_passphrase=self.private_key_passphrase,
            timeout=self.timeout,
            authenticator=self.authenticator,
        )


def _catalog_connect(
    *,
    account: str,
    user: str,
    warehouse: str = "",
    private_key_path: str = "",
    database: str | None = None,
    schema: str | None = None,
    role: str | None = None,
    private_key_passphrase: str | None = None,
    authenticator: str = "key_pair",
) -> Any:
    """Open a session for SHOW / DESCRIBE. Compute warehouse is optional."""
    return _open_connection(
        **_connect_kwargs(
            account=_require_ident(account, "account"),
            user=_require_ident(user, "user"),
            warehouse=(warehouse or "").strip() or None,
            database=(database or "").strip() or None,
            schema=(schema or "").strip() or None,
            private_key_path=private_key_path,
            role=(role or "").strip() or None,
            private_key=None,
            private_key_passphrase=private_key_passphrase,
            timeout=DEFAULT_TIMEOUT,
            authenticator=authenticator,
        )
    )


def _flag_yes(value: Any) -> bool:
    return str(value or "").strip().upper() in {"Y", "YES", "TRUE", "1"}


def _fetch_maps(cur: Any) -> list[dict[str, Any]]:
    columns = [col[0] for col in (cur.description or [])]
    return [dict(zip(columns, row)) for row in (cur.fetchall() or [])]


def list_roles(
    *,
    account: str,
    user: str,
    private_key_path: str = "",
    private_key_passphrase: str | None = None,
    authenticator: str = "key_pair",
) -> list[str]:
    """Roles granted to ``user``. Metadata only; no compute warehouse.

    Connects as the user's default role. ``SHOW GRANTS TO USER`` is the
    roles they can assume, not every role visible in the account.
    """
    user = _require_ident(user, "user")
    ctx = None
    cur = None
    try:
        ctx = _catalog_connect(
            account=account,
            user=user,
            private_key_path=private_key_path,
            private_key_passphrase=private_key_passphrase,
            authenticator=authenticator,
        )
        cur = ctx.cursor()
        cur.execute(f"SHOW GRANTS TO USER {_quote_ident(user)}")
        names: set[str] = set()
        for row in _fetch_maps(cur):
            name = _row_get(row, "role", "name")
            if name:
                names.add(str(name))
    except (AdapterError, ImportError, ValueError):
        raise
    except Exception as exc:
        raise AdapterError(str(exc) or "could not list roles") from exc
    finally:
        if cur is not None:
            cur.close()
        if ctx is not None:
            ctx.close()
    return sorted(names, key=str.lower)


def list_warehouses(
    *,
    account: str,
    user: str,
    private_key_path: str = "",
    role: str | None = None,
    private_key_passphrase: str | None = None,
    authenticator: str = "key_pair",
) -> dict[str, Any]:
    """Compute warehouses the current role can see. No warehouse needed to list."""
    ctx = None
    cur = None
    try:
        ctx = _catalog_connect(
            account=account,
            user=user,
            role=role,
            private_key_path=private_key_path,
            private_key_passphrase=private_key_passphrase,
            authenticator=authenticator,
        )
        cur = ctx.cursor()
        cur.execute("SHOW WAREHOUSES")
        names: list[str] = []
        default: str | None = None
        for row in _fetch_maps(cur):
            name = _row_get(row, "name")
            if not name:
                continue
            text = str(name)
            names.append(text)
            if _flag_yes(_row_get(row, "is_default")):
                default = text
    except (AdapterError, ImportError, ValueError):
        raise
    except Exception as exc:
        raise AdapterError(str(exc) or "could not list warehouses") from exc
    finally:
        if cur is not None:
            cur.close()
        if ctx is not None:
            ctx.close()
    names = sorted(names, key=str.lower)
    return {"warehouses": names, "default": default}


def list_databases(
    *,
    account: str,
    user: str,
    warehouse: str = "",
    private_key_path: str,
    role: str | None = None,
    private_key_passphrase: str | None = None,
    authenticator: str = "key_pair",
) -> list[dict[str, str]]:
    """Database names. Uses SHOW DATABASES."""
    ctx = None
    cur = None
    try:
        ctx = _catalog_connect(
            account=account,
            user=user,
            warehouse=warehouse,
            private_key_path=private_key_path,
            role=role,
            private_key_passphrase=private_key_passphrase,
            authenticator=authenticator,
        )
        cur = ctx.cursor()
        cur.execute("SHOW DATABASES")
        names = []
        for row in _fetch_maps(cur):
            name = _row_get(row, "name", "database_name")
            if name:
                names.append(str(name))
    except (AdapterError, ImportError, ValueError):
        raise
    except Exception as exc:
        raise AdapterError(str(exc) or "could not list databases") from exc
    finally:
        if cur is not None:
            cur.close()
        if ctx is not None:
            ctx.close()
    return [{"id": name} for name in sorted(names, key=str.lower)]


def list_schemas(
    *,
    account: str,
    user: str,
    warehouse: str = "",
    database: str,
    private_key_path: str,
    role: str | None = None,
    private_key_passphrase: str | None = None,
    authenticator: str = "key_pair",
) -> list[str]:
    """Schema names in ``database``."""
    database = _require_ident(database, "database")
    ctx = None
    cur = None
    try:
        ctx = _catalog_connect(
            account=account,
            user=user,
            warehouse=warehouse,
            database=database,
            private_key_path=private_key_path,
            role=role,
            private_key_passphrase=private_key_passphrase,
            authenticator=authenticator,
        )
        cur = ctx.cursor()
        cur.execute(f"SHOW SCHEMAS IN DATABASE {_quote_ident(database)}")
        names = []
        for row in _fetch_maps(cur):
            name = _row_get(row, "name", "schema_name")
            if name:
                names.append(str(name))
    except (AdapterError, ImportError, ValueError):
        raise
    except Exception as exc:
        raise AdapterError(str(exc) or "could not list schemas") from exc
    finally:
        if cur is not None:
            cur.close()
        if ctx is not None:
            ctx.close()
    return sorted(names, key=str.lower)


def list_tables(
    *,
    account: str,
    user: str,
    warehouse: str = "",
    database: str,
    schema: str,
    private_key_path: str,
    role: str | None = None,
    private_key_passphrase: str | None = None,
    authenticator: str = "key_pair",
) -> dict[str, Any]:
    """Tables in ``database.schema``."""
    database = _require_ident(database, "database")
    schema = _require_ident(schema, "schema")
    ctx = None
    cur = None
    try:
        ctx = _catalog_connect(
            account=account,
            user=user,
            warehouse=warehouse,
            database=database,
            schema=schema,
            private_key_path=private_key_path,
            role=role,
            private_key_passphrase=private_key_passphrase,
            authenticator=authenticator,
        )
        cur = ctx.cursor()
        cur.execute(
            f"SHOW TABLES IN SCHEMA {_quote_ident(database)}.{_quote_ident(schema)}"
        )
        names = []
        for row in _fetch_maps(cur):
            name = _row_get(row, "name", "table_name")
            if name:
                names.append(str(name))
    except (AdapterError, ImportError, ValueError):
        raise
    except Exception as exc:
        raise AdapterError(str(exc) or "could not list tables") from exc
    finally:
        if cur is not None:
            cur.close()
        if ctx is not None:
            ctx.close()
    return {"tables": sorted(names, key=str.lower)}


def list_columns(
    *,
    account: str,
    user: str,
    warehouse: str = "",
    database: str,
    schema: str,
    table: str,
    private_key_path: str,
    role: str | None = None,
    private_key_passphrase: str | None = None,
    authenticator: str = "key_pair",
) -> dict[str, Any]:
    """Column names and types for ``database.schema.table``."""
    database = _require_ident(database, "database")
    schema = _require_ident(schema, "schema")
    table = _require_ident(table, "table")
    ctx = None
    cur = None
    try:
        ctx = _catalog_connect(
            account=account,
            user=user,
            warehouse=warehouse,
            database=database,
            schema=schema,
            private_key_path=private_key_path,
            role=role,
            private_key_passphrase=private_key_passphrase,
            authenticator=authenticator,
        )
        cur = ctx.cursor()
        cur.execute(
            f"DESCRIBE TABLE {_quote_ident(database)}"
            f".{_quote_ident(schema)}.{_quote_ident(table)}"
        )
        columns = []
        for row in _fetch_maps(cur):
            name = _row_get(row, "name", "column_name")
            raw_type = _row_get(row, "type", "data_type") or ""
            if not name:
                continue
            columns.append(
                {
                    "name": str(name),
                    "type": _normalise_type(str(raw_type)),
                }
            )
    except (AdapterError, ImportError, ValueError):
        raise
    except Exception as exc:
        raise AdapterError(str(exc) or "could not list columns") from exc
    finally:
        if cur is not None:
            cur.close()
        if ctx is not None:
            ctx.close()
    columns.sort(key=lambda c: str(c.get("name") or "").lower())
    return {"columns": columns}
