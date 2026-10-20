"""One locked graph supplies runtime installation and optional analysis groups."""

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _names(values: list[str]) -> set[str]:
    return {re.split(r"[\s<>=!\[]", value, maxsplit=1)[0].lower() for value in values}


def test_default_dependencies_exclude_analysis_tools() -> None:
    """Only dashboard and non-AI maintenance packages are directly installed."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    names = _names(project["project"]["dependencies"])
    assert {
        "anthropic",
        "dotenv",
        "jupyter",
        "openai",
        "python-pptx",
        "python-dotenv",
    }.isdisjoint(names)
    assert {"requests", "openpyxl"} <= names
    groups = project["dependency-groups"]
    assert {
        "jupyter",
        "openai",
        "pydantic",
        "tqdm",
        "python-pptx",
        "pyarrow",
    } <= _names(groups["analysis"])
    assert "python-dotenv" not in _names(groups["analysis"])
    assert {"pytest", "pytest-cov", "httpx"} <= _names(groups["dev"])


def test_export_contract_rejects_stale_or_unhashed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical export detects drift without rewriting manifests or lock."""
    from tools.dependency_export import main

    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    lock = (ROOT / "uv.lock").read_bytes()
    export = tmp_path / "runtime.txt"
    assert main(["--output", str(export)]) == 0
    canonical = export.read_bytes()
    assert b"--hash=sha256:" in canonical and b"-e ." not in canonical
    assert main(["--check", "--output", str(export)]) == 0
    export.write_text("requests==0.0.0\n")
    assert main(["--check", "--output", str(export)]) == 1
    assert export.read_text() == "requests==0.0.0\n"
    assert (ROOT / "uv.lock").read_bytes() == lock


def test_locked_groups_preserve_compatibility_constraints() -> None:
    """The runtime export uses the authored lock and preserves compatibility bounds."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    deps = project["project"]["dependencies"]
    assert "streamlit>=1.54.0,<1.57" in deps
    assert "pandas>=2.3.3,<3" in deps
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {(p["name"], p["version"]) for p in lock["package"] if "version" in p}
    manifest = (ROOT / "requirements.runtime.txt").read_text()
    entries = re.findall(r"^([a-z0-9-]+)==([^ ;\\\n]+)", manifest, re.M)
    assert entries and all(entry in versions for entry in entries)
    for block in re.split(r"\n(?=[a-z0-9-]+==)", manifest):
        if re.search(r"^[a-z0-9-]+==", block, re.M):
            assert "--hash=sha256:" in block
    assert not (ROOT / "requirements.txt").exists()


def test_clean_runtime_imports_supported_entry_points(
    clean_runtime: Path, tmp_path: Path
) -> None:
    """Runtime imports and empty-cache Streamlit startup need no analysis packages."""
    modules = [
        "asset_country",
        "asset_migration",
        "backfill_country",
        "backfill_jira",
        "category_sync",
        "data_loading",
        "data_transformation",
        "desk_sync",
        "interactive",
        "issue_links",
        "jira_country_export",
        "jira_loader",
        "plotting",
        "reporting_policy",
        "resolution_bands",
        "service_desks",
        "settings",
        "source_sync",
        "styles",
        "sync_checkpoints",
        "sync_jobs",
        "temporary_history_pull",
        "ui_auth",
    ]
    code = (
        "import importlib,importlib.util,requests; "
        "requests.sessions.Session.request=lambda *a,**k:"
        "(_ for _ in ()).throw(AssertionError('network')); "
        f"[importlib.import_module(name) for name in {modules!r}]; "
        "assert importlib.util.find_spec('openai') is None; "
        "assert importlib.util.find_spec('anthropic') is None; "
        "from streamlit.testing.v1 import AppTest; "
        f"app=AppTest.from_file({str(ROOT / 'app.py')!r},default_timeout=30).run(); "
        "assert not app.exception; print('runtime-ok')"
    )
    env = dict(os.environ, DATA_DIR=str(tmp_path / "data"), PYTHONPATH=str(ROOT))
    for name in [
        "JIRA_URL",
        "JIRA_USERNAME",
        "JIRA_PASSWORD",
        "UI_PASSWORD",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
    ]:
        env.pop(name, None)
    result = subprocess.run(
        [str(clean_runtime), "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "runtime-ok" in result.stdout
