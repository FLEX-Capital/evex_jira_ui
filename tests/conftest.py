"""The offline suite must never fall through to a real Jira/Assets request."""

import pytest
import requests


@pytest.fixture(autouse=True)
def forbid_unmocked_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unmocked network request in the offline test suite")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
