"""One locked graph supplies runtime installation and optional analysis groups."""

import re
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
