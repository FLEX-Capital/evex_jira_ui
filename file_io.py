"""Neutral file publication and reentrant path transactions for local storage."""

import errno
import fcntl
import json
import os
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

Revision = tuple[int, int, int]
_registry_guard = threading.Lock()
_registry: dict[Path, threading.RLock] = {}
_local = threading.local()


class FileSnapshotChangedError(RuntimeError):
    """An artifact was edited in place while its bytes were being read."""


def read_file_snapshot(path: Path) -> tuple[bytes, Revision]:
    """Read bytes and revision from one descriptor without creating sidecars."""
    with Path(path).open("rb") as handle:
        before = os.fstat(handle.fileno())
        payload = handle.read()
        after = os.fstat(handle.fileno())
    revision = (before.st_ino, before.st_size, before.st_mtime_ns)
    if revision != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise FileSnapshotChangedError("file_changed_during_read")
    return payload, revision


class AtomicWriteError(RuntimeError):
    """Publication failed at a safe named stage; payload details are excluded."""

    def __init__(self, code: str, path: Path) -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code}: {path}")


def atomic_write(path: Path, writer: Callable[[BinaryIO], None]) -> None:
    """Serialize, flush and sync an owned temporary before replacing the target.

    Args:
        path: Destination; an existing file's permission mode is retained.
        writer: Binary serializer, called once on a unique same-directory file.

    Raises:
        AtomicWriteError: Publication failed; only this writer's temp is removed.
    """
    path = Path(path)
    temporary: str | None = None
    handle: BinaryIO | None = None
    code = "prepare_failed"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".evex-atomic-{path.name}-", suffix=".tmp"
        )
        handle = os.fdopen(fd, "wb")
        code = "serialize_failed"
        writer(handle)
        code = "flush_failed"
        handle.flush()
        code = "fsync_failed"
        os.fsync(handle.fileno())
        code = "close_failed"
        handle.close()
        code = "mode_failed"
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass
        else:
            os.chmod(temporary, mode)
        code = "replace_failed"
        os.replace(temporary, path)
    except Exception:
        raise AtomicWriteError(code, path) from None
    finally:
        if handle is not None:
            with suppress(Exception):
                handle.close()
        if temporary is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary)


def write_json_atomic(path: Path, value: object) -> None:
    """Publish JSON as UTF-8 using the shared binary atomic writer."""

    def serialize(handle: BinaryIO) -> None:
        handle.write(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        )

    atomic_write(Path(path), serialize)


def lock_exclusive(handle: BinaryIO, *, blocking: bool = True) -> None:
    """Take an exclusive flock, reporting a refused non-blocking attempt uniformly.

    Azure Files (SMB) refuses a contended LOCK_NB with EACCES where local filesystems
    use EAGAIN; both mean another descriptor holds the lock.

    Args:
        handle: Open file whose descriptor is locked.
        blocking: Wait for the lock instead of refusing while it is held.

    Raises:
        BlockingIOError: Non-blocking, and another descriptor holds the lock.
    """
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except PermissionError as exc:
        if blocking or exc.errno != errno.EACCES:
            raise
        raise BlockingIOError(errno.EAGAIN, "path_busy") from exc


@contextmanager
def path_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Serialize threads/processes by path; optional refusal of active writers."""
    path = Path(path).resolve()
    with _registry_guard:
        mutex = _registry.setdefault(path, threading.RLock())
    if not mutex.acquire(blocking=blocking):
        raise BlockingIOError("path_busy")
    try:
        held = getattr(_local, "held", None)
        if held is None:
            held = _local.held = set()
        if path in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with Path(f"{path}.lock").open("a+b") as handle:
            lock_exclusive(handle, blocking=blocking)
            held.add(path)
            try:
                yield
            finally:
                held.remove(path)
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        mutex.release()
