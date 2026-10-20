"""Prevent saved ticket output and unsafe notebook cleanup."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from tools import notebook_outputs as notebooks

MARKER = "SYNTHETIC_PRIVATE_MARKER"


@pytest.fixture
def dirty_notebook(tmp_path: Path) -> Path:
    """Create execution state alongside source and metadata that must survive."""
    path = tmp_path / "sample.ipynb"
    path.write_text(
        json.dumps(
            {
                "nbformat": 4,
                "nbformat_minor": 5,
                "metadata": {"keep": [1, "ä"]},
                "cells": [
                    {
                        "cell_type": "code",
                        "id": "cell-1",
                        "metadata": {"keep": True},
                        "source": ['print("keep")\r\n'],
                        "outputs": [{"output_type": "stream", "text": MARKER}],
                        "execution_count": 7,
                    },
                    {
                        "cell_type": "markdown",
                        "id": "cell-2",
                        "metadata": {},
                        "source": ["Keep this explanation.\n"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_check_is_read_only_and_reports_only_metadata(
    dirty_notebook: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject outputs without exposing or rewriting notebook content."""
    before = dirty_notebook.read_bytes()
    assert notebooks.main(["--check", "--path", str(dirty_notebook)]) == 1
    assert dirty_notebook.read_bytes() == before
    output = capsys.readouterr()
    assert MARKER not in output.out + output.err
    assert "print" not in output.out + output.err
    records = [json.loads(line) for line in output.out.splitlines()]
    assert records[-1] == {"checked": 1, "dirty": 1, "errors": 0}
    violation = notebooks.inspect_notebook(dirty_notebook)[0]
    assert (violation.cell_index, violation.output_count) == (0, 1)
    assert violation.execution_count_present is True


def test_strip_preserves_every_non_execution_field(dirty_notebook: Path) -> None:
    """Clear execution state while preserving the complete source document."""
    before = json.loads(dirty_notebook.read_text())
    expected = copy.deepcopy(before)
    expected["cells"][0].update(outputs=[], execution_count=None)
    assert notebooks.strip_notebook(dirty_notebook) is True
    assert json.loads(dirty_notebook.read_text()) == expected
    assert notebooks.inspect_notebook(dirty_notebook) == []
    assert notebooks.strip_notebook(dirty_notebook) is False
    assert notebooks.main(["--check", "--path", str(dirty_notebook)]) == 0


@pytest.mark.parametrize("count", [0, 7])
def test_execution_count_alone_is_dirty(dirty_notebook: Path, count: int) -> None:
    """A count is saved execution state even when the cell has no output."""
    payload = json.loads(dirty_notebook.read_text())
    payload["cells"][0].update(outputs=[], execution_count=count)
    dirty_notebook.write_text(json.dumps(payload))
    violation = notebooks.inspect_notebook(dirty_notebook)[0]
    assert violation.output_count == 0
    assert violation.execution_count_present
    assert notebooks.main(["--path", str(dirty_notebook)]) == 1


@pytest.mark.parametrize(
    "text",
    [
        '{"cells": ["' + MARKER,
        json.dumps({"cells": MARKER}),
        json.dumps({"cells": [MARKER]}),
        json.dumps({"cells": [{"cell_type": "code", "outputs": {MARKER: 1}}]}),
        json.dumps({"cells": [{"cell_type": "code", "outputs": [], "execution_count": MARKER}]}),
        json.dumps({"cells": [{"cell_type": "code", "outputs": [], "execution_count": True}]}),
    ],
)
def test_malformed_input_never_echoes_payload(
    tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid JSON or cell structure cannot leak exception context or be stripped."""
    path = tmp_path / "invalid.ipynb"
    path.write_text(text)
    for mode in ("--check", "--strip"):
        assert notebooks.main([mode, "--path", str(path)]) == 2
        output = capsys.readouterr()
        assert MARKER not in output.out + output.err
        assert "invalid_notebook" in output.out
        assert path.read_text() == text


def test_git_discovery_is_tracked_only_and_nul_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discover nested tracked paths independently of shell splitting or cwd."""
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tracked = repo / "nested" / "a b ä.ipynb"
    tracked.parent.mkdir()
    clean = json.dumps({"cells": []})
    tracked.write_text(clean)
    (repo / "untracked.ipynb").write_text(clean)
    subprocess.run(["git", "-C", str(repo), "add", "--", "nested/a b ä.ipynb"], check=True)
    monkeypatch.chdir(tmp_path)
    assert notebooks.tracked_notebooks(repo) == [tracked]
    assert notebooks.main(["--repo-root", str(repo)]) == 0
    assert notebooks.main(["--repo-root", str(repo), "--path", "nested/a b ä.ipynb"]) == 0


def test_unsafe_or_missing_paths_fail_without_writes(
    tmp_path: Path, dirty_notebook: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Symlinks, missing files, and failed discovery never change their targets."""
    before = dirty_notebook.read_bytes()
    link = tmp_path / "alias.ipynb"
    link.symlink_to(dirty_notebook)
    for path in (link, tmp_path / "missing.ipynb"):
        assert notebooks.main(["--strip", "--path", str(path)]) == 2
    alias_dir = tmp_path / "alias-dir"
    alias_dir.symlink_to(dirty_notebook.parent, target_is_directory=True)
    assert notebooks.main(["--strip", "--path", str(alias_dir / dirty_notebook.name)]) == 2
    assert notebooks.main(["--repo-root", str(tmp_path)]) == 2
    assert dirty_notebook.read_bytes() == before
    assert MARKER not in capsys.readouterr().out


def test_tracked_symlink_cannot_escape_repository(tmp_path: Path) -> None:
    """Git-tracked symlink content is not treated as notebook input."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    target = tmp_path / "private.ipynb"
    target.write_text(MARKER)
    (repo / "alias.ipynb").symlink_to(target)
    subprocess.run(["git", "-C", str(repo), "add", "alias.ipynb"], check=True)
    assert notebooks.main(["--repo-root", str(repo)]) == 2
    assert target.read_text() == MARKER


def test_failed_replace_preserves_original(
    dirty_notebook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted publication leaves the old notebook and no abandoned temp."""
    before = dirty_notebook.read_bytes()

    def fail_replace(*args: object) -> None:
        raise OSError(MARKER)

    monkeypatch.setattr("tools.notebook_outputs.os.replace", fail_replace)
    with pytest.raises(notebooks.NotebookError) as caught:
        notebooks.strip_notebook(dirty_notebook)
    assert caught.value.code == "write_error"
    assert MARKER not in str(caught.value)
    assert dirty_notebook.read_bytes() == before
    assert list(dirty_notebook.parent.iterdir()) == [dirty_notebook]


def test_strip_cli_returns_clean_without_payload(
    dirty_notebook: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Explicit strip mode cleans a selected fixture without printing its output."""
    assert notebooks.main(["--strip", "--path", str(dirty_notebook)]) == 0
    assert notebooks.inspect_notebook(dirty_notebook) == []
    assert MARKER not in capsys.readouterr().out


@pytest.mark.parametrize("cell_type", [[], {}])
@pytest.mark.parametrize("mode", ["--check", "--strip"])
def test_malformed_cell_types_use_safe_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], cell_type: object, mode: str
) -> None:
    """Unhashable cell types return a safe validation error without writes or payload."""
    path = tmp_path / "bad.ipynb"
    path.write_text(json.dumps({"cells": [{"cell_type": cell_type, "source": [MARKER]}]}))
    before = path.read_bytes()
    assert notebooks.main([mode, "--path", str(path)]) == 2
    assert path.read_bytes() == before
    output = capsys.readouterr().out
    assert "invalid_notebook" in output and MARKER not in output and "TypeError" not in output
