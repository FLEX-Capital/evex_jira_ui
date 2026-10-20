"""Shared model-network guard, explicitly scoped to classifier test modules."""

import httpx
import pytest


@pytest.fixture
def forbid_model_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject actual HTTPX transports while retaining explicit fake clients."""

    def forbidden(self: object, request: httpx.Request) -> httpx.Response:
        raise AssertionError("Unmocked model network request in offline tests")

    async def async_forbidden(self: object, request: httpx.Request) -> httpx.Response:
        raise AssertionError("Unmocked model network request in offline tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_forbidden)
