"""BigQuery execute adapter.

Pushes SQL into the caller's BigQuery project. Auth, project, location, and
the scan cap live on this class, not on ``Adapter``. Official
``google-cloud-bigquery`` client; not the ``bq`` CLI. Extra:
``pip install factcat[bigquery]``.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, ClassVar

from . import (
    CAP_BYTES_PROCESSED,
    CAP_DRY_RUN,
    CAP_SCAN_CAP,
    AdapterError,
    BytesCapError,
    QueryResult,
)

DEFAULT_MAXIMUM_BYTES_BILLED = 10 * 1024**3  # 10 GiB
DEFAULT_TIMEOUT = 600.0

_EXTRA = "pip install factcat[bigquery]"


def adc_quota_project() -> str:
    """Billing/quota project from ADC, or empty if ADC is missing."""
    env = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    try:
        google_auth = importlib.import_module("google.auth")
        creds, project = google_auth.default()
    except Exception:
        return env
    quota = getattr(creds, "quota_project_id", None)
    return (quota or project or env or "").strip()


def _cli_project_value(stdout: str) -> str:
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        lowered = line.lower()
        if lowered in {"(unset)", "none"}:
            return ""
        if lowered.startswith("warning:") or lowered.startswith("your active"):
            continue
        return line
    return ""


def gcloud_config_project() -> str:
    """Active gcloud CLI project, or empty if gcloud is missing or unset.

    User ADC often has no ``quota_project_id``. ``gcloud config set project``
    is a separate store from application-default credentials. Identity chrome
    for Setup, not SQL generation, and not a live warehouse job.
    """
    env = os.environ.get("CLOUDSDK_CORE_PROJECT", "").strip()
    if env:
        return env
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": 5,
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["gcloud", "--quiet", "config", "get-value", "core/project"],
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return _cli_project_value(completed.stdout)


def _load_google() -> tuple[Any, Any]:
    try:
        bigquery = importlib.import_module("google.cloud.bigquery")
        service_account = importlib.import_module("google.oauth2.service_account")
    except ImportError as exc:
        raise ImportError(
            "BigQuery execute adapter requires google-cloud-bigquery. "
            f"Install it with: {_EXTRA}"
        ) from exc
    return bigquery, service_account


def _require_sql(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql must be a non-empty string")
    return sql


def _looks_like_bytes_cap(message: str) -> bool:
    lowered = message.lower()
    return (
        "bytes billed" in lowered
        or "maximumbytesbilled" in lowered
        or "bytesbilledlimitexceeded" in lowered
    )


# BigQuery states both figures when it rejects a job on the cap:
#   "Query exceeded limit for bytes billed: 10737418240. 39277559808 or higher
#    required.; reason: bytesBilledLimitExceeded"
# Sniffing that only to pick the error class and dropping the numbers leaves the
# app showing a raw 500 for a condition it fully understands.
_BYTES_CAP_LIMIT = re.compile(r"bytes billed:\s*(\d+)", re.IGNORECASE)
_BYTES_CAP_REQUIRED = re.compile(r"(\d+)\s+or higher required", re.IGNORECASE)


def _bytes_cap_figures(message: str) -> tuple[int | None, int | None]:
    """``(required, limit)`` from a cap rejection. Either may be None.

    ``required`` is BigQuery's *bytes billed* figure, not
    ``total_bytes_processed``: billing rounds up (10 MB minimum per table), so
    it can exceed the dry-run estimate for the same SQL. It is still the number
    to show someone deciding whether to override, which is why it rides
    ``BytesCapError.bytes_processed`` rather than a fourth field.
    """
    required = _BYTES_CAP_REQUIRED.search(message)
    limit = _BYTES_CAP_LIMIT.search(message)
    return (
        int(required.group(1)) if required else None,
        int(limit.group(1)) if limit else None,
    )


def _looks_like_adc(exc: BaseException, message: str) -> bool:
    if type(exc).__name__ == "DefaultCredentialsError":
        return True
    lowered = message.lower()
    return (
        "could not automatically determine credentials" in lowered
        or "application default credentials" in lowered
    )


def _wrap_google_error(
    exc: BaseException,
    *,
    maximum_bytes_billed: int | None,
    timeout: float,
) -> AdapterError:
    message = str(exc)
    if _looks_like_bytes_cap(message):
        required, limit = _bytes_cap_figures(message)
        return BytesCapError(
            message,
            bytes_processed=required,
            maximum_bytes_billed=limit if limit is not None else maximum_bytes_billed,
        )
    if _looks_like_adc(exc, message):
        return AdapterError(
            "BigQuery credentials not found. Run "
            "`gcloud auth application-default login` "
            "or pass a service-account JSON path as credentials."
        )
    name = type(exc).__name__
    if name in {"TimeoutError", "FuturesTimeoutError"} or "timed out" in message.lower():
        return AdapterError(f"BigQuery job timed out after {timeout}s")
    return AdapterError(message, not_found=name == "NotFound" or "not found:" in message.lower())


@dataclass(frozen=True)
class BigQueryAdapter:
    """Run SQL in the caller's BigQuery project.

    ``project`` and ``location`` are required so we never guess ADC quota
    project or BigQuery's US default. ``credentials`` is ADC when omitted, a
    JSON key path when a string, or a google credentials object otherwise.
    ``maximum_bytes_billed=None`` is unlimited.
    """

    project: str
    location: str
    credentials: object | None = None
    maximum_bytes_billed: int | None = DEFAULT_MAXIMUM_BYTES_BILLED
    timeout: float = DEFAULT_TIMEOUT
    dialect: ClassVar[str] = "bigquery"
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {CAP_DRY_RUN, CAP_BYTES_PROCESSED, CAP_SCAN_CAP}
    )

    @classmethod
    def driver_available(cls) -> bool:
        try:
            _load_google()
        except ImportError:
            return False
        return True

    def __post_init__(self) -> None:
        if not isinstance(self.project, str) or not self.project.strip():
            raise ValueError("project is required")
        if not isinstance(self.location, str) or not self.location.strip():
            raise ValueError("location is required")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.maximum_bytes_billed is not None:
            if type(self.maximum_bytes_billed) is not int:
                raise TypeError("maximum_bytes_billed must be int or None")
            if self.maximum_bytes_billed < 0:
                raise ValueError("maximum_bytes_billed must be >= 0")

    def run(self, sql: str, *, dry_run: bool = False) -> QueryResult:
        sql = _require_sql(sql)
        bigquery, service_account = _load_google()
        job_config = bigquery.QueryJobConfig()
        if self.maximum_bytes_billed is not None:
            job_config.maximum_bytes_billed = self.maximum_bytes_billed
        job_config.job_timeout_ms = int(self.timeout * 1000)
        if dry_run:
            job_config.dry_run = True
            job_config.use_query_cache = False
        try:
            client = bigquery.Client(
                project=self.project,
                credentials=self._credentials(service_account),
            )
            job = client.query(
                sql,
                job_config=job_config,
                location=self.location,
                timeout=self.timeout,
            )
            processed = _optional_int(getattr(job, "total_bytes_processed", None))
            billed = _optional_int(getattr(job, "total_bytes_billed", None))
            job_id = getattr(job, "job_id", None)
            if dry_run:
                if (
                    self.maximum_bytes_billed is not None
                    and processed is not None
                    and processed > self.maximum_bytes_billed
                ):
                    raise BytesCapError(
                        f"query would process {processed} bytes, "
                        f"cap is {self.maximum_bytes_billed}",
                        bytes_processed=processed,
                        maximum_bytes_billed=self.maximum_bytes_billed,
                    )
                return QueryResult(
                    rows=[],
                    bytes_processed=processed,
                    bytes_billed=billed,
                    job_id=job_id,
                )
            result = job.result(timeout=self.timeout)
            rows = [dict(row) for row in result]
            processed = _optional_int(getattr(job, "total_bytes_processed", processed))
            billed = _optional_int(getattr(job, "total_bytes_billed", billed))
        except (AdapterError, ImportError):
            raise
        except Exception as exc:
            raise _wrap_google_error(
                exc,
                maximum_bytes_billed=self.maximum_bytes_billed,
                timeout=self.timeout,
            ) from exc
        return QueryResult(
            rows=rows,
            bytes_processed=processed,
            bytes_billed=billed,
            job_id=job_id,
        )

    def _credentials(self, service_account: Any) -> Any:
        return _resolve_credentials(self.credentials, service_account)


def _resolve_credentials(credentials: object | None, service_account: Any) -> Any:
    if credentials is None:
        return None
    if isinstance(credentials, str):
        try:
            return service_account.Credentials.from_service_account_file(credentials)
        except FileNotFoundError as exc:
            raise AdapterError(
                f"service-account JSON not found: {credentials}"
            ) from exc
        except Exception as exc:
            raise AdapterError(
                f"could not load service-account JSON at {credentials}"
            ) from exc
    return credentials


def _make_client(project: str, credentials: object | None = None) -> Any:
    project = (project or "").strip()
    if not project:
        raise ValueError("project is required")
    bigquery, service_account = _load_google()
    return bigquery.Client(
        project=project,
        credentials=_resolve_credentials(credentials, service_account),
    )


def list_datasets(
    *, project: str, credentials: object | None = None
) -> list[dict[str, str]]:
    """Dataset ids in ``project``. Metadata API — no query bytes."""
    try:
        client = _make_client(project, credentials)
        ids = [item.dataset_id for item in client.list_datasets()]
    except (ValueError, AdapterError, ImportError):
        raise
    except Exception as exc:
        raise _wrap_google_error(
            exc, maximum_bytes_billed=None, timeout=DEFAULT_TIMEOUT
        ) from exc
    return [{"id": name} for name in sorted(ids)]


def list_tables(
    *, project: str, dataset: str, credentials: object | None = None
) -> dict[str, Any]:
    """Tables in ``project.dataset``, plus the dataset location."""
    dataset = (dataset or "").strip()
    if not dataset:
        raise ValueError("dataset is required")
    try:
        client = _make_client(project, credentials)
        ds = client.get_dataset(dataset)
        tables = [item.table_id for item in client.list_tables(ds)]
        location = getattr(ds, "location", None) or ""
    except (ValueError, AdapterError, ImportError):
        raise
    except Exception as exc:
        raise _wrap_google_error(
            exc, maximum_bytes_billed=None, timeout=DEFAULT_TIMEOUT
        ) from exc
    return {"location": location, "tables": sorted(tables)}


def list_columns(
    *,
    project: str,
    dataset: str,
    table: str,
    credentials: object | None = None,
) -> dict[str, Any]:
    """Column names and types for ``project.dataset.table``."""
    dataset = (dataset or "").strip()
    table = (table or "").strip()
    if not dataset:
        raise ValueError("dataset is required")
    if not table:
        raise ValueError("table is required")
    try:
        client = _make_client(project, credentials)
        tbl = client.get_table(f"{project}.{dataset}.{table}")
        columns = [
            {"name": field.name, "type": getattr(field, "field_type", "")}
            for field in (tbl.schema or [])
        ]
        location = getattr(tbl, "location", None) or ""
    except (ValueError, AdapterError, ImportError):
        raise
    except Exception as exc:
        raise _wrap_google_error(
            exc, maximum_bytes_billed=None, timeout=DEFAULT_TIMEOUT
        ) from exc
    return {"location": location, "columns": columns}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
