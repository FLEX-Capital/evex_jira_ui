"""The offline suite must never fall through to a real Jira/Assets request."""

from pathlib import Path

import pytest
import requests


@pytest.fixture
def public_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep isolated UI component tests independent of a developer's password."""
    import ui_auth

    monkeypatch.setattr(ui_auth, "configured_password", lambda: None)


@pytest.fixture(autouse=True)
def forbid_unmocked_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unmocked network request in the offline test suite")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every default artifact under test-owned temporary storage."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from test_sync_service import raw

    import asset_country
    import category_sync
    import jira_loader

    path = tmp_path / "jira_data.pkl"
    monkeypatch.setattr(
        jira_loader,
        "fetch_asset_object_uncached",
        lambda c, w, oid, **kwargs: {"id": oid, "label": "Resolved"},
    )
    monkeypatch.setattr(
        category_sync,
        "fetch_category_references",
        lambda keys, **kwargs: {key: raw()["fields"] for key in keys},
    )
    monkeypatch.setattr(asset_country, "load_cache", lambda *a, **k: {})
    return path
