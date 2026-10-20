"""Validated Jira searches and operation-specific, server-respecting retries."""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import requests


@dataclass(frozen=True)
class RetryPolicy:
    """An attempt budget, fallback schedule and maximum permitted single wait."""

    attempts: int
    fallback_delays: tuple[float, ...]
    max_delay: float = 300.0

    def __post_init__(self) -> None:
        if (
            type(self.attempts) is not int
            or self.attempts < 1
            or len(self.fallback_delays) != self.attempts - 1
            or not math.isfinite(self.max_delay)
            or self.max_delay < 0
            or any(not math.isfinite(delay) or delay < 0 for delay in self.fallback_delays)
        ):
            raise ValueError("invalid_retry_policy")


CATEGORY = RetryPolicy(3, (1, 2))
SOURCE = RetryPolicy(3, (1, 2))
COUNTRY = RetryPolicy(6, (2, 4, 8, 16, 32))


class RetryDeferredError(RuntimeError):
    """The server's required wait exceeds the operation's permitted delay."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        super().__init__(f"retry_deferred: {delay}")


class InvalidSearchPayloadError(ValueError):
    """A safe search failure code, excluding response contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# Preserve the approved API names while keeping concrete exception class names
# consistent with the repository's Error-suffix standard.
RetryDeferred = RetryDeferredError
InvalidSearchPayload = InvalidSearchPayloadError


def retry_after_delay(headers: Mapping[str, str], fallback: float, *, now: datetime) -> float:
    """Honor numeric/HTTP-date Retry-After; invalid values use the fallback."""
    value = next(
        (value for name, value in headers.items() if name.casefold() == "retry-after"),
        "",
    )
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            delay = (target - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
    return max(0.0, delay) if math.isfinite(delay) else fallback


def with_retries[T](
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> T:
    """Retry transient transport/HTTP faults within one operation's budget.

    Args:
        operation: One complete read attempt; non-transient errors propagate.
        policy: Attempt count, fallback schedule and permitted single delay.
        now: Aware injected clock for HTTP dates.
        sleep: Injected waiter; invoked only when a retry is permitted.

    Returns:
        The first successful attempt's result.

    Raises:
        RetryDeferred: The server requests more than the permitted wait budget.
    """
    for attempt in range(policy.attempts):
        try:
            return operation()
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None) if response is not None else None
            if status is None:
                status = getattr(exc, "status_code", None)
            transient = isinstance(exc, (requests.Timeout, requests.ConnectionError)) or (
                isinstance(status, int) and (status == 429 or 500 <= status < 600)
            )
            if not transient or attempt == policy.attempts - 1:
                raise
            headers = response.headers if response is not None else {}
            delay = retry_after_delay(headers, policy.fallback_delays[attempt], now=now())
            if delay > policy.max_delay:
                raise RetryDeferred(delay) from None
            sleep(delay)
    raise RuntimeError("retry_policy_exhausted")


def validate_search_page(payload: object) -> dict[str, Any]:
    """Require a keyed issue array and reject HTTP-success error payloads."""
    if not isinstance(payload, dict) or payload.get("errorMessages"):
        raise InvalidSearchPayload("invalid_search_payload")
    issues = payload.get("issues")
    if not isinstance(issues, list) or any(
        not isinstance(issue, dict)
        or not isinstance(issue.get("key"), str)
        or not issue["key"].strip()
        or ("fields" in issue and not isinstance(issue["fields"], dict))
        for issue in issues
    ):
        raise InvalidSearchPayload("invalid_issue_array")
    token = payload.get("nextPageToken")
    if token is not None and not isinstance(token, str):
        raise InvalidSearchPayload("invalid_pagination_token")
    return payload
