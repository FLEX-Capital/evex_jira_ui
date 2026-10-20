"""Failure, ordering, process isolation and import contracts for file primitives."""

import itertools
import json
import multiprocessing
import os
import subprocess
import sys
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import BinaryIO

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failure", ["serialize", "flush", "fsync", "replace"])
def test_failed_write_preserves_previous_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Partial writes never replace old bytes or remove a different writer's file."""
    import file_io

    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    other = tmp_path / ".evex-atomic-state.json-other.tmp"
    other.write_bytes(b"other writer")

    def fail(*args: object) -> None:
        raise OSError("SYNTHETIC_PRIVATE_MARKER")

    def writer(handle: BinaryIO) -> None:
        handle.write(b"partial")
        if failure == "serialize":
            fail()
        if failure == "flush":
            monkeypatch.setattr(handle, "flush", fail)

    if failure in ("fsync", "replace"):
        monkeypatch.setattr(file_io.os, failure, fail)
    with pytest.raises(file_io.AtomicWriteError) as caught:
        file_io.atomic_write(target, writer)
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(caught.value)
    assert str(target) in str(caught.value)
    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".evex-atomic-*.tmp")) == [other]


def _process_write(target: str, marker: bytes, entered: Event, release: Event) -> None:
    """Hold two independent temporary files before allowing atomic publication."""
    from file_io import atomic_write, path_lock

    def writer(handle: BinaryIO) -> None:
        handle.write(marker)
        entered.set()
        assert release.wait(10)

    atomic_write(Path(target), writer)
    with path_lock(Path(target)), path_lock(Path(target)):
        pass


def test_unique_temporary_and_nested_lock_are_process_safe(tmp_path: Path) -> None:
    """Competing processes publish complete values with unique temporary names."""
    target = tmp_path / "state.json"
    old, one, two = b"old", b"1" * 200_000, b"2" * 200_000
    target.write_bytes(old)
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    entered = [context.Event(), context.Event()]
    children = [
        context.Process(
            target=_process_write, args=(str(target), value, event, release)
        )
        for value, event in zip((one, two), entered, strict=True)
    ]
    try:
        for child in children:
            child.start()
        assert all(event.wait(10) for event in entered)
        assert len(list(tmp_path.glob(".evex-atomic-state.json-*.tmp"))) == 2
        assert target.read_bytes() == old
        release.set()
        while any(child.is_alive() for child in children):
            assert target.read_bytes() in (old, one, two)
            for child in children:
                child.join(0.01)
        assert all(child.exitcode == 0 for child in children)
        assert target.read_bytes() in (one, two)
        assert not list(tmp_path.glob(".evex-atomic-*.tmp"))
    finally:
        release.set()
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(3)


@pytest.mark.parametrize(
    "modules",
    list(itertools.permutations(("file_io", "sync_checkpoints", "desk_sync"))),
)
def test_fresh_import_has_no_checkpoint_importer_cycle(
    tmp_path: Path, modules: tuple[str, ...]
) -> None:
    """Every import order works offline and without creating mutable storage."""
    root = tmp_path / "absent"
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATA_DIR": str(root),
    }
    result = subprocess.run(
        [sys.executable, "-c", ";".join(f"import {m}" for m in modules)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not root.exists()


def test_file_sync_precedes_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target keeps its mode and replacement follows flushed, synced close."""
    import file_io

    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    target.chmod(0o640)
    events = []
    observed = {}
    original_sync, original_replace = file_io.os.fsync, file_io.os.replace

    def writer(handle: BinaryIO) -> None:
        observed["handle"] = handle
        original_flush = handle.flush

        def flush() -> None:
            events.append("flush")
            original_flush()

        monkeypatch.setattr(handle, "flush", flush)
        events.append("write")
        handle.write(b'"new"')

    def sync(fd: int) -> None:
        assert events[:2] == ["write", "flush"]
        events.append("fsync")
        original_sync(fd)

    def replace(source: str, destination: Path) -> None:
        assert observed["handle"].closed
        assert "fsync" in events
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(file_io.os, "fsync", sync)
    monkeypatch.setattr(file_io.os, "replace", replace)
    file_io.atomic_write(target, writer)
    assert json.loads(target.read_bytes()) == "new"
    assert target.stat().st_mode & 0o777 == 0o640
    assert (
        events.index("write")
        < events.index("flush")
        < events.index("fsync")
        < events.index("replace")
    )
