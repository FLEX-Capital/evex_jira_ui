"""The offline suite must never fall through to a real Jira/Assets request."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def pytest_addoption(parser: pytest.Parser) -> None:
    """Keep package installation opt-in, separate from the ordinary offline suite."""
    parser.addoption(
        "--run-runtime-install",
        action="store_true",
        dest="run_runtime_install",
        help="Install and smoke-test the hashed runtime in an isolated environment.",
    )

    parser.addoption(
        "--run-runtime-image",
        action="store_true",
        dest="run_runtime_image",
        help="Build and smoke-test the owned Docker runtime image.",
    )


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


@pytest.fixture
def clean_runtime(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """Install the hashed runtime only when explicitly requested by CI or an operator."""
    if not request.config.getoption("run_runtime_install", default=False):
        pytest.skip("requires --run-runtime-install")
    environment = tmp_path / "runtime"
    env = dict(os.environ)
    env.setdefault("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "-r",
            str(PROJECT_ROOT / "requirements.runtime.txt"),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    return python
