"""Run native, locked Ruff on working or isolated index content without adapter conversion."""

import argparse
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

_CONFIGS = {"pyproject.toml", "ruff.toml", ".ruff.toml"}


def _git(root: Path, *args: str, index_file: Path | None = None) -> bytes:
    git_executable = shutil.which("git")
    if git_executable is None:
        raise ValueError("git_unavailable")
    env = dict(os.environ)
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    elif not env.get("GIT_INDEX_FILE"):
        env.pop("GIT_INDEX_FILE", None)
    # Callers supply fixed read-only Git subcommands and separate argument values.
    return subprocess.check_output(  # nosec B603
        [git_executable, "-C", str(root), *args], stderr=subprocess.DEVNULL, env=env
    )


def _inventory(root: Path, index_file: Path | None = None) -> list[tuple[str, str, str]]:
    records = []
    for item in _git(root, "ls-files", "--stage", "-z", index_file=index_file).split(b"\0"):
        if not item:
            continue
        header, raw_name = item.split(b"\t", 1)
        mode, digest, stage = header.decode("ascii").split()
        name = os.fsdecode(raw_name)
        path = Path(name)
        if path.suffix not in {".py", ".pyi", ".ipynb"} and path.name not in _CONFIGS:
            continue
        if (
            stage != "0"
            or mode not in {"100644", "100755"}
            or path.is_absolute()
            or ".." in path.parts
        ):
            raise ValueError("invalid_index_entry")
        records.append((name, mode, digest))
    return records


def _check(root: Path, names: list[str], *, with_types: bool = False) -> int:
    # Both CLI and imported entrypoints resolve the same maintained guard.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.notebook_outputs import NotebookError, inspect_notebook

    for name in names:
        if not name.endswith(".ipynb"):
            continue
        try:
            if inspect_notebook(root / name):
                print(json.dumps({"path": name, "code": "saved_execution_state"}))
                return 1
        except NotebookError as error:
            print(json.dumps({"path": name, "code": error.code}))
            return 2
    source = [
        name
        for name in names
        if Path(name).suffix in {".py", ".pyi", ".ipynb"}
        and not name.startswith("analysis/archive/")
    ]
    if not source:
        return 0
    # The interpreter/module are fixed and source paths follow the option terminator.
    lint = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format=json",
            "--",
            *source,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if lint.returncode not in (0, 1):
        print(json.dumps({"code": "ruff_check_failed", "status": lint.returncode}))
        return 2
    try:
        diagnostics = json.loads(lint.stdout)
    except ValueError:
        print(json.dumps({"code": "invalid_ruff_diagnostics"}))
        return 2
    for diagnostic in diagnostics:
        print(
            json.dumps(
                {
                    "path": str(Path(diagnostic["filename"]).relative_to(root)),
                    "code": diagnostic["code"],
                    "line": diagnostic["location"]["row"],
                    "cell": diagnostic.get("cell"),
                    "column": diagnostic["location"]["column"],
                    "message": diagnostic["message"],
                },
                ensure_ascii=True,
            )
        )
    # Formatting checks use the same fixed interpreter and separate source arguments.
    formatting = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            "--no-cache",
            "--",
            *source,
        ],
        cwd=root,
        capture_output=True,
    )
    if formatting.returncode:
        print(json.dumps({"code": "ruff_format_failed", "status": formatting.returncode}))
    if lint.returncode or formatting.returncode:
        return 1
    if with_types:
        # One configured scope in the same snapshot; no source argument list or shell.
        checked = subprocess.run(  # nosec B603
            [
                sys.executable,
                "-m",
                "mypy",
                "--python-executable",
                sys.executable,
                "--no-pretty",
                "--no-error-summary",
                "--show-error-codes",
            ],
            cwd=root,
            env=dict(os.environ, MYPYPATH=str(root), PYTHONPATH=str(root)),
            capture_output=True,
            text=True,
        )
        if checked.returncode not in (0, 1):
            print(json.dumps({"code": "mypy_tool_failed", "status": checked.returncode}))
            return 2
        for line in checked.stdout.splitlines():
            print(json.dumps({"code": "mypy", "message": line}, ensure_ascii=True))
        return checked.returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Check tracked working files or index bytes/config, without rewriting source or index."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--staged", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--types", action="store_true", help="check the full configured mypy scope")
    parser.add_argument(
        "--index-file", help="explicit hook-provided Git index; empty uses Git default"
    )
    args = parser.parse_args(argv)
    root: Path = args.repo_root.resolve()
    try:
        index_file: Path | None = None
        if args.index_file:
            index_file = Path(args.index_file)
            if not index_file.is_absolute():
                index_file = root / index_file
            index_file = index_file.resolve()
        inventory = _inventory(root, index_file)
        names = [name for name, _, _ in inventory]
        if not args.staged:
            return _check(root, names, with_types=args.types)
        with tempfile.TemporaryDirectory(prefix="python-index-") as directory:
            snapshot = Path(directory).resolve()
            for name, _, digest in inventory:
                target = snapshot / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_git(root, "cat-file", "blob", digest, index_file=index_file))
            return _check(snapshot, names, with_types=args.types)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print(json.dumps({"code": "python_quality_error"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
