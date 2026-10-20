"""Bounded operation causes safe for durable status and user-facing summaries."""

import re
from dataclasses import dataclass

import requests

from file_io import AtomicWriteError, FileSnapshotChangedError
from http_policy import InvalidSearchPayload, RetryDeferred

DIAGNOSTIC_CODES = frozenset(
    {
        "timeout",
        "connection",
        "rate_limited",
        "deferred",
        "permission",
        "not_found",
        "invalid_payload",
        "invalid_identity",
        "invalid_created",
        "busy",
        "cache_changed",
        "read_failed",
        "save_failed",
        "raw_failed",
        "unexpected",
    }
)
DIAGNOSTIC_STAGES = frozenset({"fetch", "transform", "links", "source", "assets", "save", "raw"})


@dataclass(frozen=True)
class OperationDiagnostic:
    """A payload-free cause attached to a bounded operation and ticket identity."""

    operation_id: str
    stage: str
    code: str
    project: str | None = None
    key: str | None = None
    http_status: int | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.code not in DIAGNOSTIC_CODES or self.stage not in DIAGNOSTIC_STAGES:
            raise ValueError("invalid_diagnostic_category")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", self.operation_id):
            raise ValueError("invalid_operation_id")
        for name, pattern in (
            ("project", r"[A-Z][A-Z0-9_]{0,30}"),
            ("key", r"[A-Z][A-Z0-9_]{0,30}-\d+"),
        ):
            value = getattr(self, name)
            if value is not None and not re.fullmatch(pattern, value):
                object.__setattr__(self, name, None)
        if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
            object.__setattr__(self, "http_status", None)


def safe_diagnostic(
    exc: Exception,
    *,
    operation_id: str,
    stage: str,
    project: str | None = None,
    key: str | None = None,
) -> OperationDiagnostic:
    """Classify known transport/storage errors without reading their messages.

    Args:
        exc: Failure; its message, response body and arbitrary code are ignored.
        operation_id: Generated operation identifier.
        stage: Known operation stage.
        project: Optional Jira project identity.
        key: Optional Jira ticket identity.

    Returns:
        A safe whitelisted cause, with unknown exception types marked unexpected.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    if type(status) is not int:
        status = getattr(exc, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    code, retryable = "unexpected", False
    if isinstance(exc, RetryDeferred):
        code, retryable = "deferred", True
    elif isinstance(exc, (requests.Timeout, TimeoutError)):
        code, retryable = "timeout", True
    elif isinstance(exc, requests.ConnectionError):
        code, retryable = "connection", True
    elif status == 429:
        code, retryable = "rate_limited", True
    elif status in (401, 403) or isinstance(exc, PermissionError):
        code = "permission"
    elif status == 404:
        code = "not_found"
    elif status is not None and status >= 500:
        code, retryable = "connection", True
    elif isinstance(exc, InvalidSearchPayload):
        code = "invalid_payload"
    elif isinstance(exc, FileSnapshotChangedError):
        code = "cache_changed"
    else:
        from cache_schema import CacheReadError, CacheSchemaError
        from data_loading import CacheChangedError, ImportBusyError

        if isinstance(exc, ImportBusyError):
            code = "busy"
        elif isinstance(exc, CacheChangedError):
            code = "cache_changed"
        elif isinstance(exc, (CacheReadError, CacheSchemaError)):
            code = "read_failed"
        elif isinstance(exc, (AtomicWriteError, OSError)):
            code = (
                "raw_failed"
                if stage == "raw"
                else ("save_failed" if stage == "save" else "read_failed")
            )
        elif isinstance(exc, (ValueError, KeyError, TypeError)):
            code = "invalid_payload"
    return OperationDiagnostic(operation_id, stage, code, project, key, status, retryable)
