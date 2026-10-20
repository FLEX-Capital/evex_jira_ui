"""Check or explicitly strip saved execution state from tracked notebooks."""

import argparse
import json
import os
import shutil
import subprocess  # nosec B404
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NotebookViolation:
    """Execution metadata that should not be saved in a source notebook."""

    path: Path
    cell_index: int
    output_count: int
    execution_count_present: bool


class NotebookError(RuntimeError):
    """A safe notebook operation failure without payload or parser context."""

    def __init__(self, path: Path, code: str, cell_index: int | None = None) -> None:
        super().__init__(code)
        self.path = path
        self.code = code
        self.cell_index = cell_index


def _regular_path(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise NotebookError(path, "unsafe_path")
    if not path.is_file():
        raise NotebookError(path, "read_error")


def _read_notebook(path: Path) -> dict[str, Any]:
    _regular_path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise NotebookError(path, "read_error") from None
    except (ValueError, UnicodeError):
        raise NotebookError(path, "invalid_notebook") from None
    if not isinstance(value, dict) or not isinstance(value.get("cells"), list):
        raise NotebookError(path, "invalid_notebook")
    for index, cell in enumerate(value["cells"]):
        if (
            not isinstance(cell, dict)
            or not isinstance(cell.get("cell_type"), str)
            or cell["cell_type"]
            not in {
                "code",
                "markdown",
                "raw",
            }
        ):
            raise NotebookError(path, "invalid_notebook", index)
        if cell["cell_type"] == "code":
            count = cell.get("execution_count")
            if not isinstance(cell.get("outputs"), list) or (
                count is not None and (type(count) is not int or count < 0)
            ):
                raise NotebookError(path, "invalid_notebook", index)
    return value


def tracked_notebooks(repo_root: Path) -> list[Path]:
    """Find regular tracked notebooks without consulting ignored local files."""
    root = repo_root.resolve()
    git_executable = shutil.which("git")
    if git_executable is None:
        raise NotebookError(root, "git_error")
    try:
        # Paths stay separate arguments to this fixed read-only command.
        result = subprocess.run(  # nosec B603
            [git_executable, "ls-files", "-z", "--", "*.ipynb"],
            cwd=root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise NotebookError(root, "git_error") from None
    paths = []
    for name in result.stdout.split(b"\0"):
        if not name:
            continue
        path = root / os.fsdecode(name)
        _regular_path(path)
        if not path.resolve().is_relative_to(root):
            raise NotebookError(path, "unsafe_path")
        paths.append(path)
    return sorted(paths)


def _violations(path: Path, notebook: dict[str, Any]) -> list[NotebookViolation]:
    return [
        NotebookViolation(
            path, index, len(cell["outputs"]), cell.get("execution_count") is not None
        )
        for index, cell in enumerate(notebook["cells"])
        if cell["cell_type"] == "code"
        and (cell["outputs"] or cell.get("execution_count") is not None)
    ]


def inspect_notebook(path: Path) -> list[NotebookViolation]:
    """Read execution-state violations without modifying notebook content."""
    return _violations(path, _read_notebook(path))


def strip_notebook(path: Path) -> bool:
    """Atomically clear execution state, preserving source and unrelated metadata."""
    notebook = _read_notebook(path)
    if not _violations(path, notebook):
        return False
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            cell.update(outputs=[], execution_count=None)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = handle.name
            json.dump(notebook, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        _regular_path(path)
        os.replace(temporary, path)
    except OSError:
        raise NotebookError(path, "write_error") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return True


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=True, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    """Check tracked notebooks by default; strip only on an explicit request."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="check without writing (default)"
    )
    mode.add_argument(
        "--strip", action="store_true", help="explicitly clear execution state"
    )
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--path", type=Path, action="append", help="select a path; repeatable"
    )
    args = parser.parse_args(argv)
    try:
        paths = (
            [p if p.is_absolute() else args.repo_root / p for p in args.path]
            if args.path
            else tracked_notebooks(args.repo_root)
        )
    except NotebookError as error:
        _emit({"path": error.path, "code": error.code, "cell_index": error.cell_index})
        _emit({"checked": 0, "dirty": 0, "errors": 1})
        return 2
    dirty = errors = 0
    for path in paths:
        try:
            violations = inspect_notebook(path)
            if args.strip:
                strip_notebook(path)
            elif violations:
                dirty += 1
                for violation in violations:
                    _emit({"code": "saved_execution_state", **asdict(violation)})
        except NotebookError as error:
            errors += 1
            _emit(
                {"path": error.path, "code": error.code, "cell_index": error.cell_index}
            )
    _emit({"checked": len(paths), "dirty": dirty, "errors": errors})
    return 2 if errors else int(bool(dirty))


if __name__ == "__main__":
    raise SystemExit(main())
