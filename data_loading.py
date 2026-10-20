import fcntl
import pickle  # nosec B403
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from file_io import Revision, atomic_write, path_lock
from settings import DEFAULT_TENANT, TENANT_ATTR, Settings, SettingsError, load_settings

DATA_PATH = "data/jira_data.pkl"


def _ticket_path(
    path: str | Path | None = None, *, settings: Settings | None = None
) -> Path:
    return (
        Path(path).resolve()
        if path is not None
        else (settings or load_settings()).paths.tickets
    )


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
    with path_lock(_ticket_path(path)):
        yield


def cache_revision(path=None) -> Revision | None:
    try:
        stat = _ticket_path(path).stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def load_data_snapshot(path=None, *, settings=None):
    """Read a coherent frame and revision for a later conditional save."""
    settings = settings or load_settings()
    target = _ticket_path(path, settings=settings)
    with cache_lock(target):
        return load_data(path=target, settings=settings), cache_revision(target)


def save_data_if_unchanged(
    df, revision, *, backup_prefix=None, path=None, settings=None
):
    """Commit a snapshot only if no writer has replaced its input meanwhile."""
    settings = settings or load_settings()
    target = _ticket_path(path, settings=settings)
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
        save_data(df, path=target, settings=settings)
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


def save_data(df, *, path=None, settings=None):
    settings = settings or load_settings()
    target = _ticket_path(path, settings=settings)
    with cache_lock(target):
        _save_data_unlocked(df, path=target, settings=settings)


def _save_data_unlocked(df, *, path=None, settings=None):
    config = settings or load_settings()
    df = drop_obsolete_columns(df).copy()
    identity = config.tenant.as_dict()
    if TENANT_ATTR in df.attrs and df.attrs[TENANT_ATTR] != identity:
        raise SettingsError("tenant_mismatch", ("JIRA_CLOUD_ID", "JIRA_WORKSPACE_ID"))
    df.attrs[TENANT_ATTR] = identity
    path = _ticket_path(path, settings=config)
    atomic_write(path, lambda handle: pickle.dump(df, handle))


def load_data(*, path=None, settings=None):
    config = settings or load_settings()
    path = _ticket_path(path, settings=config)
    if path.exists():
        with path.open("rb") as f:
            # bandit B301: DATA_PATH is written by save_data() on this machine,
            # so it is not untrusted input. If the cache ever becomes something
            # a third party can supply, switch to a non-executable format
            # (e.g. parquet) instead of suppressing this.
            frame = drop_obsolete_columns(pickle.load(f))  # nosec B301
        identity = frame.attrs.get(TENANT_ATTR)
        if identity is None:
            if config.tenant != DEFAULT_TENANT:
                raise SettingsError(
                    "legacy_tenant_unknown", ("JIRA_CLOUD_ID", "JIRA_WORKSPACE_ID")
                )
        elif identity != config.tenant.as_dict():
            raise SettingsError(
                "tenant_mismatch", ("JIRA_CLOUD_ID", "JIRA_WORKSPACE_ID")
            )
        frame.attrs[TENANT_ATTR] = config.tenant.as_dict()
        return frame
    return None
