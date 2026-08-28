"""BigQuery execute adapter.

Pushes SQL into the caller's BigQuery project. Auth, project, location, and
the scan cap live on this class, not on ``Adapter``. Official
``google-cloud-bigquery`` client; not the ``bq`` CLI. Extra:
``pip install factcat[bigquery]``.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, ClassVar

from . import AdapterError, BytesCapError, QueryResult

DEFAULT_MAXIMUM_BYTES_BILLED = 10 * 1024**3  # 10 GiB
DEFAULT_TIMEOUT = 600.0

_EXTRA = "pip install factcat[bigquery]"


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
    return "bytes billed" in lowered or "maximumbytesbilled" in lowered


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
        return BytesCapError(
            message,
            maximum_bytes_billed=maximum_bytes_billed,
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
    return AdapterError(message)


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
        if self.credentials is None:
            return None
        if isinstance(self.credentials, str):
            try:
                return service_account.Credentials.from_service_account_file(
                    self.credentials
                )
            except FileNotFoundError as exc:
                raise AdapterError(
                    f"service-account JSON not found: {self.credentials}"
                ) from exc
            except Exception as exc:
                raise AdapterError(
                    f"could not load service-account JSON at {self.credentials}"
                ) from exc
        return self.credentials


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
