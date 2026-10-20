"""Native Ruff preserves notebook diagnostics and checks the actual staged source/config."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

MARKER = "SYNTHETIC_PRIVATE_OUTPUT"


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def _notebook(source: str, *, dirty: bool = False) -> str:
    return (
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "id": "synthetic",
                        "metadata": {},
                        "execution_count": None,
                        "source": source.rstrip("\n").splitlines(keepends=True),
                        "outputs": (
                            [{"output_type": "stream", "name": "stdout", "text": [MARKER]}]
                            if dirty
                            else []
                        ),
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            sort_keys=True,
            indent=1,
        )
        + "\n"
    )


@pytest.fixture
def quality_repo(tmp_path: Path) -> Path:
    """Create a repository with a deliberately small, valid Ruff config and notebook."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nline-length=100\n[tool.ruff.lint]\nselect=["E", "F"]\n'
    )
    (tmp_path / "analysis.ipynb").write_text(_notebook("value = 1\n"))
    _git(tmp_path, "add", ".")
    return tmp_path


def test_native_notebook_quality_handles_multiline_locations(
    quality_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Multiline source errors fail normally while outputs have a separate payload-safe guard."""
    from tools.check_python import main

    args = ["--all", "--repo-root", str(quality_repo)]
    assert main(args) == 0
    path = quality_repo / "analysis.ipynb"
    path.write_text(_notebook("value = (\n    missing_name\n)\n"))
    assert main(args) == 1
    report = capsys.readouterr().out
    assert "F821" in report and "PositionToOffset" not in report
    assert '"cell": 1' in report
    path.write_text(_notebook("value = 1\n", dirty=True))
    assert main(args) == 1
    assert MARKER not in capsys.readouterr().out


def test_staged_quality_reads_index_and_preserves_unstaged_work(
    quality_repo: Path,
) -> None:
    """Opposite working/index contents prove source and configuration come from the index."""
    from tools.check_python import main

    path = quality_repo / "analysis.ipynb"
    path.write_text(_notebook("value = missing_name\n"))
    config = quality_repo / "pyproject.toml"
    config.write_text("not a valid TOML file")
    before = (
        path.read_bytes(),
        config.read_bytes(),
        _git(quality_repo, "ls-files", "--stage"),
    )
    assert main(["--staged", "--repo-root", str(quality_repo)]) == 0
    assert before == (
        path.read_bytes(),
        config.read_bytes(),
        _git(quality_repo, "ls-files", "--stage"),
    )
    _git(quality_repo, "add", "analysis.ipynb")
    path.write_text(_notebook("value = 1\n"))
    assert main(["--staged", "--repo-root", str(quality_repo)]) == 1


def test_formatter_and_lint_scope_are_unambiguous(quality_repo: Path) -> None:
    """Archive source exclusions never exempt saved outputs; maintained source remains checked."""
    from tools.check_python import main

    archive = quality_repo / "analysis/archive"
    archive.mkdir(parents=True)
    path = archive / "old.ipynb"
    path.write_text(_notebook("old_undefined_name\n"))
    _git(quality_repo, "add", ".")
    args = ["--all", "--repo-root", str(quality_repo)]
    assert main(args) == 0
    path.write_text(_notebook("old_undefined_name\n", dirty=True))
    assert main(args) == 1
    path.write_text(_notebook("old_undefined_name\n"))
    source = quality_repo / "maintained.py"
    source.write_text("value=1\n")
    _git(quality_repo, "add", "maintained.py")
    assert main(args) == 1
    source.write_text("value = 1\n")
    assert main(args) == main(args) == 0


def test_quality_hooks_keep_non_python_gates() -> None:
    """Trunk hooks retain non-Python checks and require the native checker on commit/push."""
    import yaml

    config: dict[str, Any] = yaml.safe_load(
        (Path(__file__).parents[1] / ".trunk/trunk.yaml").read_text()
    )
    enabled = config["lint"]["enabled"]
    assert not any(name.startswith(("black@", "isort@")) for name in enabled)
    assert any(name.startswith("actionlint@") for name in enabled)
    actions = config["actions"]
    assert {"trunk-check-pre-commit", "trunk-check-pre-push", "python-quality"} <= set(
        actions["enabled"]
    )
    quality = next(item for item in actions["definitions"] if item["id"] == "python-quality")
    assert "--staged" in quality["run"]
    assert "--index-file" in quality["run"]
    assert {"pre-commit", "pre-push"} <= set(quality["triggers"][0]["git_hooks"])


def test_explicit_index_selection_overrides_default_index(quality_repo: Path) -> None:
    """Hook-provided alternate index bytes win even when the ordinary index differs."""
    import shutil

    from tools.check_python import main

    alternate = quality_repo / ".git/quality.index"
    shutil.copy2(quality_repo / ".git/index", alternate)
    (quality_repo / "analysis.ipynb").write_text(_notebook("value = missing_name\n"))
    _git(quality_repo, "add", "analysis.ipynb")
    args = ["--staged", "--repo-root", str(quality_repo)]
    assert main([*args, "--index-file", str(alternate)]) == 0
    assert main(args) == 1


def test_type_gate_is_required_without_overlapping_adapter() -> None:
    """The strict native scope is mandatory while Trunk retains its other enabled checks."""
    import yaml

    config = yaml.safe_load((Path(__file__).parents[1] / ".trunk/trunk.yaml").read_text())
    quality = next(
        item for item in config["actions"]["definitions"] if item["id"] == "python-quality"
    )
    assert "--types" in quality["run"]
    assert not any(item.startswith("mypy@") for item in config["lint"]["enabled"])
    assert {"trunk-check-pre-commit", "trunk-check-pre-push"} <= set(config["actions"]["enabled"])
