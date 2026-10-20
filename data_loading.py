import fcntl
import os
import pickle  # nosec B403
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from settings import load_settings

DATA_PATH = "data/jira_data.pkl"
_mutex = threading.RLock()
_lock_state = threading.local()


def _ticket_path(path: str | Path | None = None) -> Path:
    return Path(path).resolve() if path is not None else load_settings().paths.tickets


class CacheChangedError(RuntimeError):
    """A writer's input snapshot is no longer the current cache."""


class ImportBusyError(RuntimeError):
    """Another UI or CLI process already owns the import operation."""


@contextmanager
def import_operation_lock(data_path=None):
    path = _ticket_path(data_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(f"{path}.import.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ImportBusyError(
                "Ein Datenimport läuft bereits. Bitte später erneut versuchen."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def cache_lock(path=None):
    """Serialize read/merge/write transactions across threads and CLI processes.

    Lock a stable sidecar, not the pickle inode replaced by atomic saves. The
    thread lock plus nesting check makes save_data safe inside a transaction.
    """
    path = _ticket_path(path)
    with _mutex:
        previous = getattr(_lock_state, "path", None)
        if previous == path:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with Path(f"{path}.lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _lock_state.path = path
            try:
                yield
            finally:
                _lock_state.path = previous
                fcntl.flock(handle, fcntl.LOCK_UN)


def cache_revision(path=None):
    try:
        stat = _ticket_path(path).stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def load_data_snapshot(path=None):
    """Read a coherent frame and revision for a later conditional save."""
    target = _ticket_path(path)
    with cache_lock(target):
        return load_data(path=target), cache_revision(target)


def save_data_if_unchanged(df, revision, *, backup_prefix=None, path=None):
    """Commit a snapshot only if no writer has replaced its input meanwhile."""
    target = _ticket_path(path)
    with cache_lock(target):
        if cache_revision(target) != revision:
            raise CacheChangedError(
                "Cache wurde parallel geändert; bitte die Aktualisierung wiederholen."
            )
        backup = None
        if backup_prefix and target.exists():
            backup = (
                f"{target}.bak-{backup_prefix}-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}"
            )
            shutil.copy2(target, backup)
        save_data(df, path=target)
        return backup


# Written by earlier versions of the loader and the category migration. They are
# constant (cloud/workspace ids) or diagnostics that belong in the refresh log,
# not per ticket, so they are dropped on the way in and out of the cache.
OBSOLETE_COLUMNS = (
    "assets_cloud_id",
    "assets_workspace_id",
    "asset_errors",
    "category_assets_cloud_id",
    "category_assets_workspace_id",
    "category_asset_errors",
)


def drop_obsolete_columns(df):
    """Return df without the obsolete asset bookkeeping columns."""
    if df is None:
        return df
    present = [column for column in OBSOLETE_COLUMNS if column in df.columns]
    return df.drop(columns=present) if present else df


def save_data(df, *, path=None):
    target = _ticket_path(path)
    with cache_lock(target):
        _save_data_unlocked(df, path=target)


def _save_data_unlocked(df, *, path=None):
    df = drop_obsolete_columns(df)
    path = _ticket_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            temporary = f.name
            pickle.dump(df, f)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_data(*, path=None):
    path = _ticket_path(path)
    if path.exists():
        with path.open("rb") as f:
            # bandit B301: DATA_PATH is written by save_data() on this machine,
            # so it is not untrusted input. If the cache ever becomes something
            # a third party can supply, switch to a non-executable format
            # (e.g. parquet) instead of suppressing this.
            return drop_obsolete_columns(pickle.load(f))  # nosec B301
    return None
