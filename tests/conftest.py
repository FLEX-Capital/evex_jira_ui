"""The offline suite must never fall through to a real Jira/Assets request."""

import pytest
import requests


@pytest.fixture(autouse=True)
def forbid_unmocked_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unmocked network request in the offline test suite")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from test_sync_service import raw

    import asset_country
    import category_sync
    import data_loading
    import jira_loader

    path = tmp_path / "cache.pkl"
    monkeypatch.setattr(data_loading, "DATA_PATH", str(path))
    monkeypatch.setattr(
        jira_loader,
        "fetch_asset_object_uncached",
        lambda c, w, oid: {"id": oid, "label": "Resolved"},
    )
    monkeypatch.setattr(
        category_sync,
        "fetch_category_references",
        lambda keys: {key: raw()["fields"] for key in keys},
    )
    monkeypatch.setattr(asset_country, "load_cache", lambda *a, **k: {})
    return path
